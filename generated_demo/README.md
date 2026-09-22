# generated_demo

由 `POST /forge/generate` 针对本仓库 `main.py`（契约版本 1.0.0）生成的 SDK 样例，
仅作展示；运行时产物默认写入被 gitignore 的 `artifacts/` 目录。

重新生成：

```bash
uv run uvicorn main:app --port 8000
curl -X POST localhost:8000/forge/generate \
  -H 'Content-Type: application/json' -d '{"package_name":"shop_sdk"}'
```

`contract.v1.0.0.json` 是对应的归档契约，可用于 `/forge/replay` 复现历史判定。
