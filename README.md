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
3. **版本化与兼容性判定**：请求与响应两个方向分别判定，破坏性变更精确到
   「接口 → 方向 → 参数/请求体/响应状态 → 字段」；规则集按版本冻结、可回溯，
   同一份演进可一次对比多套规则版本下的结论。
4. **稳定的生成与判定过程**：服务不可达、契约版本不匹配、并发重复触发均有确定
   行为；判定按「旧契约版本 + 新契约版本 + 规则版本」幂等归档，失败不残留半成品，
   重复请求不覆盖已完成结果。
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

## 兼容性规则：请求与响应两个方向

基准视角始终是「旧客户端调用新服务」，但请求侧（客户端 → 服务端）与响应侧
（服务端 → 客户端）的语义不同，**分开判定**。每条结论都带四要素：

- 接口定位 `location`（精确到 `METHOD /path -> 参数/请求体/响应状态 -> 字段`）
- 方向 `direction`：`request` / `response` / `endpoint`
- 类别 `kind`
- 人类可读的判定依据 `detail`

### 请求方向（`request`，旧客户端向新服务发数据）

| 变化 | 结论 |
| --- | --- |
| 新增**必填**参数 / 请求体字段、参数或字段可选→必填、请求体可选→必填 | 拒绝（`required_added`） |
| 删除参数 / 请求体字段 / 原有请求体 | 拒绝（`parameter_removed` / `field_removed` / `body_removed`） |
| 类型收窄（如 `number → integer`、可空→非空、format 收窄）、枚举收窄 | 拒绝（`type_narrowed` / `enum_narrowed`） |
| 新增**可选**参数/字段、必填→可选、类型放宽、枚举新增取值 | 接受 |

### 响应方向（`response`，新服务向旧客户端回数据）

| 变化 | 结论 |
| --- | --- |
| 字段**必填 → 可选**：服务端可能不再返回该字段，旧客户端会读到缺失数据 | **拒绝（`required_removed`，自 `2026.10.01` 起）** |
| 字段**可选 → 必填**：服务端给出更强保证 | **接受（`required_strengthened`）** |
| 删除响应字段、删除响应状态、删除响应体 schema | 拒绝（`field_removed` / `response_removed`） |
| 响应类型收窄、枚举收窄 | 拒绝（`type_narrowed` / `enum_narrowed`） |
| 新增响应字段（即使标必填，旧客户端可安全忽略多余字段）、新增响应状态 | 接受 |
| 类型放宽、枚举新增取值、可空化 | 接受 |

接口级：删除接口拒绝（`endpoint_removed`，`direction=endpoint`）；新增接口接受。

> 同一处「必填 ⇄ 可选」变化在两个方向结论**相反**：请求侧必填→可选是放宽（接受），
> 响应侧必填→可选是破坏（拒绝）；请求侧可选→必填是破坏，响应侧可选→必填是接受。

## 规则集版本：冻结、回溯与跨版本对比

规则集按版本发布，**旧版本一经发布即完整冻结、不得就地修改**；注册表见
`sdk_forge/versioning.py` 的 `_RULESETS`：

| 规则版本 | 与上一版的实际差异 |
| --- | --- |
| `2026.09.01`（冻结） | 响应字段「必填→可选」当时按放宽接受 |
| `2026.10.01`（当前，`RULES_VERSION`） | 同样的变化改判为破坏（`required_removed`）；请求/响应方向分离 |

- **复现**：`check_with_rules(old, new, "2026.09.01")` 用旧规则重跑，
  `check_with_rules(old, new, "2026.10.01")` 用新规则重跑。复现不盖时间戳，
  相同输入的结论、定位、类别、依据逐字节一致，不受执行时间与执行顺序影响
  （结论在产出前按 `location/kind/detail/direction` 稳定排序）。
- **跨版本对比**：`compare_across_rules(old, new)`（或 HTTP
  `/forge/compare-rules`）一次返回同一份演进在所有冻结规则版本下的结论，
  便于规则升级前评估影响面。
- **未知版本**：传入未注册的规则版本抛 `UnknownRulesVersionError`
  （`code=unknown_rules_version`、HTTP 400，同时是 `ValueError` 子类），
  **绝不静默按当前规则处理**，错误里带可区分的版本号与可用版本清单。

### 变更类别（kind）速查

| kind | 含义 |
| --- | --- |
| `endpoint_removed` | 接口被删除 |
| `parameter_removed` | 路径/查询/请求头参数被删除 |
| `response_removed` | 响应状态或响应体 schema 被删除 |
| `field_removed` | 请求体/响应字段被删除 |
| `required_added` | 请求方向：新增必填参数/字段，或可选变必填、请求体必填化 |
| `required_removed` | 响应方向：字段必填变可选（服务端可能不再返回） |
| `type_narrowed` | 类型收窄或不兼容变更（如 `string → integer`、可空 → 非空、format 收窄、无约束 → 具体类型） |
| `enum_narrowed` | 枚举删除取值（同样用于联合类型删除成员） |
| `body_removed` | 原有请求体被删除 |

规则更新时在 `sdk_forge/versioning.py` 提升 `RULES_VERSION` 并在 `_RULESETS`
注册新的 `_RuleSet`（差异通过构造参数显式表达），旧规则集原样保留。

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
| 409 | `judgement_in_progress` | 同一对契约 + 同一规则版本的判定正在执行（可改用 `wait=true` 等待复用） |
| 400 | `unknown_rules_version` | 传入了未冻结的规则版本（绝不静默回退到当前规则） |

## 生成产物

```
artifacts/
├── <contract-version>/
│   ├── contract.json            # 归档的原始契约（兼容性基线/复现依据）
│   ├── manifest.json            # 版本、规则版本、tag 范围、文件清单
│   ├── sdk.zip                  # 打包好的 SDK
│   ├── checks/<rules-version>/compatibility.json  # 判定报告（按版本镜像）
│   └── generated_sdk/
│       ├── __init__.py          # Client / ApiError 导出
│       ├── models.py            # dataclass 模型 + str-Enum
│       ├── client.py            # 按 tag 分组的调用方法
│       ├── metadata.json
│       └── py.typed
└── _judgements/                 # 判定归档（集中存放，查询/列举入口）
    └── <old>__<new>__<rules>/   # key = 旧版本__新版本__规则版本
        ├── meta.json            # old/new/rules 版本、compatible
        ├── report.json          # 完整结论（与 replay 逐字节一致，无时间戳）
        ├── old_contract.json    # 旧契约快照（基线判定时缺省）
        └── new_contract.json    # 新契约快照
```

写入采用「临时目录 + fsync + `os.replace` 原子切换」：任何失败都会清理临时目录，
磁盘上只可能存在上一版完整产物或本次完整产物，不会出现半成品。

**判定幂等与并发**：判定按「旧契约版本 + 新契约版本 + 规则版本」唯一归档。
重复触发直接返回已完成报告（不重复计算、不覆盖）；并发触发同一组合时，默认得到
`409 judgement_in_progress`，传 `wait=true` 则等待并复用正在进行的同一份结论
（worker 失败时等待方收到同一失败，且不留下半成品）。

## 本地验证方法

```bash
# 1. 安装依赖（需要 Python 3.14）
uv sync --group dev

# 2. 运行全部测试（-s 会打印契约输入与判定依据日志）
uv run pytest
#    重点：tests/test_versioning.py（方向语义、规则冻结/复现/对比）
#          tests/test_service.py（判定幂等、并发去重、归档查询）

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

### 兼容性判定、归档、复现与跨规则对比

```bash
# 拉取运行时契约，与 artifacts/ 中上一版归档契约比对（结论同步归档）
curl -s -X POST http://localhost:8000/forge/check-compatibility -d '{}'

# 直接对一对契约做判定并归档（幂等；并发可加 "wait": true 等待复用）
curl -s -X POST http://localhost:8000/forge/judge -H 'Content-Type: application/json' \
  -d '{"old_contract": {...}, "new_contract": {...}, "rules_version": "2026.10.01"}'

# 列出已归档判定、按契约版本+规则版本查回
curl -s http://localhost:8000/forge/judgements

# 用任意两份历史契约复现判定（不落盘）；可指定任意已冻结 rules_version
curl -s -X POST http://localhost:8000/forge/replay -H 'Content-Type: application/json' \
  -d '{"old_contract": {...}, "new_contract": {...}, "rules_version": "2026.09.01"}'

# 同一份演进一次对比全部冻结规则版本（规则升级前评估影响面）
curl -s -X POST http://localhost:8000/forge/compare-rules \
  -H 'Content-Type: application/json' \
  -d '{"old_contract": {...}, "new_contract": {...}}'
# -> {"2026.09.01": {compatible: true, ...}, "2026.10.01": {compatible: false, ...}}
```

Python 侧等价入口（`sdk_forge.service` / `sdk_forge.versioning`）：

```python
from sdk_forge.contract import parse_contract
from sdk_forge.versioning import (
    check_with_rules, compare_across_rules,
    RULES_VERSION_2026_09, RULES_VERSION,
)
from sdk_forge.service import ForgeService

svc = ForgeService()
report = svc.judge(old_contract=old, new_contract=new, output_dir="artifacts")
same   = svc.get_judgement(output_dir="artifacts", old_version="1.0.0",
                           new_version="2.0.0", rules_version=RULES_VERSION)
rows   = svc.list_judgements("artifacts")
across = compare_across_rules(parse_contract(old), parse_contract(new))
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
