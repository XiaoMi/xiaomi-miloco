# 系统架构总览

## 项目定位

Miloco 是小米面向未来的全屋智能 AI 开源方案。运行在家庭本地设备（NUC、树莓派、Mac Mini 等）上，通过 MiOT 协议管理米家设备、持续感知家庭环境、驱动自动化规则，并通过 OpenClaw 插件将 AI Agent 接入整套能力闭环。

用户只需告诉 AI"把书房台灯调暗，爷爷在看书"——Miloco 识别谁在哪里、联动灯光、并将这条信息沉淀为家庭记忆。

### 核心能力域

- **设备控制** — 通过 MiOT 协议操控米家设备：开关、调参、查状态、触发场景
- **环境感知** — 摄像头四层流水线（MultimodalCollector → Gate → Identity → Omni），将画面转化为结构化事件
- **身份识别** — 识别家庭成员和陌生人，让感知结果从"有人"升级为"谁在场"
- **自动化规则** — 用自然语言描述"当 X 时做 Y"，VLM 负责语义判断
- **家庭记忆** — 从感知与对话中沉淀长期知识，注入每次 Agent 对话的 system prompt
- **任务管理** — 创建持久意图（任务装配 rule / cron / record），带行为统计与周期归档
- **AI Agent 集成** — OpenClaw 插件 + `miloco-*` Skill 套件，双向与后端通信
- **实时摄像头观看** — 浏览器无插件直播，与感知流水线共享解码
- **设备欢迎** — 新设备绑定后主动通知用户，建立"Miloco 已接管该设备"的信任感
- **事件反馈** — 用户指认感知事件出错，把该事件的 omni 复现数据脱敏打包到本地，供坏例回收与模型迭代

---

## 代码库结构

Python + TypeScript 混合仓库，uv workspace + pnpm 管理。

```
miloco/
├── backend/
│   ├── miloco/src/miloco/         # Server 主应用（FastAPI）
│   │   ├── main.py                # 应用入口、lifespan、路由注册、SPA handler
│   │   ├── manager.py             # 依赖注入单例 Manager
│   │   ├── config/settings.py     # 统一配置（pydantic-settings）
│   │   ├── miot/                  # 设备控制域
│   │   ├── perception/            # 感知域（含 engine/ 子包）
│   │   ├── rule/                  # 规则引擎域
│   │   ├── person/                # 身份域
│   │   ├── task/                  # 任务域（生命周期主体）
│   │   ├── task_record/           # 任务记录域（行为统计 + 周期归档）
│   │   ├── home_profile/          # 家庭记忆域
│   │   ├── dispatch/              # Agent 事件调度
│   │   ├── observability/         # 性能追踪与指标
│   │   ├── admin/                 # 管理接口（含 token 用量查询）
│   │   ├── node_monitor/          # 节点生命周期监控
│   │   ├── database/              # Repo 层（SQLite）
│   │   └── middleware/            # 鉴权与异常处理
│   └── miot/src/miot/             # MiOT SDK 子包（miloco-miot 独立包，Python 源码在仓内，仅摄像头 C 库预编译）
├── cli/src/miloco_cli/            # Click CLI（miloco-cli 命令）
├── plugins/
│   ├── openclaw/src/              # TypeScript OpenClaw 插件
│   └── skills/                    # Skill 套件（miloco-* 前缀）
├── web/                           # 家庭面板前端（React + Vite + Tailwind）
├── knowledge/                     # 本知识库
└── xaf/                           # XAF（git submodule）
```

---

## Server 五层架构

后端遵循 Router → Service/Runner → Repo/外部代理 分层，各域 router 以各自 prefix 挂载，由 `main.py` 统一收在 `/api` 下。

| 层           | 职责                               | 典型类（文件路径）                                                                                                                                                                                                                                                                                     |
| ------------ | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Router**   | 接收 HTTP 请求、参数校验、鉴权前置 | `miot/router.py`、`perception/router.py`、`rule/router.py`、`person/router.py`、`home_profile/router.py`、`task/router.py`、`task_record/router.py`、`admin/router.py`、`observability/router.py`、`node_monitor/router.py`                                                                            |
| **Service**  | 业务编排，跨域协调                 | `MiotService`（`miot/service.py`）、`PerceptionService`（`perception/service.py`）、`RuleService`（`rule/service.py`）、`PersonService`（`person/service.py`）、`HomeProfileService`（`home_profile/service.py`）、`TaskService`（`task/service.py`）、`TaskRecordService`（`task_record/service.py`） |
| **Runner**   | 异步后台循环，驱动持续任务         | `PerceptionRunner`（`perception/runner.py`）、`RuleRunner`（`rule/runner.py`）、`TerminateEvaluator`（`rule/terminate_evaluator.py`）                                                                                                                                                                  |
| **Repo**     | 数据持久化，隔离 SQLite 细节       | `KVRepo` / `RuleRepo` / `PersonRepo` / `PerceptionLogRepo` / `TaskRepo` / `TokenUsageRepo`（`database/*.py`）、任务记录各 Repo（`task_record/repo.py`）                                                                                                                                                |
| **外部代理** | 封装第三方 API                     | `MiotProxy`（`miot/client.py`）、`PerceptionEngineProxy`（`perception/client.py`）                                                                                                                                                                                                                     |

`Manager`（`manager.py`）是进程内依赖注入中心，各 Router 通过统一入口取到单例实例。

---

## 服务生命周期

### 启动顺序

`lifespan`（`main.py`）保证 AgentDispatcher 先于感知/MiOT 等 producer 就绪，以防 producer 启动后立即触发事件时消费方尚未准备好。

### 优雅关闭

生产者先于消费者关闭，防止 in-flight 数据丢失。

### 健康检查（`/health`）

`/health` 无需鉴权。语义是"不不健康"：

| 返回                         | 含义                           |
| ---------------------------- | ------------------------------ |
| `200 {"status":"ok"}`        | 无节点处于 FAILED 或 STALLED   |
| `503 {"status":"unhealthy"}` | 至少一个节点 FAILED 或 STALLED |
| `503 {"status":"unknown"}`   | 健康检查自身抛异常             |

感知引擎 PREREQ_MISSING（Omni API Key 未配置或 ONNX 模型缺失）是预期等待态，`/health` 返回 200。节点详情通过 `/api/monitor/nodes`（需鉴权）查询。

---

## 四条主链路数据流

### 设备控制

```
CLI / Agent Skill
  → POST /api/miot/devices/{did}/control
  → MiotService（scope 校验）
  → MiotProxy → MIoTClient.http_client
  → 小米云 HTTP API → 米家设备
```

> 控制写入固定走 Cloud HTTP，不走 LAN 直连（详见 [device-control](../03-features/device-control.md#控制写入固定走-cloud-http)）；LAN / mDNS 仅用于设备发现与在线状态维护。

### 感知→规则→设备完整链路

```
摄像头码流
  → MultimodalCollector（帧缓冲）→ PerceptionRunner 触发
  → PipelineProcessor → PerceptionEngineProxy
      Gate（帧差分+音频能量，过滤静止窗口）
        ↓ 通过
      Identity（DeepSORT 跟踪 → {track_id → person_id}）
        ↓
      Omni（VLM：caption + matched_rules + speeches + suggestions）
  → PerceptionEngineProxy（结果后处理）
      → RuleService.update_state（驱动 RuleRunner 状态机）
          STATIC 规则 → MiotProxy → 设备
          DYNAMIC 规则 → AgentDispatcher → OpenClaw Webhook → Agent → Skill
```

### Agent 指令

```
用户 → OpenClaw Agent → Skill（miloco-*）→ miloco-cli
  → POST /api/*（Bearer token 鉴权）
  → Service → 设备/感知/规则/任务
```

### 家庭记忆注入

```
感知日志 / 用户对话
  → Cron（perception-digest / home-patrol / home-dreaming 等）
  → HomeProfileService → profile.md（$MILOCO_HOME/home-profile/profile.md）
  ├─ before_prompt_build Hook → Agent system context（hooks/prompt.ts）
  └─ home_profile_loader.py → Omni prompt 动态层
```

---

## 技术栈

| 组件     | 技术                                                                                                                  |
| -------- | --------------------------------------------------------------------------------------------------------------------- |
| Web 框架 | FastAPI + Uvicorn（单进程，不支持 multi-worker）                                                                      |
| 持久化   | SQLite（`miloco.db` 业务数据，18 张表，schema 见 `database/connector.py`；`observability.db` 性能追踪）               |
| 配置     | pydantic-settings，优先级：环境变量 > `$MILOCO_HOME/config.json` > `settings.yaml` > 代码默认值                       |
| MiOT     | OAuth2 + 小米云 API + LAN OT 协议 + 摄像头 C 库                                                                       |
| 感知     | OpenCV + ONNX Runtime（人体检测 + ReID）+ VLM（可配置）                                                               |
| CLI      | Click + httpx                                                                                                         |
| 插件     | TypeScript + OpenClaw Plugin SDK                                                                                      |
| 前端     | React + Vite + Tailwind（由 SPA handler 伺服，无独立前端服务）；中英双语 i18n（`web/src/i18n/`，默认中文 / 可选英文） |
| 包管理   | uv workspace（Python）/ pnpm（TypeScript）                                                                            |

---

## HTTP 契约语义

所有 API 响应走统一 JSON 封装（`NormalResponse`）：

| 状态码 | 触发场景                                           |
| ------ | -------------------------------------------------- |
| `200`  | 成功；业务错误也返回 200，靠 `code` 字段区分       |
| `401`  | Bearer token 缺失或无效（code=1003）               |
| `422`  | 请求参数 Pydantic 校验失败（code=1002）            |
| `503`  | 感知引擎未就绪；或 `server.token` 未配置时访问 SPA |

业务错误码（HTTP 200 + code）：常见 code 含义见 [故障排查 · 错误码速查](../06-dev-guide/troubleshooting.md#错误码速查)。

---

## 家庭面板（web/）

家庭面板是 React SPA，构建产物由 `spa_handler`（`main.py`）伺服，无需独立前端服务器。访问 `http://<host>:1810/` 即可使用，开发期 `pnpm dev` 启动 Vite dev server 自动代理 `/api` 到 backend。

### 五个主标签页

| 标签                 | 内容                                                                                                                                        | 独有操作                      |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| **概览（now）**      | 当前在家成员、摄像头状态卡（含直播入口）、感知暂停/恢复；MiOT/感知引擎状态条                                                                | 实时摄像头直播、感知引擎启停  |
| **设备（devices）**  | 按房间展示设备列表、属性查询、开关/属性控制、场景触发                                                                                       | 直接点击控制设备（无需 CLI）  |
| **家庭（family）**   | 成员档案、正式家庭记忆、待审候选知识                                                                                                        | 人脸注册、成员管理            |
| **日志（activity）** | 今日有价值事件流（规则命中、语音指令、建议），可回放视频片段                                                                                | —                             |
| **模型（usage）**    | LLM Token 用量统计（今日/近7天/近30天），含调用类型与模态构成饼图；omni 模型配置管理（增改/切换生效/停用/删除档案，切换生效前做连通性校验） | omni 模型档案管理与连通性测试 |

**家庭切换器**：顶部 TopBar 中显示当前启用的家庭名，多家庭账号可在此切换——切换后后端单事务写 scope 并触发后台刷新，前端整页 reload。底部左侧显示米家账号登录状态，可在此绑定/解绑米家账号。

**与 CLI 的功能分工**：设备控制、感知查询、规则/任务管理既可通过 web 面板操作，也可通过 `miloco-cli` 命令行完成。仅 web 面板支持实时摄像头直播和人脸注册可视化预览；仅 CLI 支持批量导出、脚本自动化。

**信任边界**：`/` 和 `/index.html` 响应体内嵌明文 `server.token`，等价于"能 GET / 就能调任意 `/api/*`"。默认 `server.host=127.0.0.1` 仅本机可达；开放 LAN 需自行评估网络边界。

---

## Token 用量追踪

Miloco 的感知流水线每次调 VLM（Omni）都会消耗 token。为了让用户了解"这套家庭 AI 每天花了多少 token、主要花在哪里"，系统内置了 token 用量追踪机制。

**为什么要追踪**：LLM API 按 token 计费，VLM 调用量与家庭摄像头数量、活跃程度直接相关；用户需要知道实际开销，也需要知道缓存命中率（cache 不计费），以便评估成本和调整配置。

**追踪什么**：每次 Omni 调用完成后，`TokenUsageRepo`（`database/token_usage_repo.py`）同步写入一条用量记录；记录结构见该文件。

**数据存储**：`miloco.db` 中 token 用量相关表采用两级设计——原始明细表保留近期每次调用，每日汇总表聚合历史趋势，保证细粒度分布和长期历史趋势均可查；schema 见 `database/connector.py`。

**在哪查看**：web 面板"模型"标签页查看（今日时序分布 + 多日汇总），API 端点见 `admin/router.py`。

---

## 横切关注点

**节点监控**（`node_monitor/`）：进程级单例注册表（`NodeMonitor`），追踪多个关键节点（camera / collector / processor / engine / rule / miot_proxy 等）的 lifecycle 和运行指标。节点有两类终态：`prereq_missing`（预期等待态，如 Omni API Key 未配置，不触发 503）和 `stalled/failed`（触发 `/health` 503）。配套两个后台守护线程：`WatchdogTask` 周期扫描各节点、把长时间无进展者判为 `stalled`；`ResourceMonitor` 定期采样进程内存 / 资源占用（供排查内存增长）。节点详情通过 `/api/monitor/nodes` 查询、内存/资源采样通过 `/api/monitor/memory` 系列端点查询（均需鉴权）。

**事件调度**（`dispatch/dispatcher.py`，`AgentDispatcher`）：所有后端 → Agent 投递的统一收口。保证同一 session_key 单飞、同类批量合并、队列超长时按优先级淘汰。五类事件分三条 session 路由（交互、绑定、onboarding 共用主会话，规则、建议各一条），按合并类型各自单飞、互不混入同一 turn；详见 [Agent 集成](../03-features/openclaw-integration.md)。

**可观测性**（`observability/`）：`MetricsClient`（`observability/metrics_client.py`）通过异步队列将感知 cycle trace 写入 `observability.db`；`AgentMetaPoller`（`observability/agent_meta_poller.py`）从 OpenClaw 轮询 agent run 元数据，写入 `agent_runs` 表。`perf.enabled=false` 时整套不初始化。性能数据通过 web 面板 URL hash `#perf` 进入的独立调试视图查看，或通过可观测性相关端点查询。

**后台清理**（`main.py`）：周期性清理感知日志、规则日志、`meaningful_events`、事件截图与 omni trace（TTL + 磁盘 LRU，跟事件目录同生共死）、observability 各表。

**管理接口**（`admin/router.py`）：系统状态聚合、token 用量查询、debug 开关、日志打包、后端版本查询（`/api/admin/version`，含 git 信息；wheel 部署无 git checkout 时从包版本号反推）、omni 模型配置管理（增改 / 切换生效 / 停用 / 删除档案，并提供连通性校验端点，供前端在切换生效前校验、不通过则不启用）。

---

其余模块详见 [knowledge/README.md](../README.md) 全局索引。
