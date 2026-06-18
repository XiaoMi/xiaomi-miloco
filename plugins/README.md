# miloco 插件目录

每个子目录是一个 **agent runtime 适配**：把 miloco 的 16 个共享 skill（`../skills/miloco-*`）接到对应 agent 上，并提供入站 webhook 适配层（如果该 agent 不能直接注册 HTTP 路由）。

## 当前支持的 runtime

| 子目录 | agent | 语言 | 状态 |
|---|---|---|---|
| [`openclaw/`](openclaw/) | [OpenClaw](https://openclaw.ai) | TypeScript | **默认**，跟 miloco 主仓一起发布 |
| [`hermes/`](hermes/) | [Hermes Agent](https://github.com/NousResearch/hermes-agent)（开源 MIT，Python） | Python | 第二 runtime，本目录新增 |

## 给后续第三个 runtime 的最小骨架

加新 runtime 时，按以下结构和命名约定走，便于用户发现、agent 安装脚本抓取、维护者一眼看明白：

```
plugins/<runtime>/
├── README.md                    # 6 段对齐 openclaw：Install / What It Does / Configuration / Development / License
├── install-<runtime>.sh         # 一键安装（patch miloco config / agent env / 启 adapter，幂等）
├── <runtime>-plugin/            # 该 agent 侧的插件（出站）
│   └── （hook / tool / skill 装载逻辑）
├── adapter/                     # 入站 webhook 适配进程（如 agent 不能注册 HTTP 路由才需要）
│   └── __main__.py
├── scripts/
│   ├── install.sh               # 高级/手动安装
│   └── <runtime>-adapter.sh     # adapter 生命周期（start/stop/restart/status/logs）
├── skills/                      # sync-skills.py 生成的产物，gitignore
└── tests/                       # 至少：pytest 单元 + bash e2e（装+adapter 全生命周期）
```

**agent 安装脚本（给 AI agent 跑）放哪**：

- 放 `scripts/install-guide-<runtime>.md`（与 OpenClaw 的 `scripts/install-guide.md` 同级）
- 模板见 [scripts/install-guide-hermes.md](../scripts/install-guide-hermes.md)
- 主 README 方式一里加 `<runtime>` 子段，URL 用 `https://raw.githubusercontent.com/XiaoMi/xiaomi-miloco/main/scripts/install-guide-<runtime>.md`

**skill 源复用**：

- **不要**在 `<runtime>/skills/` 里维护 skill，所有 skill 源在 [`../skills/miloco-*`](../skills/)
- 用 `<runtime>/scripts/sync-skills.py` 从共享源生成并适配 frontmatter（删 agent-specific 字段、加 date 引号等）
- OpenClaw 版做法见 [`openclaw/scripts/`](../openclaw/scripts/)，Hermes 版做法见 [`hermes/scripts/sync-skills.py`](hermes/scripts/sync-skills.py)

**PR 提交规范**：

- 主 README（英+中）方式一加 `<runtime>` 子段
- `knowledge/03-features/<runtime>-integration.md` 写架构 + 跟 OpenClaw 差异
- `knowledge/05-external-deps/sdk-<runtime>.md` 写 agent 平台契约
- `.gitignore` 加 `plugins/<runtime>/skills/`
- 致谢加该 agent 项目
- 标题建议：`<runtime> 兼容层（统一 plugins/<runtime>/ 规范）`

## 命名约定

| 资源 | 命名 |
|---|---|
| 子目录 | `plugins/<runtime>/`（小写 agent 名） |
| 插件名（agent 侧） | `miloco`（与 OpenClaw 版同名，agent 看到的是同一插件） |
| 一键安装脚本 | `plugins/<runtime>/install-<runtime>.sh` |
| Adapter 生命周期脚本 | `plugins/<runtime>/scripts/<runtime>-adapter.sh` |
| 适配层包名 | `plugins.<runtime>.adapter`（可作为 Python `python -m` 入口） |
| agent 安装 skill | `scripts/install-guide-<runtime>.md` |
| 知识库条目 | `knowledge/03-features/<runtime>-integration.md` / `knowledge/05-external-deps/sdk-<runtime>.md` |
