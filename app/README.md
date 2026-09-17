# Miloco.app —— macOS 独立应用（slim 版）

把 Miloco 的**后端核心 + Web 管理页**打成一个双击即用的 macOS 应用：内嵌 Python 解释器与
全部依赖，不需要 CLI、不需要 openclaw/hermes 插件、不需要 supervisor 管生命周期，也不需要
任何**本地端侧模型文件**（ONNX 那一套全部裁掉）。

> ⚠️ 唯一必填的配置是**一个多模态（omni）云模型的 API Key**：slim 版走「纯场景触发」
> （`perception.engine.rule_only`），即由 omni 模型看视频判断是否命中规则，本地不存在替代
> 方案。不配置的话服务能正常启动、设备/场景手动控制可用，但**不会自动触发任何联动**，
> 顶部会出现红色告警条，点「到「模型」页修改」即打开设置抽屉里的模型配置
> （管理页 → 右上角设置 → 模型配置：Base URL + API Key + 模型，可测试连接）。

```
Miloco.app/
├── Contents/MacOS/Miloco            启动器（菜单栏图标，管生命周期；**不驻留 Dock**）
└── Contents/Resources/
    ├── py/                          内嵌 CPython 3.12 + 精简依赖（不写进用户目录）
    ├── defaults/config.json         slim 默认配置（首次启动写入数据目录）
    └── Miloco.icns
```

## 系统要求

- macOS **14.0+**（Sonoma；`av`/`opencv` 的 arm64 wheel 最低要求）
- Apple Silicon（M1 及以后）
- 约 340 MB 磁盘

## 安装

1. 把 `Miloco.app` 拖进 `/Applications`。
2. **首次打开前**去掉隔离属性（应用未做 Developer ID 公证，这一步是必需的）：

   ```bash
   xattr -dr com.apple.quarantine /Applications/Miloco.app
   ```

3. 双击 `Miloco.app`。菜单栏出现 Miloco 图标，浏览器自动打开管理页
   （`http://127.0.0.1:1812/`，也可以从菜单栏图标再打开 App 内置窗口、或用浏览器打开）。

> 也可以右键 → 打开 → 在弹窗里点「打开」，效果等同 `xattr`。

## 日常使用

菜单栏图标是 **favicon 里那栋房子本身**（去掉橙色圆角底、透明背景的单色模板图
`app/launcher/MenuBarIcon.svg`），所以会跟随菜单栏浅色/深色自动反色，没有白色底板。
日志里会打 `菜单栏图标：MenuBarIcon.svg（房子线稿·模板单色 isTemplate=true）`。

> 不要用 App 图标（`Miloco.icns`）当菜单栏图标：icns 是 QuickLook 渲的、**背景不透明**，
> 缩到 18px 放进菜单栏就是一块带白边的方块（模板渲染更会整块变实心）。
> `app/smoke_test.sh` 会用像素统计把这条卡住。

**没有 Dock 图标**：启动器把自己的激活策略设成 `accessory`（`app/launcher/main.swift` 的
`activationPolicy`，`--selftest` 输出 `activation=accessory`，构建期断言），所以 Dock 与
⌘-Tab 都不驻留 —— 生命周期的唯一入口就是菜单栏图标（能看状态、能启停、能退出）。Dock 图标
原来只是"内置窗口开关"，摆在那儿反而让人以为关窗口/退 Dock 就是停服务。窗口仍在：菜单栏
「打开管理页面」、Finder 双击、菜单栏状态行都能打开；聚焦时 App 依旧是前台应用，自己的菜单栏
（含 Edit→Paste ⌘V、⌘Q）照常生效。Launchpad / Finder 里也照常能找到并启动。

点一下图标提供：

| 菜单项 | 说明 |
| --- | --- |
| 打开管理页面 | 打开 **App 内置的管理窗口**（WKWebView，像 Electron/Tauri 那样），不跳浏览器；右上角设置里含「模型配置」 |
| 在浏览器中打开 | 用默认浏览器打开同一个管理页（多屏、投屏、调试时用） |
| 启动服务 / 停止服务 / 重启服务 | 手动控制后端；停止 = SIGTERM 优雅退出，25s 不退则 SIGKILL |
| 开机自动启动 | 写入/删除 `~/Library/LaunchAgents/com.xiaomi.miloco.plist`（默认关）。plist 里是**绝对路径**，所以 App 被挪到别处（下载目录 → /Applications）后启动时会自动按新路径重写，否则开机只会静默失败 |
| 运行期间防止休眠 | 默认**开**：服务运行期间阻止 macOS 空闲休眠（屏幕该睡还是睡），避免米家推送、摄像头流与场景联动因系统睡眠中断；服务停止或退出 App 时自动释放 |
| 语言 | 子菜单：跟随系统 / 简体中文 / English（主菜单的「Miloco → 语言」里也有一份）。切换会同时作用于 App 菜单、通知和内置页面 |
| 打开数据目录 | `~/Library/Application Support/Miloco` |
| 打开日志目录 | 同上 `/log`，`launcher.log` 是启动器日志，`miloco-backend_*.log` 是后端日志 |
| 退出 Miloco | 停掉后端并退出（⌘Q 同效） |

**关窗 ≠ 退出**：窗口关掉后服务继续在菜单栏跑，再点菜单栏「打开管理页面」（或 Finder 里
双击 App）就能把窗口叫回来。没有 Dock 图标，所以退出只有两条路：⌘Q，或菜单「退出 Miloco」
（会先 SIGTERM 停后端）。

### 界面语言：跟随系统，默认英文

| 场景 | 界面语言（菜单 / 通知 / 窗口）/ 页面语言 |
| --- | --- |
| 系统语言是中文 | 中文 |
| 系统语言是其它（英语、法语…） | **英文**（不支持的语言一律落到英文） |
| 想强制指定 | `MILOCO_APP_LANG=zh`（或 `en`）启动，例如<br>`MILOCO_APP_LANG=en /Applications/Miloco.app/Contents/MacOS/Miloco`；此时页面也会带上 `?lang=en` |
| 页面里切换 | 页面自己的语言开关**会同步到原生菜单**（页面通过 `webkit.messageHandlers.milocoLang` 回报），App 菜单跟着切 |
| 从 English 切回跟随系统 | 选菜单「语言 → 跟随系统」；这会把页面里存过的语言偏好一并清掉（内部用 `?lang=auto`），否则页面会把旧偏好报回来把「跟随系统」顶掉 |

实现上没有引入资源文件：启动器里是一张「英文 / 中文」行内对照表（`t("English", "中文")`），
语言取自 `Locale.preferredLanguages`，所以 `-AppleLanguages '(fr)'` 这类系统级覆盖也生效。
构建期会把「中文系统→中文、非中英文系统→英文、`MILOCO_APP_LANG` 可强制」三条都断言一遍
（`app/smoke_test.sh` 第 1 步），不需要靠肉眼看菜单。日志文本仍是中文（面向排查）。

### 端口：和 CLI 版分开

App 固定用 **1812**（CLI / supervisor 版是 1810），两条链路可以同时跑、互不抢端口，
App 也**不会**去接管 1810 上的 CLI 后端。只有 1812 被非 Miloco 程序占用时才顺延到 1813–1822。

### 感知参数的默认档（网页「设置」里可改）

| 参数 | 默认 | 说明 |
|---|---|---|
| `input.video_short_edge` | **768** | 送模型画面的短边。清晰度主要由它决定（小目标/文字），调低省 CPU 与 token |
| `input.media_resolution` | **high** | 仅 Gemini：视频请求里**显式**带 `MEDIA_RESOLUTION_HIGH`（264 tok/帧）；配 `low` 得 `MEDIA_RESOLUTION_LOW`（66 tok/帧） |
| `input.rule_only_input` | **image** | 感知输入是**图片**（窗口各帧 JPEG），对纯静态规则判定更聚焦；`video` = 整窗 mp4（按帧计费的模型更省 token） |
| `input.last_frame_only` | **false** | 图片输入时送**多帧**（动作/手势类规则更稳）；`true` = 每窗只送最后一帧（省 token、更快） |

后两项就是网页「设置 → 感知输入」的两个控件（只对 slim 的 `rule_only` 引擎生效）。四个都是
**热读**：保存后下个感知窗口生效，不需要重启引擎。用户在网页改过的值落在数据目录的
`config.json`，会覆盖包内 `settings.yaml` 的默认档 —— 所以升级 App 不会把住户自己的选择改掉。

### 粘贴（⌘V）为什么必须有主菜单

WKWebView 的 ⌘C / ⌘V / ⌘A / ⌘Z 不是 WebKit 自己抓的键，而是「App 主菜单的 key
equivalent 命中 → 转发给 first responder」。所以 App 现在会建一份真正的 `NSApp.mainMenu`
（Miloco / 编辑 / 窗口），其中「编辑」菜单里是标准的剪切/拷贝/粘贴/全选。

没有这份主菜单时，在窗口里按 ⌘V 什么都不会发生（右键 → 粘贴还能用），表现就是住户说的
「粘贴 token / 模型配置失败」——实测 A/B：无主菜单时粘贴进去的内容是错的，有主菜单时正常。
启动日志里会打一行 `主菜单：… 编辑[… 粘贴⌘V …]`，出问题时先看这行。

### 构建产物不要出现在启动台

启动台的图标列表来自 **LaunchServices 注册表**（`lsregister -dump` 能看到，Spotlight 索引
是另一条路径）。所以只要 `open` 过仓库里的 `dist/app/Miloco.app`，它就被注册，之后和装到
`/Applications` 的那份一起，启动台会出现两个同名 Miloco —— 实测注册表里确实同时有：

```
path:  /Applications/Miloco.app
path:  /Users/…/xiaomi-miloco-public/dist/app/Miloco.app      ← 多出来的那个
```

构建脚本做了两件事防止它复发：`dist/` 里放 `.metadata_never_index`（Spotlight 跳过整棵树），
以及构建收尾 `lsregister -u "$APP"` 注销本次产物（只删注册记录、不动文件，直接跑
`Contents/MacOS/Miloco` 或 `app/smoke_test.sh <path>` 都不受影响）。

已经多出来一个时：

```bash
lsregister -u /path/to/dist/app/Miloco.app     # 注销仓库那份
lsregister -u /Volumes/Miloco/Miloco.app       # dmg 卷卸载后残留的注册
killall Dock                                   # 刷新启动台
```

排查时**别用 `open dist/app/Miloco.app`**（会把仓库那份重新注册进启动台），要么用装好的
`/Applications/Miloco.app`，要么直接跑 `dist/app/Miloco.app/Contents/MacOS/Miloco`。

### 内置窗口

管理页面直接在 App 窗口里用 `WKWebView` 渲染：

- 页面加载完成后会往 `launcher.log` 写一条
  `内置窗口已渲染：… title=… 正文=… 字符 页面语言=…`（延迟 1.5s 再探，等 React 画完），
  排查「窗口白屏」时先看这一行。
- 站内链接留在窗口里；住户点开的外部链接（米家授权页等）交给默认浏览器，避免第三方登录态
  混进 App。
- 已放宽 WebView 内的 ATS（`NSAllowsArbitraryLoadsInWebContent`），否则局域网/云端的摄像头
  流会被拦；App 自身发起的请求仍走默认策略。
- 允许媒体自动播放（摄像头画面进页面即起播），并开启 Web Inspector（`isInspectable`）便于排查。

### 服务异常会自愈

| 场景 | 行为 |
| --- | --- |
| 后端进程崩溃 / 被 OOM 杀掉 | 指数退避自动重启（1s→2s→…→30s），连续 5 次仍失败则停下并弹通知，不再空转 |
| 后端卡死（进程在但 `/health` 不通） | 每 15s 巡检，连续 3 次失败判定为异常并重启 |
| `config.json` 损坏/手改错 | 启动前用内置解释器按后端真实路径校验；失败则备份为 `config.json.bad-<时间戳>` 并回默认配置 |
| 1812 端口被别的程序占用 | 自动顺延到 1813–1822，并在日志与菜单里显示实际端口 |
| 重复双击应用 | 单实例：第二个实例通知第一个打开管理页后自己退出 |
| 1812 端口上已有**本 App** 的后端在跑（如启动器曾异常退出） | 接管为「运行中」而不重复启动 |
| 1812 端口上是**别的安装**的 Miloco 后端 | 仍复用它（同一台机器跑两套感知引擎会重复占用米家配额、抢摄像头流），但会用 token 鉴权区分归属：不是本 App 的就在日志与通知里说明「数据目录不同」 |

## 数据目录与「独立性」

App 的数据**只在** `~/Library/Application Support/Miloco/`：

```
config.json      配置（server.token、模型配置、全局上下文等，经 Web 改写）
miloco.db        数据库：米家授权 token、场景联动规则、事件与活动记录
images/          事件截图    log/  日志    miot_cache/  米家本地缓存
```

**App 不读取、不复制任何外部安装的数据** —— 不碰 `~/.openclaw/miloco/`，也不碰 CLI 版或
任何 openclaw 目录。首次启动只把**包内自带的 slim 默认配置**写进上面的目录，米家账号与
模型 Key 都要在 App 里重新绑定 / 配置。

这样做是为了数据归属清晰：App 与 CLI 版彻底隔离，两份安装可以并存、各自升级、互不影响，
不会出现「App 悄悄用了 CLI 版账号和模型 Key」这种说不清的状态。

> 真要把 CLI 版的账号与规则搬过来，属于手工操作：退出 App，把 `~/.openclaw/miloco/` 里的
> `config.json` 与 `miloco.db` 拷进上面的目录，再启动。注意 `config.json` 里必须
> `perception.engine.rule_only = true`、`perf.enabled` 保持 `true` —— 全量版配置会让 slim
> 去加载它根本没打包的端侧模型（onnxruntime），直接起不来。

### 本地网络权限

首次连接米家摄像头时，macOS 会弹「Miloco 想要查找并连接本地网络上的设备」——**必须允许**，
否则局域网发现与点对点视频连接不可用（应用已声明 `NSLocalNetworkUsageDescription` 说明用途）。

## 这个版本不包含什么

slim 版的界面只有四个 tab —— **概览 / 场景联动 / 日志 / 模型**，外加右上角设置抽屉：

- **概览**：摄像头实时画面 + 每路「投喂 / 拾音」开关（家人、宠物、token 入口都不显示）；
  顶部状态条上是全局感知控制（感知状态 · 唤醒 / 重启引擎）。
- **场景联动 / 日志**：规则触发的联动与活动流。日志页右上角有**清理**按钮（行内二次确认，不用弹窗），
  一次清空当前全部日志 —— 三处存储各一个 `POST /clear`、返回各自删除条数：
  `meaningful_events`（感知事件）、`on_demand_log`（按需查询日志）、`action_ledger`
  （动作台账，**「触发场景」就在这本台账里**；漏了它住户清完仍会看到一屏触发场景）。
  事件截图/片段是文件，不归这个按钮管：它们由后端自己的 TTL（`perception.snapshot_ttl_days`）
  + 磁盘上限（`snapshot_max_disk_mb`）两阶段清理。
  动作流**独立轮询**（3s）：`/api/events/stream` 只在感知事件落库时推消息，而"退出场景"
  这类动作没有对应的新事件（退出那一窗在 rule_only 下不会成为感知事件），只靠 SSE 会
  等到切走再切回来才显示。轮询先拉 1 条比对最新 id，变了才全量重拉。
- **模型**：只看 / 配模型（Base URL、API Key、模型、测试连接），不带 token 用量统计。
- **设置**：对全局上下文（全局感知系统提示词）等仍然生效的项开放；帧率、事件紧急度、
  agent 定时任务这些在 slim 下拨了也没用的项隐藏。
- **服务状态**：菜单栏**只有图标**（透明模板房子图，不跟状态文字、也不置灰，避免菜单栏变宽
  和「置灰看不出是在跑还是被系统禁用」）；鼠标悬停的 tooltip 是完整状态。点开菜单，顶部
  两行 = 版本号（灰、次要）+ 服务状态（**彩色圆点 + 语义色粗体**：运行中=绿、已停止=红、
  启动中/重启中=黄、无响应/反复崩溃=橙），一眼能看出后端有没有在跑。菜单里另有
  「检查服务状态」可手动重探（自动巡检 15s 一轮，且只在进程还在时探）。菜单里点「停止服务」后，
  已打开的内置网页 2s 内显示「服务已停止」+ 一条说明横幅，并每 2s 自动重试；
  服务回来后**页面自动整页重载**（各 tab 的 SSE、相机流、分页游标都是按老进程建的），
  重载前把当前 tab 记进 `sessionStorage`，不把住户弹回概览页。

以下能力**整体不注册**（不是隐藏入口，而是后端路由与后台任务都没有）：

- 身份识别、宠物识别、家庭档案、任务、米家设备页、token 用量统计页面
  （身份/宠物那半边依赖 ONNX 端侧模型，全量包才有）
- agent 动态动作（openclaw/hermes 插件、CLI 工具链）
- 在线一键升级（升级 = 用新版 `Miloco.app` 覆盖安装）
- 性能观测**页面**（`capabilities.observability=false`，导航里没有）。但 `perf.enabled` 保持
  `true` —— 它同时是动作台账 `action_ledger` 的总开关，而日志页的「动作」流（设备控制 / TTS /
  **场景触发**）读 `/api/actions`；关掉 perf 会让住户在日志里看不到场景进入/退出（曾经的回归）。
  slim 下性能采集本身几乎不产生数据（没有 agent，`agent_runs` / trace 恒空），只多一行场景触发台账。

Web 端会读 `GET /api/admin/edition` 自动收敛导航与首屏请求（身份/宠物/家庭档案这三条在
slim 未注册的路由**根本不发请求**，否则首屏会弹三条「加载失败」告警）；如果后端不可达或
版本较老，前端按「全量版」渲染，不会误砍功能。

## 卸载

```bash
# 退出应用后
rm -rf /Applications/Miloco.app
rm -rf ~/Library/Application\ Support/Miloco      # 含全部数据，谨慎
rm -f ~/Library/LaunchAgents/com.xiaomi.miloco.plist
```

## 从源码构建

```bash
app/build_app.sh                       # 完整构建 + 冒烟测试
app/build_app.sh --skip-web            # 前端未改时提速
app/build_app.sh --dmg                 # 额外产出 dist/app/Miloco-<ver>-arm64.dmg
app/build_app.sh --version 2026.9.16 --dmg # 发布用：显式指定版本号
```

不指定 `--version` 时版本来自仓库 git 状态（形如
`2026.6.18.post1.dev547+g90eaa867f.d20260916`，同时写进 `CFBundleShortVersionString` 与
dmg 文件名）——正式对外发布建议先打 tag，或用 `--version` 给一个干净版本号。

### 发布给他人（这一版是 ad-hoc 签名，未公证）

| 事项 | 现状 / 要求 |
| --- | --- |
| 代码签名 | ad-hoc（无 Developer ID、无公证）。使用者第一次打开前必须执行 `xattr -dr com.apple.quarantine /Applications/Miloco.app`，否则 Gatekeeper 会拦 |
| 系统要求 | **macOS 14+ / Apple Silicon**，已写进 `LSMinimumSystemVersion` |
| 产物 | `dist/app/Miloco.app`（≈332 MB）与 `dist/app/Miloco-<ver>-arm64.dmg`（≈159 MB，内含 `/Applications` 拖拽快捷方式） |
| 数据独立 | 不会读写 `~/.openclaw/miloco`，每个使用者拿到的是干净首启（需自己绑米家 + 配模型） |
| 依赖 | 全内置（CPython 3.12 + 精简依赖），使用者机器上不需要 python / node / uv / CLI |
| 签名门禁 | 构建脚本在冒烟测试后**再次校验签名**；任何在签名后往包里补写文件的行为都会让构建失败 |

构建脚本做的事：

1. `web/` 前端构建 → 拷进 `backend/miloco/src/miloco/static/`（由后端 SPA handler 提供）
2. 打 `miloco` + `miloco-miot`（darwin/arm64）wheel，复用 `scripts/version_normalize.py` 换算版本
3. `uv python install 3.12` → 把 python-build-standalone 树拷进 `Contents/Resources/py`
   （该发行版可重定位，`sys.prefix` 跟随 App 位置，因此整体挪动/改名都不会坏）
4. 装精简依赖（见下）→ 删掉 test/idlelib/tkinter/tcl-tk 等无用标准库 → 预热字节码
5. 自检：`MILOCO_EDITION=slim` 下能 import `miloco.main`，且 `/api/person`、`/api/pet`、
   `/api/home_profile` 路由**没有**被注册
6. `swiftc` 编译启动器 → ad-hoc 签名（内嵌 Mach-O → 主程序 → bundle）
7. `app/smoke_test.sh`：先跑 `Miloco --selftest` 卡住布局与可见行为（固定端口 1812、
   菜单栏图标是透明模板 SVG、界面语言默认英文/跟随系统、**`activation=accessory` 即无 Dock 图标**、
   **`pipereader=ok`** 即后端输出管道在 EOF 上自注销不会空转 CPU），
   再真起服务，验 `/health`、`/api/admin/edition`、身份路由 404、`observability.db` 已生成且
   `/api/actions` 读得到场景触发台账、一键清理清空三处存储、bootstrap 不破坏默认配置、
   包内感知默认档（768p / `media_resolution=high` / 图片输入 / 多帧）、SIGTERM 优雅退出

### 依赖裁剪

保留：`fastapi` / `uvicorn[standard]` / `pydantic` / `pydantic-settings` / `sse-starlette` /
`aiofiles` / `aiocache` / `aiohttp` / `apscheduler` / `httpx` / `python-dotenv` / `pyyaml` /
`cryptography`（米家云 X-Client-Secret 加解密）/ `paho-mqtt` / `psutil` / `numpy` /
`opencv-python-headless` / `av`（实时视频 H.264 转码）/ `Pillow` / `pi-heif`。

裁掉：`onnxruntime` / `scipy` / `tokenizers`（全量感知才用）、`fastmcp` + `mcp`（仅
`miot/mcp.py`，全仓库无人 import）、`zeroconf`（mDNS 是死桩，局域网发现实际走 UDP 广播）。

> 注意：`pydantic-settings` / `sse-starlette` / `pyyaml` / `python-multipart` / `cryptography`
> 原本是靠 `fastmcp → mcp` 传递进来的**未声明依赖**；裁掉 fastmcp 后已在
> `backend/miloco/pyproject.toml`、`backend/miot/pyproject.toml` 里显式声明。

### 启动器实现要点（`app/launcher/main.swift`）

- 子进程：`Contents/Resources/py/bin/python3 -m miloco.main`，工作目录 = 数据目录
- 注入 `MILOCO_HOME` / `MILOCO_EDITION=slim` / `MILOCO_SERVER__HOST` / `MILOCO_SERVER__PORT` /
  `MILOCO_SERVER__URL` / `PYTHONDONTWRITEBYTECODE=1`（不在签名包里写 `__pycache__`）
- **不设** `MILOCO_SUPERVISED`：后端 bootstrap 才会把 stdout/stderr 落到自己的日志文件
- 必须 `-m miloco.main` 启动（该模块的 `start_server()` 会先 bootstrap 生成/落盘 token）；
  用 `uvicorn miloco.main:app` 会跳过 bootstrap
- **后端输出管道的 reader 在 EOF 上必须自注销**（`readOutputOnce`）：bootstrap 会把子进程
  stdio 重定向到自己的日志文件 → 管道写端关闭 → `readabilityHandler` 底层是 level-triggered
  的 `EVFILT_READ`，EOF 的 fd 永远"可读"、`availableData` 立刻返回空。不自注销就是每毫秒空转
  一轮、吃满一个核（实测 25 万次回调/250ms、App 常驻 100% CPU）。`--selftest` 会在"写端已关"
  的真管子上跑一遍这条读取路径并断言回调次数，构建期就挡住回退
