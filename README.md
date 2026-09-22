# OpenAPI SDK Forge (Python)

由**服务端运行时接口契约（OpenAPI）驱动**的客户端 SDK 生成与版本兼容性校验工具链。
同一份契约可以稳定地生成可直接调用的 Python 客户端与请求/响应模型；契约演进时，
按**版本化的规则集**给出可复现、可归因的兼容性判定。

- 服务框架：FastAPI（仅用于演示服务）
- 生成产物：**纯标准库**（`urllib` + `dataclasses`），不引入第三方依赖
- 校验规则：规则集冻结 + 版本号 + 逐条规则 id，历史判定可按版本复现

---

## 目录结构

| 文件 | 职责 |
| --- | --- |
| `main.py` | ASGI 入口（`uvicorn main:app`） |
| `sdk_forge/contract.py` | 契约获取（HTTP）、解析、规范化；畸形片段给出带路径的可区分错误 |
| `sdk_forge/compat.py` | 规则版本化的向后兼容判定（冻结的规则注册表） |
| `sdk_forge/generator.py` | 由契约生成客户端 SDK（模型 + client + 元数据） |
| `sdk_forge/storage.py` | 原子发布、内容寻址、并发锁、判定历史（JSONL） |
| `sdk_forge/service.py` | demo 资源服务 + 挂载在 `/forge` 的管理 API |
| `tests/` | 以破坏性契约变更与畸形契约为主的测试 |

---

## 支持的契约范围

- **文档**：OpenAPI **3.0.x / 3.1.x**，JSON；从 `<base_url>/openapi.json` 获取。
- **操作**：`paths` 下的 HTTP 操作；必须带非空且唯一的 `operationId`。
- **参数**：`path`（必填）与 `query`（可选/必填）参数及其 schema；
  不支持 header/cookie 参数与 path-item 级 `parameters`（显式报错，不跳过）。
- **请求体**：单个 `application/json` body，schema 必填。
- **响应**：按状态码键控的一个或多个响应；JSON 响应必须有 schema；
  `default` 与任意 2xx/4xx 状态码均支持。
- **Schema 类型**：`object`（`properties` + `required`）、`array`（`items`）、
  `string` / `integer` / `number` / `boolean`、`enum`、`format`（保留不强制）。
- **可空**：3.0 的 `nullable: true`，以及 3.1 / Pydantic v2 的
  `{"anyOf": [{"type": T}, {"type": "null"}]}` 与 `type: [T, "null"]`。
- **引用**：仅支持 `#/components/schemas/<Name>`；悬空引用、循环引用、
  无法解析/不支持的片段一律抛出 `malformed_contract`，**绝不静默跳过**。

### 错误分类（`error` 机器码）

| code | HTTP | 含义 |
| --- | --- | --- |
| `service_unreachable` | 502 | 连接不上/超时（连契约端点都不可达） |
| `contract_fetch_failed` | 502 | 端点可达但 HTTP 非 2xx 或返回非 JSON |
| `malformed_contract` | 422 | 契约缺失/无法解析的片段，`details.path` 指明位置 |
| `version_mismatch` | 409 | 请求的版本与服务实际暴露的版本不一致 |
| `generation_in_progress` | 409 | 相同指纹的生成正在并发进行 |
| `artifact_write_failed` | 500 | 产物落盘失败 |

---

## 生成产物

对一份契约生成一个可导入的包，包含：

```
<packagename>/
  __init__.py     # 导出 Client / ApiError / 全部模型
  models.py       # 每个 object schema 一个 dataclass，含 from_dict；enum 常量
  client.py       # 每个操作一个方法；路径/查询参数、JSON 请求体、
                  # 2xx 自动解析为模型，非 2xx 抛出 ApiError(status_code, body)
  codegen.json    # 契约标题/版本/指纹/openapi 版本/操作与模型清单
```

- 方法名由 HTTP 方法 + 路径推导（如 `GET /items/{id}` → `get_item`，
  `POST /items` → `create_item`）。
- 目录按 `<版本>-<契约指纹前12位>` **内容寻址**；同契约重复生成直接返回已有产物。
- 可选请求字段为 `None` 时不出现在请求 JSON 中（不会把 `null` 强发给服务端）。

---

## 兼容性规则

判定分别针对**请求方向**（client → server）与**响应方向**（server → client），
每条结论都带 `rule_id`、`severity`、`location`（精确到 操作 / 参数 / 响应码 / 字段）。

### 接受（非破坏）

- 新增操作、新增已记录的响应状态码；
- 新增**可选**请求字段、可选 query 参数；
- 响应新增字段；响应字段由可选变必填、由可空变非空（服务承诺增强）；
- 类型放宽 `integer → number`；
- 枚举**新增**取值（当前规则集）；
- 请求字段允许 `null`（请求方向的类型放宽）。

### 拒绝（破坏性，必须明确归因）

- 删除操作；
- 删除任意字段或参数；
- 请求参数/字段**必填化**；
- 响应字段由必填变可选、由非空变可空（服务承诺被削弱）；
- 类型收窄（`number → integer`）或其他不支持的类型变更（如 `string → boolean`）；
- 枚举**删除**取值（收窄）；
- 删除已记录的响应状态码。

### 规则版本与历史可复现

规则集在 `sdk_forge/compat.py` 的 `RULE_SETS` 中冻结，按版本号寻址：

- `rules-2026.09`：`R105-enum-changed` —— 枚举值集合**任何**差异都判破坏（严格版）；
- `rules-2026.12`（当前默认）：`R205-enum-narrowed/widened` ——
  方向感知，仅删除枚举值判破坏，新增取值接受。

每份报告记录 `rule_version`、新旧契约指纹与全部触发的规则 id。
**规则更新后，用旧版本号重新判定，仍得到与历史完全一致的结论**（见测试
`test_enum_evolution_verdict_reproduces_per_rule_version`）。判定历史以
append-only JSONL 存于 `reports/<target>.jsonl`（文件锁串行化，行不会交错）。

---

## 并发与失败语义

- 相同 `(target, fingerprint)` 的并发生成：持锁者生成，后来者立即收到
  `409 generation_in_progress`（非阻塞 `flock`，进程崩溃时由内核自动释放）。
- 生成先写 `staging/...tmp.<pid>`，逐文件 `.part` 临时写入 + `os.replace`，
  成功后整目录原子 rename 到 `artifacts/`；任何失败都清理自己的 staging，
  且下一次尝试前会清扫崩溃残留——**不残留半成品**。
- 已完成产物内容寻址、永不被覆盖；不同指纹的生成互不阻塞。

---

## 本地验证（完整可复现）

要求 Python 3.14+，使用仓库自带的 `uv` 环境：

```bash
uv sync                                  # 安装依赖（fastapi + pytest）
uv run pytest                            # 全部 63 个测试
uv run pytest --log-cli-level=INFO       # 同时打印契约输入与逐条判定依据
```

启动服务并手工走一遍流程：

```bash
uv run uvicorn main:app --port 8000 &

# 1) 从运行时契约生成 SDK
curl -s -X POST localhost:8000/forge/generate \
  -H 'content-type: application/json' \
  -d '{"base_url":"http://127.0.0.1:8000"}'

# 2) 下载生成产物（用上一步返回的 version/fingerprint）
curl -s localhost:8000/forge/artifacts/default/1.0.0/<fingerprint>/client.py

# 3) 版本不匹配（可预期的 409 分类）
curl -s -X POST localhost:8000/forge/generate \
  -H 'content-type: application/json' \
  -d '{"base_url":"http://127.0.0.1:8000","version":"9.9.9"}'

# 4) 服务不可达（可预期的 502 分类）
curl -s -X POST localhost:8000/forge/generate \
  -H 'content-type: application/json' \
  -d '{"base_url":"http://127.0.0.1:1"}'

# 5) 兼容性判定（两个运行中的服务地址；可指定 rule_version）
curl -s -X POST localhost:8000/forge/compat \
  -H 'content-type: application/json' \
  -d '{"old_base_url":"http://127.0.0.1:8000",
       "new_base_url":"http://127.0.0.1:8001",
       "rule_version":"rules-2026.12"}'

# 6) 历史判定
curl -s localhost:8000/forge/reports/default
```

产物与报告的落盘根目录可用环境变量 `SDK_FORGE_STORE` 指定（默认 `./.forge-store`）。

### 测试重点场景

- 畸形契约：不支持的 openapi 版本、缺失 info/paths/operationId、
  悬空/循环 `$ref`、缺 `items`/`properties`、未知类型、非 JSON media type、
  未被任何操作引用但本身畸形的组件模型 —— 全部按 `details.path` 精确归因。
- 破坏性演进：删字段、必填化、类型收窄、删枚举值、删操作、删响应码、
  响应承诺削弱 —— 判定为不兼容并指出接口与字段。
- 稳定性：并发重复触发（一个 201、一个 409）、失败发布无残留、报告并发追加不交错。
- 端到端：真实 uvicorn 服务上，**生成出来的 SDK 实际发起 HTTP 调用**，
  覆盖路径参数、查询参数、请求体、200/201/404 多种响应状态。
