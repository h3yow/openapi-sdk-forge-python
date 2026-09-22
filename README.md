# OpenAPI SDK Forge（Python）

由服务运行时暴露的 OpenAPI 契约驱动的 **SDK 生成** 与 **版本兼容性校验** 工具。
同一份契约可以稳定产出可直接调用的 Python 客户端；契约演进时给出字段级、
可复现的兼容性结论。

## 能力总览

1. **契约驱动生成**：从运行服务的 `/openapi.json` 拉取契约，解析后生成零第三方
   依赖（仅标准库）的客户端包，覆盖：
   - 路径参数、查询参数、请求头参数
   - JSON 请求体与具名/内联请求模型
   - 多个响应状态（2xx/4xx 均可在元数据中区分；4xx/5xx 调用时抛 `ApiError`）
   - 具名对象模型、内联对象模型、字符串枚举、数组、字典、联合/可选类型
2. **严格契约校验**：契约中缺失或无法解析的片段 **不会被静默跳过**，而是抛出
   带精确定位的可区分错误（见“错误分类”）。
3. **版本化与兼容性判定**：按规则集版本（`RULES_VERSION`）判定新旧契约，
   破坏性变更精确到「接口 → 参数/请求体/响应状态 → 字段」。
4. **稳定的生成过程**：服务不可达、契约版本不匹配、并发重复触发均有确定行为；
   失败不残留半成品，重复请求不覆盖已完成结果。
5. **历史可复现**：归档原始契约 + 规则版本号；任意时刻可用
   `/forge/replay` 复现历史判定，相同输入得到字节级一致的结论。

## 支持的契约范围

OpenAPI **3.x**（FastAPI 默认产出 3.1.0），具体支持：

| 类别 | 支持内容 |
| --- | --- |
| 操作 | `get` / `post` / `put` / `patch` / `delete`；`operationId` 缺省时自动生成 |
| 参数 | `path`（必须 `required: true`）、`query`、`header`；不支持 `cookie` |
| 请求体 | JSON（`application/json`），具名 `$ref` 或内联对象 schema |
| 响应 | 任意已声明状态码（含 `default`），JSON 响应体；无响应体的状态（如 204） |
| 模型 | `object`（含 `required`）、`array`、`string|integer|number|boolean`、`enum`、`anyOf/oneOf`（含 `null` 联合）、`additionalProperties` 字典 |
| 引用 | **仅文档内** `$ref`（`#/components/...`），支持前向引用；外部/悬空引用直接报错 |
| 格式 | `date-time`/`date`（映射 `datetime`）、`uuid`、`binary`、int32/int64 等 |
| 范围过滤 | 生成时可用 `include_tags` 只纳入指定 tag（默认只生成业务 tag `items`） |

明确 **不支持** 并会报错的情况（不静默跳过）：OpenAPI 2.x、`cookie` 参数、
非 JSON 媒体类型、`allOf`、悬空/外部 `$ref`、未知 schema `type`、
空 `paths`、空响应声明、path 参数非必填、非法 `$ref` 目标等。

## 兼容性规则（规则集版本 `2026.09.01`）

以「旧客户端调用新服务」为基准：

### 接受（向后兼容）

- 新增接口
- 新增 **可选** 的查询/请求头参数
- 请求体新增 **可选** 字段；必填字段变为可选
- 响应新增字段（即使响应字段标为必填，旧客户端忽略多余字段）；响应新增状态码
- 删除必填约束（字段/参数由必填变可选）
- 类型放宽：`integer → number`；非空 → 可空；`date → date-time → 无 format`
- 字符串枚举 **新增** 取值；联合类型新增成员；具体类型扩展为包含它的联合

### 拒绝（破坏性变更，报告给出接口与字段定位）

| kind | 含义 |
| --- | --- |
| `endpoint_removed` | 接口被删除 |
| `parameter_removed` | 路径/查询/请求头参数被删除 |
| `response_removed` | 响应状态或响应体 schema 被删除 |
| `field_removed` | 请求体/响应字段被删除 |
| `required_added` | 新增必填参数、必填请求体字段，或可选参数/字段必填化、请求体必填化 |
| `type_narrowed` | 类型收窄或不兼容变更（如 `string → integer`、可空 → 非空、format 收窄、无约束 → 具体类型） |
| `enum_narrowed` | 枚举删除取值（同样用于联合类型删除成员） |
| `body_removed` | 原有请求体被删除 |

规则更新时在 `sdk_forge/versioning.py` 提升 `RULES_VERSION`，旧规则集保留在
`_RULESETS` 注册表中，历史判定可凭 `rules_version` 复现。

## 错误分类（HTTP 状态码 → code）

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 502 | `service_unreachable` | 连接被拒绝/超时/HTTP 错误/未暴露 `/openapi.json` |
| 502 | `contract_fetch_error` | 端点可达但内容不是合法 OpenAPI JSON |
| 422 | `malformed_contract` | 契约结构不在支持范围内（消息含精确定位） |
| 422 | `missing_contract_fragment` | `$ref` 悬空，`detail.fragment` 给出缺失片段 |
| 409 | `version_mismatch` | `expected_version` 与运行时版本不一致 |
| 409 | `incompatible_contract` | 兼容性判定不通过，`detail.breaking_changes` 列出全部破坏项 |
| 409 | `generation_in_progress` | 同一契约版本已有生成任务在执行 |

## 生成产物

```
artifacts/<contract-version>/
├── contract.json            # 归档的原始契约（兼容性基线/复现依据）
├── manifest.json            # 版本、规则版本、tag 范围、文件清单
├── sdk.zip                  # 打包好的 SDK
└── generated_sdk/
    ├── __init__.py          # Client / ApiError 导出
    ├── models.py            # dataclass 模型 + str-Enum
    ├── client.py            # 按 tag 分组的调用方法
    ├── metadata.json
    └── py.typed
```

写入采用「临时目录 + fsync + `os.replace` 原子切换」：任何失败都会清理临时目录，
磁盘上只可能存在上一版完整产物或本次完整产物，不会出现半成品。

## 本地验证方法

```bash
# 1. 安装依赖（需要 Python 3.14）
uv sync --group dev

# 2. 运行全部测试（41 个；-s 会打印契约输入与判定依据日志）
uv run pytest

# 3. 启动演示服务
uv run uvicorn main:app --reload
#    业务接口： GET/POST /items, GET/PATCH/DELETE /items/{id}
#    管理接口： /docs 下可见 /forge/*
```

### 生成 SDK（自举：默认从请求自身所在服务拉契约）

```bash
curl -s -X POST http://localhost:8000/forge/generate \
  -H 'Content-Type: application/json' -d '{}'
# 指定版本（不匹配时 409 version_mismatch；已生成过时直接复用，不访问网络）
curl -s -X POST http://localhost:8000/forge/generate \
  -H 'Content-Type: application/json' -d '{"expected_version":"1.0.0"}'
# 下载
curl -OJ http://localhost:8000/forge/artifacts/1.0.0/sdk.zip
```

### 使用生成的 SDK

```python
from generated_sdk import Client, models

client = Client("http://localhost:8000")
resp = client.list_items_items_get(tag=None)          # -> _Response(status_code, data)
created = client.create_item_items_post(
    body=models.ItemCreate(name="gadget", price=3.0)
)
# 4xx/5xx 抛 generated_sdk.client.ApiError(status_code, body)
```

### 兼容性判定与复现

```bash
# 拉取运行时契约，与 artifacts/ 中上一版归档契约比对
curl -s -X POST http://localhost:8000/forge/check-compatibility -d '{}'

# 用任意两份历史契约复现判定（也可传 rules_version 指定历史规则集）
curl -s -X POST http://localhost:8000/forge/replay -H 'Content-Type: application/json' \
  -d '{"old_contract": {...}, "new_contract": {...}}'
```

## 代码结构

| 模块 | 职责 |
| --- | --- |
| `sdk_forge/contract.py` | 契约获取（标准库 urllib）与 OpenAPI→IR 解析/严格校验 |
| `sdk_forge/models.py` | 契约 IR 与兼容性报告数据结构 |
| `sdk_forge/versioning.py` | 规则集版本化的向后兼容判定（schema/operation 级 diff） |
| `sdk_forge/generator.py` | IR → 可 import 的客户端包代码与 zip 打包（纯函数） |
| `sdk_forge/service.py` | 编排：并发去重锁、原子落盘、版本匹配、归档、历史复现 |
| `sdk_forge/errors.py` | 可区分错误类型与 HTTP 状态映射 |
| `main.py` | 演示业务服务 + `/forge/*` 管理 API |
| `tests/` | 解析/生成/兼容性/编排/API 测试（以破坏性变更与畸形契约为主要场景） |
