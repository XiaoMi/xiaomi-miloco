# MiOT SDK 依赖

## L1：它是什么

小米 MiOT SDK（`backend/miot/`，包名 `miloco-miot`）是 Miloco 访问小米智能家居生态的底层库。它封装了 OAuth2 认证、小米云 HTTP API、局域网 OT 协议直连、摄像头流媒体（C 库封装）、mDNS 局域网发现、MQTT 云端推送等能力。它作为独立版本化的包与 Miloco 同仓维护，Python 层源码在仓内（`backend/miot/src/miot/`）可读；仅摄像头流媒体依赖一个闭源预编译 C 库（`libmiot_camera_lite`，按平台仅提供 `.so`/`.dylib`）。

### 能力范围

- **OAuth2 + 云端 API**：账号授权、设备列表、属性读写、动作触发、场景执行、App 推送通知
- **设备能力解析（MIoT-spec）**：解析设备的 MIoT-spec 能力模型（服务 / 属性 / 动作 / 事件），标准库与设备类型目录拉取自 miot-spec.org，供上层理解设备可读写属性与可触发动作（`MIoTSpecParser`，`spec.py`）
- **LAN 发现（OT 协议）**：UDP 广播发现局域网 MiOT 设备，维护在线/离线状态；Miloco 侧仅用于设备发现与在线判定，不承载控制写入（控制统一走 Cloud HTTP，见 [设备控制](../03-features/device-control.md)）
- **摄像头流媒体**：通过 C 动态库（`libmiot_camera_lite.so`/`.dylib`）建立 PPCS P2P 连接，接收 H.264/H.265 视频流和 Opus / G.711 音频流
- **媒体解码**：基于 PyAV 将原始码流解码为视频帧与音频 PCM 采样
- **mDNS 发现**：基于 zeroconf 发现局域网 MiOT Central Service 节点，用于 MQTT 本地路由
- **MQTT**：通过 `MIoTMipsCloud`（`mips_cloud.py`）订阅四类推送事件——用户设备绑定 / 解绑、设备 meta 变更（含跨家庭移入）、家庭场景变更（改 / 删 / 重命名）、设备云端上线 / 离线状态。绑定与移入驱动设备欢迎，场景变更驱动场景列表刷新，上线 / 离线事件驱动缓存的摄像头在线状态刷新（事件化替代轮询），与米家保持同步

---

## L2：我们怎么用

### 封装层

SDK 对外的统一入口是 `MIoTClient`（`backend/miot/src/miot/client.py`），内部编排 OAuth2、云 API、LAN、mDNS、MQTT 推送与摄像头等子客户端。Miloco 在其之上再封装一层：`MiotProxy`（`backend/miloco/src/miloco/miot/client.py`），对整个 Server 暴露统一的异步接口。MiotProxy 负责：

- token 生命周期管理（后台自动刷新，SDK 本身不做）
- 内存缓存（设备/摄像头/场景列表），屏蔽 SDK 的直接 HTTP 调用
- 摄像头 scope 判定（启用家庭白名单 + 摄像头黑名单 + 在线 + 上限）收敛到单一选择口径，使 native 拉流集与感知投喂集共用同一判定、避免两套逻辑漂移（选择逻辑见 `miot/filter.py`）
- 摄像头实例（`CameraVisionHandler`，`miot/camera_handler.py`）生命周期管理

未绑定小米账号或 OAuth token 过期时，`MiotService` 抛 `MiotOAuthException`；调用失败时抛 `MiotServiceException`。

详细使用方式见 [设备控制](../03-features/device-control.md) 和 [感知流水线](../03-features/perception-pipeline.md)。

### 集成约束

- **OAuth 绑定要求**：所有 Cloud API 调用必须先完成小米账号 OAuth2 授权（`miloco-cli account bind`）。未绑定时设备列表为空，感知无法启动。
- **C 库依赖**：摄像头流依赖闭源预编译的 `libmiot_camera_lite.so`/`.dylib`，仅提供 Linux（x86_64 / aarch64）和 macOS（x86_64 / arm64）预编译版本，不支持交叉编译或源码定制。
- **PPCS UDP 穿透**：摄像头 P2P 连接依赖 UDP 入站，防火墙需要允许来自局域网的 UDP 包（常见问题，见 [故障排查 · 摄像头连接问题](../06-dev-guide/troubleshooting.md#摄像头连接问题)）。
- **单进程约束**：SDK 部分子模块（LAN daemon、摄像头 C 库绑定）假设单进程运行，是 Miloco Server `workers=1` 约束的原因之一（该约束还兼顾感知引擎等单实例组件，横向扩展走反代层）。

### 已知限制

- LAN 发现仅限同子网（OT 广播 UDP 无法跨路由器）
- 摄像头在线判定依赖 SDK 内部状态，设备实际离线时状态可能未及时更新

### 出问题找谁

MiOT SDK 作为独立包（`miloco-miot`）由小米团队维护，Python 层源码在仓内（`backend/miot/`）可读可查；摄像头依赖的 C 库 `libmiot_camera_lite` 为闭源预编译产物，工程侧不持有其源码。遇到 C 库层问题（崩溃、PPCS 连接异常）需向 SDK / C 库维护团队反映；Python 层问题可直接在仓内定位。Miloco 侧对上层的隔离与降级通过 `MiotProxy` 封装层实现。
