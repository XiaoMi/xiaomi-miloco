# 实时摄像头观看

## 背景与目标

用户想随时查看家里摄像头的实时画面——孩子是否安全回到家、老人状态如何、有没有陌生人闯入。传统方式需要打开米家 App，在众多设备中找到摄像头，且依赖手机 App 安装。

Miloco 让用户通过浏览器直接看到家中摄像头的实时画面，无需安装任何 App，无需设备配对——在家庭面板的浏览器标签页里打开即可。

---

## 产品面

### 能做什么

- **跨平台无插件**：支持任意现代浏览器（Linux Chrome / macOS Safari / iOS Safari / Android Chrome），无需安装额外组件
- **多标签低 CPU 复用**：多个浏览器标签页同时观看同一摄像头，编码仅发生一次，额外订阅者只增加网络转发开销，不增加 CPU
- **与感知流水线共解码**：直播和感知流水线共用同一次解码，不额外占用摄像头资源
- **多摄像头支持**：家庭面板可切换查看不同摄像头和 channel

### 典型场景

**场景 1 — 下班前确认**：父母下班路上，在手机浏览器打开 Miloco 家庭面板，切到摄像头直播，确认孩子已回家、安全做作业。无需 App，扫码或书签即可访问。

**场景 2 — 多标签同时监看**：家庭面板在电脑上开多个标签，分别观看客厅和卧室摄像头。两路直播同时运行，但 CPU 占用不成倍增加，因为同一摄像头只做一次 H.264 编码。

### 能力边界

- 不支持 PTZ 控制或双向语音
- 实时观看依赖摄像头与 Miloco 服务在同一局域网；PPCS P2P 连接需要 UDP 入站（防火墙配置见 [故障排查 · 摄像头连接问题](../06-dev-guide/troubleshooting.md#摄像头连接问题)）
- 不支持录制或历史回放（有价值事件的视频片段另有 meaningful_events 机制保存）
- 不支持跨局域网/公网访问，需摄像头和服务在同一网络

---

## 研发面

### 架构概览（数据流图）

```
GET /api/miot/watch → watch.html（server.token 注入）
  → 浏览器 WebSocket 接入 /api/miot/ws/video_stream?camera_id=...&channel=...
  → MIoTVideoStreamManager（miot/ws.py）
      第一个订阅者 → 创建 H264LiveEncoder（miot/transcoder.py）+ 注册帧回调
      后续订阅者  → 复用已有编码输出
  → H264LiveEncoder（统一重编为浏览器兼容 H.264 NAL 流）
  → WebSocket 推给浏览器
  → 浏览器解码渲染：WebCodecs VideoDecoder（secure context）或 MSE+jmuxer（LAN HTTP 回退）
```

解码层由 MiOT SDK PyAV 完成（摄像头原始码流 → BGR ndarray）。感知流水线和直播通过 `start_camera_decode_video_stream`（`multi_reg=True`）共用同一次解码，各自独立回调，互不干扰。

### 核心模块

**`/api/miot/watch` 端点**（`miot/router.py::watch_page`）

入口端点：把 `server.token` 注入 `watch.html` 模板（运行期在 `static_dir`，源文件为 `web/public/watch.html`）后返回，让页面无需用户手动粘贴 token 即可自足启动。浏览器收到注入 token 的页面后，用 token 调 `/api/perception/devices` 拉摄像头列表，用户选择后通过 WebSocket 接入视频流。同一页面还支持 `embedded=1` 模式：`camera_id`/`channel` 由 URL 直接传入、隐藏选择器与页面 chrome，供家庭面板以 iframe 内嵌复用（HeroNow 实时卡、可展开播放器均如此），播放器只有这一份实现。

**信任边界**：`/api/miot/watch` 响应体内嵌明文 token，等价于"能访问该 URL 的人拥有 token"。默认仅监听 `127.0.0.1`；若开放 LAN 访问，应自行评估网络可信边界。`server.token` 未配置则返回 `503`。

**MIoTVideoStreamManager**（`miot/ws.py`）

管理所有 WebSocket 订阅者。每个摄像头持有一个 `H264LiveEncoder` 实例；第一个订阅者触发编码器创建和帧订阅，后续订阅者直接复用已编码输出。订阅新旧交替、SDK 启停在每个摄像头一把 `asyncio.Lock` 下串行，避免并发首订阅者互相踩坏 reg_id/编码器。管理器只对外暴露 `has_emitted_frame`（是否已广播过首帧）；真正的首帧超时检测在 WS 路由侧（`video_stream_websocket` 起的看门狗，`miot/router.py`）：等满首帧超时窗口后检查 `has_emitted_frame`，若期间一帧都没出（跨局域网 / PPCS 中继未建立等），向前端发送明确的 error 信令后关闭连接，避免用户看到"正在连接"一直等下去。

**H264LiveEncoder**（`miot/transcoder.py`）

将 SDK 解码出的 BGR 帧重编为浏览器普遍兼容的 H.264 NAL 流（限定 level 上限以保证兼容，具体见 `transcoder.py`）。WebSocket 新连接先发 init 消息（含编码格式信息），后续为视频帧数据。

### 关键设计决策

**为什么不直传原始码流而要重编**：摄像头原始码流可能是 H.264 或 H.265（HEVC）。H.265 在部分浏览器/系统中受专利限制无法直接播放；不同平台对 H.264/H.265 的硬解支持差异很大，直传需要逐平台适配。统一重编为浏览器普遍兼容的 H.264（限定 level 上限），任何现代浏览器都支持。代价是额外的 CPU 开销和一轮解码+编码的延迟。

**浏览器端解码双路径**：secure context（HTTPS / localhost）下用 WebCodecs `VideoDecoder` API，通过多档 `hardwareAcceleration` 轮试确保兼容性，解决 Linux Chrome / VAAPI 过度乐观导致运行时失败的问题。非 secure context（如 LAN HTTP 访问）下 WebCodecs 不可用，回退到 MSE + jmuxer（`/vendor/jmuxer.min.js`）：把 Annex-B NAL 重封为 fmp4 喂给 `<video>` 播放。

**直播与感知引擎解耦、与相机启停同源**：直播复用的 native 会话（camera manager，含 PPCS 会话 + 解码）与感知投喂**共用同一套「活跃相机」选择口径**（启用家庭 + 未拉黑 + 在线，且受并发相机上限约束，见 `miot/filter.py::select_active_camera_dids`）——拉流集与投喂集同源、不漂移。由此产生两条边界：Omni 感知引擎暂停/停止不销毁 native 会话，正在进行的直播照常；但把某相机停用（关闭感知）或移出当前家庭，会真正销毁其 native 会话（停 PPCS + 停解码），其直播随之中断（离线或超出并发上限的相机同理不建会话）。native 会话的建/销在 `refresh_cameras`（`miot/client.py`）里统一编排，每台单独兜异常，避免批量销毁时一台失败拖垮其余。

### 如果我要修改直播相关功能

| 修改目标                              | 去看哪个文件                                               |
| ------------------------------------- | ---------------------------------------------------------- |
| 修改 WebSocket 订阅/管理逻辑          | `miot/ws.py`（MIoTVideoStreamManager）                     |
| 修改编码参数或 WebSocket 帧格式       | `miot/transcoder.py`（H264LiveEncoder）                    |
| 修改 watch.html 页面或 token 注入逻辑 | `miot/router.py`（`watch_page`）和 `web/public/watch.html` |
| 修改浏览器端解码逻辑                  | `web/public/watch.html` 内的 JavaScript 部分               |

### 与其他模块的关系

**上游**：`MIoTVideoStreamManager` 通过 `MiotService.start_video_stream` / `stop_video_stream` 管理 SDK 订阅生命周期。

**共享**：感知流水线和直播共用 `start_camera_decode_video_stream`（`multi_reg=True`）的解码层，两者互不干扰——感知引擎不运行时，直播仍可正常工作。
