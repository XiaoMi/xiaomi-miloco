# Miloco Hermes Plugin — Integration Test

使用 Docker Compose 验证 Miloco Hermes 插件的加载和 bridge 连通性。

## 前置条件

- Docker + Docker Compose
- 复制 `.env.example` 为 `.env` 并填入模型 API Key（可留空，连通性测试不需要 LLM）

## 运行

```bash
cd plugins/hermes/integration-test
cp .env.example .env  # 按需填入 API key

docker compose up -d --build
python3 run_test.py
```

## 测试项

| 检查 | 说明 |
|------|------|
| Hermes gateway 健康 | `GET :8642/health` 返回 200 |
| Miloco backend 健康 | `GET :1810/health` 返回 200 |
| 插件加载 | `hermes plugins list` 显示 miloco 为 enabled |
| Bridge 契约 | POST `:18789/miloco/webhook` 发送 `get_trace` action，验证返回 `{ code:0, data:{ status:"unknown" } }` |

## 清理

```bash
docker compose down -v
```
