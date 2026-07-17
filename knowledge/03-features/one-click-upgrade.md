# 一键升级（Web 升级提示 + 自升级）

## 背景与目标

Miloco 只经 GitHub Release 发布（CalVer tag，各平台归档 + `install.sh`）。过去用户无从在面板得知有新版本，升级只能到主机上手动重跑 `install.sh`。本能力让 Web 主动、克制地提示新正式版并支持一键升级，把"知道有新版 + 完成升级"收进家庭面板，降低升级门槛。

核心难点是**自升级**：提供 Web 的后端**自己就是被升级的对象、又是拉起升级的人**——升级过程中它会重启（甚至多次重启）自己，界面与进度必须扛过这次"自我重启"，并在新版本真正就绪后让页面自动切过去。

---

## 产品面

### 能做什么

- **被动提示、零主动打扰**：打开页面查一次，有新正式版时出一条顶部窄 banner + 侧栏版本号旁一个提示点；无轮询、不弹窗打扰。
- **提示点常驻、banner 可按版本关**：只要有可升级新版，侧栏提示点就常驻（低调入口，不受关闭影响）；关掉顶部 banner = 按版本"已确认"，该版本在出现**更新的**版本前不再出 banner（永久、不设时限，仅关 banner 触发、点升级入口不算）。**已确认状态存后端、不放浏览器**——随服务器状态走、重装即清零，换浏览器 / 清缓存都不影响。
- **侧栏版本号随时可点**：有已知更新 → 开确认弹窗引导升级；无已知更新 → 现查一次 GitHub（跳过缓存），结果就在同一弹窗里显示"已是最新"或"暂时无法检查"，连不上时绝不误报"已是最新"。
- **一键升级**：确认弹窗 → 升级中显示「下载 → 安装 → 重启」三步实时进度 → 完成后自动刷新到新版本。
- **不锁死用户**：升级中可「转入后台」（升级继续跑、完成仍自动刷新）；失败或超时给出可操作提示（刷新确认 / 稍后重试 / 联系管理员）。

### 能力边界

- **仅 release 部署可一键升级**：归档 / wheel 部署才给一键；git checkout(dev) 部署不出 banner 与提示点（按 release 门控隐藏）；侧栏版本号入口仍可点，但点击（检查更新）只会提示"请 `git pull` 更新后重启"，不给一键升级——不谎称"已是最新"。
- **只提示官方 latest 正式版**：不提示预发布版。
- **roll-forward、不回滚**：升级失败则保持在原版本并引导重试，不自动降级。
- **检测依赖能连到 GitHub**：连不上时静默不提示（不打扰、不报错），不影响其它功能。

---

## 研发面

### 架构概览（数据流）

检测与执行两条链路，均为 admin 端点（`/api/admin/upgrade/*`，走统一鉴权）；执行链路刻意**复用官方安装器**而非自研升级编排。

```
打开页面 → GET /upgrade/check（admin/router.py::upgrade_check）
  查 GitHub releases/latest + 比对当前版本 + 判 deploy_kind，结果服务端缓存（可带 force 跳缓存现查）
  → 有新版：侧栏提示点常驻（web Sidebar）+ 顶部 banner（web UpgradeNotice）
    提示点只看"有没有可升级新版"；banner 另受"已确认版本"门控

关 banner → POST /upgrade/dismiss（admin/router.py::upgrade_dismiss）
  按版本把"已确认"记到后端；下次 check 随 data.dismissed 返回，banner 据此门控（提示点不看它）

点升级 → POST /upgrade/run（admin/router.py::upgrade_run）
  仅 release 放行；起一个**脱离后端会话**的独立进程（start_new_session）：
    curl 官方 install.sh → bash --agent-prepare && --agent-finish → miloco-cli service start
    → 全部完成后向 upgrade.log 追加终态标记 AGENT_UPGRADE_DONE / AGENT_UPGRADE_FAILED
  注：安装器会在中途多次重启后端，令新版本"提前"可达

升级中 → 前端定时轮询：
  GET /upgrade/status（admin/router.py::upgrade_status）读 upgrade.log → 报当前阶段
    后端正被重启、连不上 = 前端点亮「重启」步（把断连当进度信号，非报错）
    读到 AGENT_UPGRADE_DONE = 完成 → 前端 location.reload() 拉新版本（含新注入 token）
    读到 AGENT_UPGRADE_FAILED = 失败 → 前端显失败提示（仍在原版本）
```

### 核心模块

| 类 / 符号                                          | 文件                                                                | 职责                                                                                                            |
| -------------------------------------------------- | ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `upgrade_check` / `upgrade_run` / `upgrade_status` | `admin/router.py`                                                   | 检测 / 触发 / 进度三端点：版本比较、deploy_kind 判定、GitHub 查询与服务端缓存、detached 起进程、日志阶段解析    |
| `UpgradeNotice`                                    | `web/src/components/UpgradeNotice.tsx`                              | 升级 UI：banner + 确认弹窗 + 升级中三步进度 + 失败/超时态；轮询 status、完成刷新                                |
| `Sidebar`（页脚版本行）                            | `web/src/components/Sidebar.tsx`                                    | 侧栏底部版本号 + 提示点，作为常驻升级入口                                                                       |
| `useUpgrade` / `lib/upgrade`                       | `web/src/hooks/useUpgrade.ts` / `web/src/lib/upgrade.ts`            | check 结果跨组件共享（window 事件）+ 手动强制现查、dismiss 写后端并广播、banner/点可见性与阶段→步骤映射等纯逻辑 |
| `install.sh` agent 模式                            | `scripts/install.py`（见 [开发指南](../06-dev-guide/dev-guide.md)） | 实际装包 / 重启服务，被 `upgrade_run` 复用；本模块不自研这段                                                    |

### 关键设计决策

**自升级如何扛过自身重启**（本模块最核心的"为什么"）——后端既提供 UI、又是被升级对象、又拉起升级，三点保证升级中界面与进度不断：① UI 是浏览器里**已加载的前端**，后端 down 时页面照常渲染，只是数据（进度）暂取不到；② 升级进程用 `start_new_session` **脱离后端进程组/会话**，后端被自己重启杀掉也不影响它继续跑、继续写日志；③ 进度来自 `upgrade_status` 读**磁盘上的 `upgrade.log`**，跨后端重启一直可读，哪个后端实例活着都能报。后端真正不可达的几秒，前端把"连不上"当作「重启中」信号点亮，而非报错。

**完成只认日志终态标记，不看版本变更**——`install.py` 在 agent-prepare / agent-finish 里会多次重启到新版本，令新版本在升级还没真正干完（模型未解压、插件未装）时就"提前"可达；若以"版本号变了"判完成会误刷到半成品后端。故脚本在**全部完成之后**才向日志追加 `AGENT_UPGRADE_DONE`，前端只认这个终态标记触发刷新（失败追加 `AGENT_UPGRADE_FAILED`）。

**复用官方 `install.sh`、不自研升级编排**——`upgrade_run` 只是 detached 调用安装器的非交互 agent 模式（`--agent-prepare` / `--agent-finish`，见 [开发指南](../06-dev-guide/dev-guide.md)），保证"Web 一键升级"与"手动安装"走同一条路径、行为一致，避免维护两套升级逻辑。

**检测与显示的语言解耦**——升级子进程强制 `MILOCO_LANG=zh`，让 `upgrade.log` 语言确定，`upgrade_status` 的阶段解析不受服务器 locale 影响；而用户看到的步骤文字走前端 i18n、跟随网页语言。日志是内部产物、用户不可见，两者互不影响。

**release / dev 判定基于包版本串**——用 hatch-vcs 版本本地段（`.dev` / `+g<sha>`）判定部署类型，而非目录里有无 `.git`；避免 wheel 恰好装在某个 git 仓库子目录里被误判成 dev、错误禁用一键升级。

**"已确认版本"（dismiss）存后端、不存浏览器；提示点与它解耦**——关 banner 写到后端、随 `/upgrade/check` 的 `dismissed` 字段回来，而非浏览器 localStorage：这样重装即清零（可复现测试）、跨浏览器一致、不被清缓存等浏览器行为干扰。且**只有关 banner 才算 dismiss**（点升级入口不算——那是去升级、不是"不看了"）；侧栏提示点只看"有没有可升级新版"、不看 dismiss，关掉 banner 后仍常驻，作为低调的复查入口，避免"明明有新版却什么提示都没有"。

**单飞防并发 + roll-forward**——`upgrade_run` 用进程内单飞标志防止并发升级；不做回滚，失败保持原版本、靠再发新版本修复。

> 具体阈值（缓存 / 轮询 / 单飞 TTL、轮询间隔与超时）、字段与状态码全表属实现细节，见 `admin/router.py` 与 `web/src/components/UpgradeNotice.tsx`，此处不展开。

### 对外契约语义

- **`GET /upgrade/check`**（可带 `force` 跳缓存现查，供手动"检查更新"）：返回当前版本、最新版本、是否有更新、部署类型（release / dev）、GitHub 是否可达、后端已确认版本 `dismissed` 等；不可达时静默降级为"无更新、不可达"，不报错。
- **`POST /upgrade/dismiss`**：按版本把"已确认"记到后端；下次 `check` 随 `dismissed` 返回，banner 据此门控（提示点不受影响）。
- **`POST /upgrade/run`**：仅 release 部署放行；dev 部署 / 无更新归一为「失败」类状态码拒绝，「已在升级中」另用一类状态码拒绝——供前端区分"提示失败"与"接管进度"。放行后立即返回，升级在 detached 进程异步进行。
- **`GET /upgrade/status`**：报升级阶段（`idle` / 下载 / 安装 / `done` / `failed` 等），供前端点亮步骤、判完成、判失败。

字段、状态码取值见 `admin/router.py`。

### 如果我要改一键升级相关功能

| 修改目标                                       | 去看哪个文件                                                        |
| ---------------------------------------------- | ------------------------------------------------------------------- |
| 检测 / 版本比较 / deploy_kind / GitHub 缓存    | `admin/router.py`（`upgrade_check` 及其 helper）                    |
| 手动检查更新（force）/ 已确认版本(dismiss)存储 | `admin/router.py`（`upgrade_check` 的 `force`、`upgrade_dismiss`）  |
| 升级触发脚本 / 终态标记 / 单飞                 | `admin/router.py`（`upgrade_run`）                                  |
| 进度阶段解析（日志标记）                       | `admin/router.py`（`upgrade_status` + 阶段标记表）                  |
| 升级 UI / 三步进度 / 完成刷新 / 失败态         | `web/src/components/UpgradeNotice.tsx`                              |
| 提示点 / 版本号页脚入口                        | `web/src/components/Sidebar.tsx`                                    |
| 提示可见性 / 已确认版本 / 阶段映射（纯逻辑）   | `web/src/lib/upgrade.ts`、`web/src/hooks/useUpgrade.ts`             |
| 实际装包 / 重启流程                            | `scripts/install.py`（见 [开发指南](../06-dev-guide/dev-guide.md)） |

### 与其他模块的关系

- **安装器**：复用 `install.sh` 的 agent 模式，装包 / 重启的真实流程见 [开发指南](../06-dev-guide/dev-guide.md)；升级卡住 / 失败的排查见 [故障排查](../06-dev-guide/troubleshooting.md)。
- **设计规范**：banner / 确认弹窗 / 进度状态点复用 [设计规范](../07-design/README.md) 的既有 token 与模式（状态用点不用块）。
