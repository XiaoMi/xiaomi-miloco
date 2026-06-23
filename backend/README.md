# Miloco Backend

Xiaomi Miloco 面向未来的全屋智能 AI 感知与控制后端服务。

## 项目结构

[uv workspace](https://docs.astral.sh/uv/concepts/workspaces/) 布局，两个成员包：

```
backend/
├── pyproject.toml           # workspace 根（taskipy / 开发依赖 / ruff 配置）
├── uv.lock                  # 统一锁文件
├── miloco/                  # 主应用（FastAPI + 感知引擎 + 规则 + MIoT 网关）
│   ├── pyproject.toml
│   └── src/miloco/
│       ├── main.py          # FastAPI 入口（miloco-backend 脚本）
│       ├── manager.py       # 服务 Manager 单例
│       ├── config/          # settings.yaml + 加载器
│       ├── database/        # SQLite 仓储层（kv_repo / person_repo / ...）
│       ├── middleware/      # 认证中间件
│       ├── miot/            # 小米 MIoT 设备/账号服务
│       ├── perception/      # 感知引擎（采集 + gate + identity + omni）
│       ├── person/          # 人员管理服务
│       ├── rule/            # 规则引擎
│       ├── admin/           # 系统管理 API
│       ├── schema/          # 共享 Pydantic schema
│       ├── utils/           # bootstrap / uvicorn / logger / cert 等工具
│       └── static/          # 内置静态资源（感知 dashboard 等）
└── miot/                    # 小米 IoT SDK（独立子包）
    ├── pyproject.toml
    └── src/miot/
```

## 环境要求

- Python >= 3.11
- [uv](https://docs.astral.sh/uv/) 最新版

## 安装

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # 安装 uv（已装可跳过）
uv sync --all-packages                            # 安装所有 workspace 包
```

## 开发

```bash
uv run task dev      # 前台启动（日志直接到终端，不写文件）
uv run task test     # pytest
uv run task lint     # ruff 检查 + 格式化
uv run task check    # ty 类型检查
uv run task reset    # 全量重装依赖
```

> 正常使用建议通过 `miloco-cli service start` 以 daemon 模式启动（会写 `~/.openclaw/miloco/log/miloco-backend_<ts>.log`）。

## 配置

三端共享配置文件：`$MILOCO_HOME/config.json`（默认 `~/.openclaw/miloco/config.json`），通过 CLI 管理：

```bash
miloco-cli config set model.omni.api_key sk-xxxxx   # 设置 LLM API Key
miloco-cli config show                               # 查看合并后配置
```

环境变量同构覆盖：`MILOCO_MODEL__OMNI__API_KEY`、`MILOCO_SERVER__URL` 等，详见开发指南。

`miloco/src/miloco/config/settings.yaml` 仅作为后端默认值。

覆盖 workspace 根目录用环境变量：`MILOCO_HOME`（默认 `~/.openclaw/miloco/`）。

## 入口点

| 命令            | 模块                       | 说明              |
| --------------- | -------------------------- | ----------------- |
| `miloco-backend` | `miloco.main:start_server` | 启动 FastAPI 服务 |

## 常用链接

- 健康检查：http://127.0.0.1:1810/health
