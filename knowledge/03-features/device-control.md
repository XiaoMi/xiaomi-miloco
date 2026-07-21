# 设备控制

## 背景与目标

用户想让 AI 帮自己控制家里的灯、空调、风扇。传统方式需要打开米家 App 找到设备再操作；Miloco 让 Agent 直接理解用户意图并执行。

设备控制模块提供完整的米家设备操作能力：单属性写入、批量属性写入、动作调用、属性查询、场景执行，覆盖用户在 Agent 对话中所有可能的设备操作需求。

---

## 产品面

### 能做什么

- **属性控制**：设置设备的任意可写属性（亮度、色温、温度、开关、模式）；同一设备多属性可合并为一次请求
- **动作调用**：触发设备支持的动作（如音箱播报 TTS、扫地机开始清扫、空气净化器启动自动清洁）
- **属性查询**：读取设备当前状态，用于 Agent 回答"客厅灯现在是多少亮度"
- **场景执行**：一键触发米家配置的智能场景（多设备联动预设），如"回家模式""睡眠模式"
- **Scope 管理**：配置 Miloco 管控哪些家庭和摄像头，是设备接入的前置配置

### 典型场景

**场景 1 — 对话控制**：对 Agent 说"把客厅的灯调到 60% 亮度"。Agent 选择 `miloco-devices` Skill，通过 CLI 调 `/api/miot/devices/{did}/control`，后端执行属性写入，用户约 1 秒内看到灯光变化。

**场景 2 — 规则自动化**：感知流水线检测到"有人进入书房"，STATIC 规则触发，`RuleRunner` 直接调 `MiotProxy.set_device_properties` 打开书房台灯，无需 Agent 介入，无 LLM 额外调用。

**场景 3 — 家庭面板操作**：用户在浏览器打开家庭面板"设备"标签，按房间浏览设备列表，点击开关或滑块直接发起控制请求。

**场景 4 — 音箱 TTS**：Agent 需要向用户播报提醒，通过 `miloco-devices` Skill 找到房间内的音箱设备，调用 `play-text` 动作完成播报。

### 能力边界

- 仅操作已绑定小米账号、且被纳入启用家庭（scope）的设备，其余请求一律拒绝（返回 scope 校验错误）
- 设备联网状态由小米云管理，Server 不感知"控制是否真正送达硬件"——返回成功仅表示指令已发出
- 场景执行由小米云侧完成，Server 只负责转发
- 不支持自定义协议或非米家生态设备
- MiOT OAuth 未绑定时相关端点抛 `MiotOAuthException`

---

## 研发面

### 架构概览（数据流图）

```
CLI / Agent（miloco-devices Skill）
  → POST /api/miot/devices/{did}/control
  → MiotService（scope 校验 + 类型分发）（miot/service.py）
  → MiotProxy（miot/client.py）
  → MIoTClient.http_client（MIoTHttpClient，backend/miot/src/miot/cloud.py）
  → 小米云 HTTP API → 设备
```

属性查询入口为 `MiotService.get_device_status`，场景触发为 `trigger_scene` → `MiotProxy.execute_miot_scene`，链路结构相同。控制写入 / 查询 / 动作调用统一走 Cloud HTTP，不走局域网直连（见下「控制写入固定走 Cloud HTTP」）。

规则触发的 STATIC 控制路径较短：`RuleRunner`（`rule/runner.py`）直接调 `MiotProxy`，绕过 `MiotService`（规则在创建时已绑定 scope 内设备，设计上已保证安全）。

### 核心模块

**MiotService**（`miot/service.py`）

业务编排层，主要职责：

- **scope 校验**：检查请求 did 所属家庭是否在启用集内（KV 存储 home 白名单）
- **控制类型分发**：将 `set_property` / `set_properties` / `call_action` 请求转换为对应 MiOT 参数类型
- **LRU 设备目录维护**：记录用户操作过的设备及属性，确保其出现在 Agent 设备目录（catalog）中
- **home / camera scope 管理**：`switch_home` / `toggle_camera` 写 KV 后触发后台刷新并同步感知层 adapter
- **OAuth 校验**：未绑定或 token 过期时抛 `MiotOAuthException`

**MiotProxy**（`miot/client.py`）

Server 代理层，主要职责：

- **token 生命周期**：后台自动刷新，失效时清空 OAuth 缓存；token 通过 `KVRepo` 持久化，重启后自动恢复
- **数据缓存**：设备/摄像头/场景列表内存维护，重启时重新拉取
- **实时事件订阅**：注册 MIPS 云 MQTT 回调（设备改名 / 换房换家、家庭场景变更、摄像头云端上线 / 离线），经 `mips_listeners.py` 的防抖监听器刷新对应缓存，使本地缓存无需轮询即与云端收敛；设备绑定 / 移入受管家庭触发的设备欢迎见 [device-welcome.md](device-welcome.md)
- **device spec 按需缓存**：首次使用时加载并缓存，避免启动时全量拉取拖慢启动
- **摄像头 manager 管理**：维护每个摄像头的 `CameraVisionHandler`（`miot/camera_handler.py`）实例

**MIoTClient**（`backend/miot/src/miot/client.py`）

MiOT SDK 顶层客户端，聚合 Cloud、LAN、mDNS、MQTT、摄像头等子模块，对 MiotProxy 暴露统一异步接口。详见 [sdk-miot.md](../05-external-deps/sdk-miot.md)。

### Scope 机制

Scope 定义了"Miloco 管控哪些设备"的边界，分为两个维度：

**家庭维度（Home Scope）**：用户的小米账号下可能有多个家庭（如"公寓""父母家"），Miloco 同一时刻只管控一个家庭的设备。启用的家庭白名单持久化在 `miloco.db::kv` 表中，由 `filter.py`（`miot/filter.py`）读取后应用于过滤。

**摄像头维度（Camera Scope）**：在启用家庭内，用户可以进一步禁用某些摄像头（如不想让 Miloco 看客厅）。被禁用的摄像头 DID 以黑名单形式存在 KV 表中——新摄像头默认被感知，用户选择性关闭。同时启用的摄像头数量有上限（4 台，`filter.py::MAX_ENABLED_CAMERAS`，经状态接口下发前端作为唯一来源），主动启用超限或启用离线摄像头会被 `toggle_camera` 拒绝。

**Scope 过滤的作用点**：

- 设备列表 / 场景列表接口返回前，`filter.py` 过滤掉不在启用家庭的条目
- 控制设备前，`MiotService` 校验 did 所属家庭是否在启用集内，不在则拒绝
- 摄像头流水线层：摄像头拉流（native PPCS 会话 + 解码）与感知投喂**共用同一选择口径** `select_active_camera_dids`（`miot/filter.py`）——在启用家庭内、未拉黑、在线、且按 did 截断到启用上限，拉流集即投喂集不漂移。scope 变更后 `MiotService` 先 `refresh_cameras` 按新口径建 / 销 camera manager（关闭 / 移出家庭 / 离线 / 超额的摄像头会停掉 native 会话与解码），再同步感知 adapter 的投喂订阅，无需重启服务

**Scope 变更**：切换家庭时按「先加目标家、再移其余家」的顺序写 KV，保证切换过程中启用集不瞬时空掉（空集会触发兜底自动选家、扰动感知）；落库后再通知感知层 adapter 同步，无需重启服务。账号切换时清空所有家庭与摄像头 scope，回到干净状态。若启用集为空或无效，自动回退到首个可见家庭，避免感知全黑。

**切换家庭时重置 Agent 会话**：切换真正改变启用集时，`switch_home` 会后台 best-effort 触发一次 openclaw 侧 miloco agent 会话的重置（`agent_client.reset_agent_sessions` → 插件 `reset_sessions` webhook，见 [Agent 集成](openclaw-integration.md)），清掉旧家庭遗留在会话里的上下文（设备 / 房间 / 习惯），避免旧家庭上下文串入新家庭造成干扰。空切（重复选中当前已是唯一启用的家庭）跳过重置，以免白删仍有效的热上下文；`list_homes` 启用集为空时的兜底自动选家属同一 bug class，同样触发重置。整个重置纯后台 fire-and-forget，openclaw 不可达只 WARN、绝不阻塞或打断切换本身。待重置的会话集以 `MILOCO_SESSION_KEYS`（`dispatch/dispatcher.py`，由 `_ROUTE` 派生的唯一事实源）为准。

**在哪配置**：web 面板"概览"标签（摄像头在用切换）和顶部 TopBar 家庭切换器；也可通过 `miloco-miot-scope` Skill 在 CLI 完成。

### 关键设计决策

#### 控制写入固定走 Cloud HTTP

设备属性写入 / 查询 / 动作调用统一经 `MiotProxy` → `MIoTClient.http_client`（`MIoTHttpClient`，`backend/miot/src/miot/cloud.py`）发往小米云 HTTP API——不走局域网直连。`MIoTClient` 内的 LAN（`backend/miot/src/miot/lan.py`）/ mDNS 子模块用于局域网设备发现与在线状态维护，摄像头实时画面走 PPCS 串流（见 [live-camera-view](live-camera-view.md)），均不承载控制写入。SDK 各路径能力见 [sdk-miot.md](../05-external-deps/sdk-miot.md)。

**为什么 STATIC 规则绕过 MiotService**：规则在创建时已绑定 scope 内设备，MiotService 的 scope 校验是冗余的。STATIC 规则执行路径需要极低延迟（感知到设备响应），省掉 service 层的开销。

**Scope 为什么用 KV 而非配置文件**：Scope 是运行期可变的用户选择，不是静态配置。KV 表提供事务性单行原子写，读路径走内存缓存，变更即生效，与配置文件的"重启才生效"语义不同。

### 如果我要修改设备控制相关功能

| 修改目标              | 去看哪个文件                                                                     |
| --------------------- | -------------------------------------------------------------------------------- |
| 修改 scope 过滤逻辑   | `miot/filter.py`                                                                 |
| 修改 scope CRUD 逻辑  | `miot/service.py`（`switch_home` / `toggle_camera` / `list_cameras_with_state`） |
| 修改设备控制 API 端点 | `miot/router.py`                                                                 |
| 修改 MiOT SDK 封装层  | `miot/client.py`（MiotProxy），更底层看 `backend/miot/src/miot/`                 |
| 修改摄像头管理逻辑    | `miot/camera_handler.py`（`CameraVisionHandler`）                                |

### 设备控制相关 API 路径

主要入口：`POST /api/miot/devices/{did}/control`（控制设备），`GET /api/miot/device_list`（设备列表），完整端点见 `miot/router.py`。

### 与其他模块的关系

**上游**：`miloco-devices` Skill 通过 CLI 调 `/api/miot/devices/{did}/control`，是主要控制入口。`RuleRunner`（`rule/runner.py`）在 STATIC 规则条件满足时直接调用 `MiotProxy`。

**下游**：所有控制 / 查询 / 动作指令最终经 `MiotProxy` → `MIoTClient.http_client` 固定发往小米云 HTTP API，不走 LAN 直连（见上「控制写入固定走 Cloud HTTP」）。

**互动**：scope 变更（切换家庭 / 启停摄像头）后，`MiotService` 先按 `select_active_camera_dids` 口径重建 / 销毁 camera manager（停用 / 移出家庭的摄像头停掉 native 会话），再同步感知层 adapter 的投喂订阅，无需重启服务。OAuth 完成后，`MiotService` 主动重启感知引擎，让摄像头 adapter 重新注册帧回调。
