# 设备欢迎

## 背景与目标

用户在米家 App 新绑定了一台设备——一台空气净化器、一个智能插座、一个新摄像头。用户希望知道 Miloco 是否已经"认识"了这个新设备，是否可以对它发出指令了。

设备欢迎能力让这个确认过程自动发生：用户在米家 App 完成绑定后，Miloco 在数秒内感知到，并通过现有的音箱/屏幕等终端用 TTS 主动告知——"已为您接入了小米空气净化器"——建立"设备已被 Miloco 识别接管"的信任感，无需用户主动检查。

---

## 产品面

### 能做什么

- **自动感知到达**：设备新绑定、或从别的家庭移入受管家庭后，Miloco 数秒内收到通知
- **TTS 主动播报**：通过现有音箱或屏幕设备播报欢迎语，告知用户新设备已接入
- **摄像头自动接入**：新到达的摄像头无需重启服务，感知流水线自动接入

### 典型场景

**场景 — 新设备接入确认**：用户在书房安装了新的小米台灯，用米家 App 完成配网绑定。几秒后，客厅音箱播报"已为您接入小米台灯，您可以说'打开书房台灯'"。用户不用打开 Miloco 面板查看，直接知道新设备已就位，随时可以用语音控制。

### 触发条件

- 用户在米家 App 完成设备绑定（bind 事件），或把设备移入受管家庭（hr_change 事件）
- 设备归属于 Miloco 当前启用的家庭（scope 内）
- 设备 did 在 `refresh_devices` 后确认存在于账号下（排除绑定抖动）

### 能力边界

- 欢迎播报由 `miloco-notify` Skill 路由，TTS 设备选择（同房间优先等逻辑）在 notify Skill 中实现，不在本模块
- 若新绑定的设备是摄像头，感知流水线自动接入新设备（无需重启服务）
- 设备解绑不触发欢迎播报
- MQTT 连接状态可通过 `GET /api/miot/mips_status` 查询，用于排查欢迎功能不触发

---

## 研发面

### 架构概览（数据流图）

设备"到达"受管家庭有两条路径——新绑定（bind push）或从别的家庭移入（hr_change push）。两条路径各自防抖，最终都委托同一个欢迎动作。

```
米家 App 绑定 / 移入设备
  → 小米 MQTT broker 推送 bind/unbind 或 hr_change 事件
  → MIoTMipsCloud（backend/miot/src/miot/mips_cloud.py）
  → MiotProxy（miot/client.py）
  → 对应监听器（miot/mips_listeners.py，trailing-edge 防抖；MQTT payload 非权威）
      BindEventListener（bind 路径）/ DeviceMetaEventListener（home-move 路径）
      → 拉云端终态（refresh_devices）→ present 判断
      → 委托 welcome(did)
  → DeviceWelcomeService（miot/welcome_service.py）
      scope gate（设备在启用家庭内）+ 跨路径去重 + 构造欢迎消息
      → dispatch_event("bind", ...)
  → AgentDispatcher（dispatch/dispatcher.py）
      bind 事件复用主交互会话
  → run_agent_turn → OpenClaw Webhook
  → Agent 调 miloco-notify Skill 播报欢迎语
```

### 核心模块

| 类                        | 文件                                  | 职责                                                                                                                                                                         |
| ------------------------- | ------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MIoTMipsCloud`           | `backend/miot/src/miot/mips_cloud.py` | 小米 MQTT broker 推送入口：订阅用户 bind / 设备 meta / 场景事件主题，是 bind / hr_change push 的源头（MQTT 连接与协议细节见 [sdk-miot.md](../05-external-deps/sdk-miot.md)） |
| `BindEventListener`       | `miot/mips_listeners.py`              | bind 路径：trailing-edge 防抖 → 拉云端终态 → present 判断（present 委托 welcome，absent 即 unbind 丢弃）                                                                     |
| `DeviceMetaEventListener` | `miot/mips_listeners.py`              | home-move 路径：全局防抖刷新设备/摄像头/场景；仅问候被 proxy 标记为「移入受管家庭」的 did，改名 / 域内移动只刷新不问候                                                       |
| `DeviceWelcomeService`    | `miot/welcome_service.py`             | 欢迎动作本体：scope gate、跨路径去重（一次到达若同时触发 bind 与 hr_change 只播报一次）、构造欢迎消息、调 `dispatch_event("bind", ...)`                                      |
| `MiotProxy`               | `miot/client.py`                      | 持有各 listener 与 `DeviceWelcomeService` 生命周期，把 MQTT push 转给对应 listener；`hr_change` 的「是否移入受管家庭」判定（`_is_move_into_scope`，需家庭白名单）在此完成    |
| `AgentDispatcher`         | `dispatch/dispatcher.py`              | bind 事件复用主交互会话（自成合并类型、单飞不混入同一 turn），经 `run_agent_turn` 投递给 OpenClaw；bind 不计入 `agent_runs` 统计                                             |

### 关键设计决策

**防抖设计意图**：用户常见操作是 unbind 后立即 rebind（配错再改）。trailing-edge 防抖等到操作稳定后再上报终态，避免中间状态（设备已解绑）触发不必要的欢迎语或误导性提示。MQTT payload 不是权威，必须 `refresh_devices` 拉云端确认终态。

**欢迎动作独立成 Service**：欢迎被 bind 与 home-move 两条路径共享，故把动作本体抽到 `DeviceWelcomeService`，两个 listener 只管"防抖 + 刷新 + present 判断"后委托 `welcome(did)`，listener 不各自携带欢迎逻辑。`DeviceWelcomeService` 本身对刷新 / 防抖无状态，只保留一个短去重窗口——同一次到达若同时触发 bind 与 hr_change（各自在自己链路防抖），只播报一次。

**摄像头绑定的级联处理**：bind 防抖落定后，listener 统一刷新设备 / 摄像头 / 场景列表（不区分设备类型）；新到达的摄像头由此被纳入摄像头列表，感知层 adapter 在后续 sync 时自动接入，无需重启服务。

**meta 订阅必须账号全域**：`_sync_meta_subscriptions` 对全账号设备订阅设备 meta 主题，**不**按受管家庭裁剪——否则一台位于域外家庭的设备根本没被订阅，把它移入受管家庭的 `hr_change` 就无从听见，home-move 欢迎路径便失效。受管家庭 scope 只在欢迎那一步施加（`_is_move_into_scope` / `DeviceWelcomeService`），不施加于订阅。（场景订阅相反，是按受管家庭 scope 过滤的：场景没有「移入受管家庭」这一语义。）

### 如果我要修改设备欢迎相关功能

| 修改目标                             | 去看哪个文件                                                            |
| ------------------------------------ | ----------------------------------------------------------------------- |
| 修改防抖 / present 判断逻辑          | `miot/mips_listeners.py`（BindEventListener / DeviceMetaEventListener） |
| 修改欢迎消息格式 / scope gate / 去重 | `miot/welcome_service.py`（DeviceWelcomeService）                       |
| 修改 MQTT 连接参数                   | `backend/miot/src/miot/mips_cloud.py`（MIoTMipsCloud）                  |
| 查看 MQTT 连接状态                   | `GET /api/miot/mips_status`（`miot/router.py`）                         |

### 与其他模块的关系

**上游**：MiOT SDK（`MIoTMipsCloud`）接收小米 MQTT broker 的 bind / hr_change push，是整条链路的源头。`MiotProxy` 持有各 listener 并将 push 转发给对应监听器。

**下游**：`AgentDispatcher` 将 bind 事件投递给 OpenClaw，Agent 经 `miloco-notify` Skill 播报。若为摄像头：感知层 adapter 同步，流水线自动接入。

**共享**：设备欢迎不依赖感知流水线（欢迎消息路径独立），仅通过 bind 事件 + 云端设备列表工作。
