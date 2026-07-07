<h1 align="center">Xiaomi Miloco</h1>

<p align="center"><a href="README.md">English</a> | 简体中文</p>

小米面向未来的全屋智能 AI 开源方案，以米家摄像头的画面与声音为全模态感知入口，以自研 MiMo 大模型为智能大脑，以 Agent 插件形式运行在 [OpenClaw](https://openclaw.ai) 之上，联动全屋设备带来主动智能体验。

Miloco 2.0 能感知家中发生的事件，能基于常识主动判断并操控设备，能将"模糊又长期"的目标拆解成可追踪的家庭任务，能识别家庭成员、依托家庭记忆为每位成员提供个性化服务——查询和控制设备、把家调到成员舒适的状态，或在合适的时机给出有用的提醒。

<p align="center"><a href="https://www.bilibili.com/video/BV1fALo6hEkc"><img src="assets/video_cover.png" width="600" alt="Xiaomi Miloco 视频介绍" /></a></p>

## 最新动态

- **2026-07-03** — 发布 v2026.7.3：新增事件反馈打包，以及全新安装时主动发起的对话式初始化引导；并改进面板内模型配置管理、感知稳定性（非人误检防护）、相机生命周期与 CLI 诊断。
- **2026-06-18** — Miloco 2.0 正式发布：重构为 OpenClaw 插件，新增通用常识、身份识别、家庭记忆、家庭任务、主动智能、家庭面板。详见下方[核心特性](#核心特性)。

## 核心特性

- **通用常识** — 无需预设规则，基于系统内建的通用常识自动识别危险隐患并分级预警（如孩子玩刀具、老人跌倒）。
- **身份识别** — 融合人脸、体态等身份信息，由大模型实现家庭成员的身份识别，支持主动注册新成员，以及基于身份的个性化操作。
- **家庭记忆** — 从感知与交互中沉淀家庭成员的长期习惯与偏好，作为 Agent 主动决策时的参考依据；长期稳定的习惯还可主动提醒，或升级为家庭任务自动执行。
- **家庭任务** — 从单一的「条件触发规则」升级为可长期运转的复杂家庭任务：条件自动化（"有人进门就开灯"）、定时提醒（"每天提醒吃药"）、习惯统计（"每天运动半小时"）等，触发后由 Agent 理解意图并自主执行。
- **主动智能** — 以通用常识、身份识别、家庭记忆、家庭任务四大能力为基础，让系统像有常识、懂家人、会规划的管家一样主动观察、判断并适时干预，在用户开口前把事做好。
- **家庭面板** — 面向用户的 Web 面板，查看家中实时概览、米家设备、家庭成员与家庭档案、历史事件日志。

> [!TIP]
> **养成你自己的 Miloco。** 它的初始表现未必合你心意——直接通过 OpenClaw 告诉 Miloco（如"家里乱不用提醒我"），它就记住你的偏好、相应调整主动行为。你每说一句，就是在"养成"一个更懂你家的 Miloco，越用越贴心。

## 前置条件

- **硬件**：建议内存 ≥ 4GB，存储 ≥ 256GB，7×24 常驻运行，推荐 Mac mini
- **操作系统**：macOS / Linux（Windows 请在 WSL 中运行）
- **OpenClaw** — Miloco 以插件形式运行其上，需先[安装](https://openclaw.ai)且版本 ≥ 2026.5.2
- **小米账号** + 已接入米家的设备
- **多模态大模型 API Key** — 推荐使用[小米 MiMo](https://platform.xiaomimimo.com)：感知用 MiMo-v2.5，Agent 用 MiMo-v2.5-pro（在 OpenClaw 中配置）

> [!CAUTION]
> **成本提示**：Miloco 2.0 的感知与 Agent 主要依赖云端大模型，会持续产生 API 调用费用，请关注用量。可在家庭面板「模型」页查看 token 消耗。

## 安装

### 方式一：通过 Agent 安装（推荐）

向 OpenClaw 发送以下指令即可自动完成安装：

```text
帮我安装 Miloco 插件：https://raw.githubusercontent.com/XiaoMi/xiaomi-miloco/main/scripts/install-guide.md
```

### 方式二：命令行一键安装

```bash
curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash
```

### 方式三：从源码构建

在项目根目录执行：

```bash
bash scripts/install.sh --dev   # 从源码构建（scripts/build.sh）后本地安装
```

---

### Windows（WSL）

无论选用上面哪种方式，都暂不支持原生 Windows，请在 [WSL](https://learn.microsoft.com/zh-cn/windows/wsl/install) 中安装并运行。

> [!IMPORTANT]
> **本地拉流需额外配置 WSL 网络。** 家庭面板「家里此刻」的实时画面靠局域网拉取摄像头流，而 WSL 默认 NAT 模式会拦截摄像头发来的 UDP 包——不配置则画面加载不出来，需启用镜像网络模式并放行 Hyper-V 防火墙。

1. **在 Windows 侧** —— 在 `%USERPROFILE%\.wslconfig`（即 `C:\Users\<你的用户名>\.wslconfig`，没有则新建）中加入以下内容，再在 PowerShell 执行 `wsl --shutdown` 重启 WSL：

   ```ini
   [wsl2]
   networkingMode=mirrored
   ```

2. **在 Windows 侧（管理员 PowerShell）** —— 放行入站流量：

   ```powershell
   Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
   ```

3. **在 WSL 内** —— 装好 Miloco 后执行 `miloco-cli doctor` 验证（会检查防火墙与 WSL 网络配置）。

## 快速开始

安装完成后，先重启 OpenClaw 网关让插件生效：

```bash
openclaw gateway restart
```

随后打开家庭面板完成首次配置：

```bash
miloco-cli dashboard   # 在浏览器打开家庭面板（或直接访问 http://<host>:1810/）
```

在面板中三步即可上手：

1. **配置模型** — 在「模型」页填入 MiMo 的 api_key（安装时已填则跳过）；模型名与 Base URL 默认即 MiMo，无需改动。
2. **绑定小米账号** — 绑定后自动拉取米家设备。
3. **开启摄像头感知** — 在「概览」页为需要感知的摄像头打开开关（其余保持关闭，不会被分析）。

也可改用命令行完成：

```bash
miloco-cli config set model.omni.api_key sk-xxx   # 配置模型密钥（默认即 MiMo，通常只需这一项）
miloco-cli account bind                           # 绑定小米账号
miloco-cli scope camera enable <did>              # 开启指定摄像头感知
```

跑起来之后，日常怎么用见 [使用说明书](user_guide_zh.md)。

## 项目结构

```text
miloco-plugin/
├── backend/             # uv workspace
│   ├── miloco/          # 主服务：感知引擎、规则、MIoT 网关
│   └── miot/            # MIoT SDK（独立子包）
├── cli/                 # miloco-cli 命令行工具
├── plugins/
│   ├── openclaw/        # OpenClaw 插件（TypeScript）
│   └── skills/          # Agent Skill 文档
├── web/                 # 家庭面板（React 19 + Vite）
├── scripts/             # build.sh / install.sh / manifest.json
└── knowledge/           # 项目知识库
```

## 深入文档

- [后端服务](backend/README.md) — FastAPI + 感知引擎 + 规则 + MIoT 网关
- [命令行 miloco-cli](cli/README.md) — 服务、设备、配置管理
- [家庭面板 web](web/README.md) — 部署架构与本地开发
- [完整知识库](knowledge/README.md) — 架构 / 模块 / 功能 / API 速查

## 交流群

遇到问题、想反馈或交流玩法，欢迎扫码加入飞书用户群（二维码永久有效）：

<img src="assets/Xiaomi_Miloco_Feishu_Group.png" width="240" alt="Xiaomi Miloco 用户群" />

## 致谢

Miloco 站在以下开源项目之上：

- [OpenClaw](https://openclaw.ai) — AI Agent 运行时与插件平台
- [jMuxer](https://github.com/samirkumardas/jmuxer)（MIT）— 家庭面板实时视频流封装
- [BGE / bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5)（智源研究院，MIT）— 文本向量化模型
- [Silero VAD](https://github.com/snakers4/silero-vad)（Silero Team，MIT）— 语音活动检测，门控感知语音字段

## 许可证

完整许可条款见 [LICENSE.md](LICENSE.md)。

**重要声明**：本项目仅限非商业用途。未经小米公司书面授权，不得用于开发应用程序（APP）、Web 服务或其他形式的软件。
