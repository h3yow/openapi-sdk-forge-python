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
3. **版本化与兼容性判定**：请求方向与响应方向使用**各自的兼容语义**判定，
   破坏性变更精确到「接口 → 方向（request/response）→ 参数/请求体/响应状态 →
   字段」，每条结论都带 `kind` / `direction` / 定位 / 判定依据。
4. **规则集冻结与回溯**：至少保留两套有实际差异的规则版本；任意历史结论都
   能凭契约对 + 规则版本字节级复现，也能一次对比同一演进在不同规则版本下的
   结论（规则升级前评估影响面）；未知规则版本明确报错，不静默回退。
5. **稳定的生成与判定过程**：服务不可达、契约版本不匹配、并发重复触发均有
   确定行为；失败不残留半成品，重复请求不覆盖已完成结果。判定按
   `新版本/checks/规则版本/旧版本.json` 归档，可查回、可列举。
6. **历史可复现**：归档原始契约 + 规则版本号；任意时刻可用
   `/forge/replay` 或 `/forge/compare-rules` 复现/对比历史判定，相同输入
   得到字节级一致的结论（无时间戳、与执行顺序无关）。

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

## 兼容性规则（按数据方向分别判定）

以「旧客户端调用新服务」为基准，**请求方向**（客户端 → 服务端：参数、请求体）
与**响应方向**（服务端 → 客户端：响应状态、响应字段）语义不同，每条结论都带
`direction`（`request` / `response`，接口删除为 `endpoint`）。

### 请求方向（`direction=request`）

接受：新增可选参数/字段；参数或字段**必填 → 可选**（客户端不再被强制）；
请求体本身必填 → 可选；类型放宽；枚举新增取值。

拒绝（破坏）：

| kind | 含义 |
| --- | --- |
| `parameter_removed` | 路径/查询/请求头参数被删除 |
| `field_removed` / `body_removed` | 请求体字段或请求体被删除 |
| `required_added` | 新增必填参数/必填字段（请求体任意嵌套深度），或可选参数/字段必填化、请求体必填化 |
| `type_narrowed` / `enum_narrowed` | 请求类型或枚举收窄 |

### 响应方向（`direction=response`）

接受：新增响应字段（含必填，旧客户端忽略多余字段）；字段**可选 → 必填**
（服务端给出更强保证）；新增响应状态；类型放宽；枚举新增取值。

拒绝（破坏）：

| kind | 含义 |
| --- | --- |
| `response_removed` | 响应状态或响应体 schema 被删除 |
| `field_removed` | 响应字段被删除 |
| `field_became_optional` | 响应字段**必填 → 可选**：服务端可能不再返回，老客户端会读到缺失数据 |
| `type_narrowed` / `enum_narrowed` | 响应类型或枚举收窄 |

> 典型对比：同一个「必填 ↔ 可选」翻转，在请求方向必填→可选是**放宽**，
> 在响应方向必填→可选是**破坏**；可选→必填则两方向结论正好相反。

### 规则集版本：冻结与回溯

| 规则版本 | 状态 | 与上一版的实际差异 |
| --- | --- | --- |
| `2026.09.01` | 已冻结（`_FrozenRulesV1`，永不改动） | 方向不敏感：字段必填→可选一律接受；新增必填字段仅在**请求体顶层**判破坏 |
| `2026.09.30` | 当前（`RULES_VERSION`） | 请求/响应分方向判定；请求体任意深度新增必填字段都破坏；响应必填→可选破坏（`field_became_optional`），可选→必填接受 |

* 升级规则时新增一个版本并登记到 `sdk_forge/versioning.py` 的 `RULESETS`，
  旧版本实现原样保留；`available_rules_versions()` 列出全部版本。
* `check_with_rules(old, new, version)` / `service.replay(...)` 用指定版本
  复现：归档/复现的报告**不盖时间戳**，结论经稳定排序，相同输入 + 规则版本
  永远逐字节一致，与执行顺序无关。
* `compare_across_rules(old, new)`（HTTP：`/forge/compare-rules`）一次返回
  同一演进在各规则版本下的结论，用于规则升级前评估影响面：旧规则接受而新
  规则拒绝的项，就是升级后会新增拦截的演进。
* 传入未注册版本抛 `UnknownRulesVersionError`（HTTP 404，
  `code=unknown_rules_version`，响应中带 `available` 列表），绝不静默按当前
  规则处理。

### 判定归档与触发稳定性

`ForgeService.run_check(...)`（HTTP：`POST /forge/checks`）对同一组
（旧契约版本, 新契约版本, 规则版本）：

* 已有归档 → 直接取回，**不重复计算、不覆盖**；
* 正在计算 → 第二个并发请求得到 `CheckInProgressError`（HTTP 409，
  `code=check_in_progress`），完成后再触发则复用结果；
* 归档走「临时目录 + fsync + `os.replace`」原子切换，过程中不留半成品，
  两个请求不会互相写坏或串结果。

归档布局：

```
artifacts/<新契约版本>/checks/<规则版本>/<旧契约版本>.json
```

`get_check(...)`（`GET /forge/checks/{new}/{rules}/{old}`）按版本对取回，
`list_checks(...)`（`GET /forge/checks`）列出全部判定记录。

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
| 409 | `check_in_progress` | 同一组（旧版本, 新版本, 规则版本）判定正在执行 |
| 404 | `unknown_rules_version` | 规则版本未注册，`detail.available` 给出可用版本，不静默回退 |

## 生成产物

```
artifacts/<contract-version>/
├── contract.json            # 归档的原始契约（兼容性基线/复现依据）
├── manifest.json            # 版本、规则版本、tag 范围、文件清单
├── sdk.zip                  # 打包好的 SDK
├── generated_sdk/
│   ├── __init__.py          # Client / ApiError 导出
│   ├── models.py            # dataclass 模型 + str-Enum
│   ├── client.py            # 按 tag 分组的调用方法
│   ├── metadata.json
│   └── py.typed
└── checks/
    └── <规则版本>/
        └── <旧契约版本>.json  # 归档判定（无时间戳，可逐字节复现）
```

写入采用「临时目录 + fsync + `os.replace` 原子切换」：任何失败都会清理临时目录，
磁盘上只可能存在上一版完整产物或本次完整产物，不会出现半成品。

## 本地验证方法

```bash
# 1. 安装依赖（需要 Python 3.14）
uv sync --group dev

# 2. 运行全部测试（67 个；-s 会打印契约输入与判定依据日志）
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

### 兼容性判定、跨版本复现与对比

```bash
# 拉取运行时契约，与 artifacts/ 中上一版归档契约比对（可用 rules_version 指定规则）
curl -s -X POST http://localhost:8000/forge/check-compatibility -d '{}'

# 用任意两份历史契约复现判定（不落盘；传 rules_version 指定历史冻结规则集）
curl -s -X POST http://localhost:8000/forge/replay -H 'Content-Type: application/json' \
  -d '{"old_contract": {...}, "new_contract": {...}, "rules_version": "2026.09.01"}'

# 一次对比同一演进在全部（或指定）规则版本下分别判成什么
curl -s -X POST http://localhost:8000/forge/compare-rules -H 'Content-Type: application/json' \
  -d '{"old_contract": {...}, "new_contract": {...}}'

# 查看可用规则版本；执行判定并归档（重复触发复用、不覆盖）
curl -s http://localhost:8000/forge/rules-versions
curl -s -X POST http://localhost:8000/forge/checks -H 'Content-Type: application/json' \
  -d '{"old_contract": {...}, "new_contract": {...}, "rules_version": "2026.09.30"}'
# 列出现有判定；按 新版本/规则版本/旧版本 取回单条
curl -s http://localhost:8000/forge/checks
curl -s http://localhost:8000/forge/checks/2.0.0/2026.09.30/1.0.0
```

也可以直接在 Python 中使用：

```python
from sdk_forge.contract import parse_contract
from sdk_forge.versioning import (
    RULES_VERSION, check_with_rules, compare_across_rules,
    available_rules_versions,
)

old_ir, new_ir = parse_contract(old_spec), parse_contract(new_spec)
# 用冻结的旧规则复现
check_with_rules(old_ir, new_ir, "2026.09.01")
# 跨版本对比：{version: CompatibilityReport}，升级规则前看影响面
for version, report in compare_across_rules(old_ir, new_ir).items():
    print(version, report.compatible,
          [(c.kind, c.direction, c.location) for c in report.breaking_changes])
```

## 代码结构

| 模块 | 职责 |
| --- | --- |
| `sdk_forge/contract.py` | 契约获取（标准库 urllib）与 OpenAPI→IR 解析/严格校验 |
| `sdk_forge/models.py` | 契约 IR 与兼容性报告数据结构 |
| `sdk_forge/versioning.py` | 规则集版本化、请求/响应分方向的兼容判定、冻结规则与跨版本对比 |
| `sdk_forge/generator.py` | IR → 可 import 的客户端包代码与 zip 打包（纯函数） |
| `sdk_forge/service.py` | 编排：并发去重锁、原子落盘、版本匹配、判定归档/查询、历史复现 |
| `sdk_forge/errors.py` | 可区分错误类型与 HTTP 状态映射 |
| `main.py` | 演示业务服务 + `/forge/*` 管理 API |
| `tests/` | 解析/生成/兼容性/编排/API 测试（以破坏性变更与畸形契约为主要场景） |
