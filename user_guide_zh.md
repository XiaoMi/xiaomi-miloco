# Miloco 2.0 使用说明书

Miloco 2.0 是基于 [OpenClaw](https://openclaw.ai) 的小米全屋智能 AI 开源方案——一个会观察、能琢磨、越住越懂你家的 AI 管家。

本手册聚焦"装好之后怎么用"。产品定位、完整特性与安装详情见 [README](README.zh.md)；下面先简单认识它，再上手使用。

# 一、认识 Miloco 2.0

## 1. Miloco 2.0 是什么

Miloco 2.0 以米家摄像头的画面与声音为全模态感知入口，以自研 MiMo 大模型为智能大脑，以 Agent 插件形式运行在 [OpenClaw](https://openclaw.ai) 之上：感知家中发生的事、基于常识主动判断、联动米家设备执行，并依托身份识别与家庭记忆为每位成员提供个性化服务。

## 2. 五大主要特性

Miloco 的能力由五项特性构成：**通用常识、身份识别、家庭记忆、家庭任务、主动智能**。每项的详细说明见 [README · 核心特性](README.zh.md#核心特性)。

## 3. 两大核心功能

五大特性各司其职，最终汇成两大核心功能：

- **基于常识和记忆的主动智能**：结合通用常识、身份识别与家庭记忆，持续观察家中状况——识别到危险或异常会分级预警，识别到家人会按其偏好个性化服务，无需逐条配置规则，开箱即用，越用越懂。
- **家庭复杂任务**：把"模糊又长期"的目标（每天喝 8 杯水、久坐提醒、按时吃药）拆解成可追踪的家庭任务，自动计数、统计进度、按时提醒，由 Agent 理解意图并自主执行。

这两大核心功能的具体用法，见下文第二章「开始使用」。

# 二、开始使用

日常使用只有一条核心心法：**想做什么，直接告诉 OpenClaw**。认人、记习惯、管任务、按常识主动照看……这些能力（详见第一章）它会自己判断、自己调用，你不用记命令，也不用关心背后是哪一项在起作用。

> [!TIP]
> **养成你自己的 Miloco。** 它的初始表现未必合你心意——直接通过 OpenClaw 告诉 Miloco（如"家里乱不用提醒我"），它就记住你的偏好、相应调整主动行为。你每说一句，就是在"养成"一个更懂你家的 Miloco，越用越贴心。

## 1. 想做什么，直接告诉它

照着下面的例子做就行：

- **让它认识家人**："添加一个家庭成员，叫王磊，是爸爸"、"让它认识我"、"家里登记了哪些人？"

  ![让它认识家人](assets/chat_add_member.png)

- **记住偏好**："记住，爸爸不喜欢灯太亮"、"记住，我们家一般 11 点睡觉"

  ![记住偏好](assets/chat_remember_pref.png)

- **调教主动提醒**："以后家里乱不用特意提醒我"、"别再提醒我整理东西了" —— 告诉它你**不想要**（或想要）哪类主动提醒，它会把这条记进家庭档案；之后摄像头感知时就照此判断，相应的提醒不再触发。**你随口一句，就是在"养成"它**——越用越懂你，越用越少打扰。

  ![调教主动提醒](assets/chat_tune_reminder.png)

- **养习惯 / 打卡**："每天喝 8 杯水"、"每天保证锻炼一次"

  ![养习惯 / 打卡](assets/chat_build_habit.png)
- **定提醒**："30 分钟后提醒我看炉子"、"工作日早上 7 点叫我起床"
- **追踪时长**："孩子玩 iPad 连续 1 小时提醒我"、"久坐 50 分钟提醒我活动"
- **查询 / 控制设备**："客厅有人吗？"、"把卧室灯关掉"

说完它会用大白话跟你确认。比如你说"每天喝 8 杯水"，它大概会这样回你：

> 搞定了！每天 10、12、14、16、18、20 点我会提醒你喝水，每天凌晨自动重置进度。

## 2. 管理与回看

- **管理任务**："我现在有哪些任务？"、"暂停喝水提醒"、"把喝水目标改成 10 杯"、"不用记录喝水了"
- **回看它做了什么**：打开 Web 面板「日志」页（见第三章），能看到每一次"感知 → 判断 → 处理"的完整记录。

**注意**：习惯跟踪 / 行为计数这类任务依赖摄像头感知；家里若没有摄像头、也没有带麦克风的设备，这类任务无法创建。纯定时提醒（如"明天 8 点叫我"）则不需要摄像头。

# 三、Web 家庭面板

除了对话，Miloco 还提供一个 Web 面板，在手机、平板、电脑上都能直接查看和操作。

## 1. 怎么访问

- **方式一**：后端服务启动后，浏览器打开 `http://127.0.0.1:1810/`（本机）；同一 WiFi 下的其他设备访问 `http://<主机 IP>:1810/`。
- **方式二**：终端执行 `miloco-cli dashboard`，自动在浏览器打开面板，无需手动输地址。

## 2. 面板能做什么

- **概览**：一键观看在线摄像头「家里此刻」的实时画面，各平台浏览器通用。

  ![概览页](assets/panel_overview.png)

- **设备**：按房间一眼看清家里全部设备状态。

  ![设备页](assets/panel_devices.png)

- **家庭**：身份注册、家庭档案 / 成员习惯、家庭任务、Dreaming 记忆等信息可直接在网页管理。

  ![家庭页](assets/panel_family_1.png)

  ![家庭页](assets/panel_family_2.png)

- **日志**：「家庭今天发生了什么」，按时间线回看每一次"感知 → 判断 → 处理"的完整记录。

  ![日志页](assets/panel_logs.png)

- **模型**：模型的选择、用量、性能数据可直接查看（今日 token 总量、模型构成、消耗类型、时间分布等）。

  ![模型页](assets/panel_models_1.png)

  ![模型页](assets/panel_models_2.png)

# 四、命令速查

日常使用以对话为主，下面这些命令只在排查问题或手动操作时才用得上。`miloco-cli` 共 16 个命令组、近 100 条子命令，绝大多数日常操作直接对管家说即可。

## 1. 服务管理

```bash
miloco-cli service start | stop | restart | status
miloco-cli service logs -f          # 实时日志
```

## 2. 账号

```bash
miloco-cli account status           # 绑定状态
miloco-cli account bind             # 发起绑定
miloco-cli account authorize "<授权码>"
miloco-cli account unbind           # 解绑
```

## 3. 配置

```bash
miloco-cli config show              # 查看配置（密钥打码）
miloco-cli config set model.omni.api_key sk-xxxx
miloco-cli config get model.omni.model
miloco-cli config list-paths        # 列出所有可配置项
```

## 4. 设备

```bash
miloco-cli device list              # 设备列表（可加 --room / --category / --online）
miloco-cli device spec <did>        # 看设备能力规格
miloco-cli device control <did> --set <iid> <值>
```

## 5. 成员与感知

```bash
miloco-cli person list              # 成员列表
miloco-cli person add --name "小明" --role "爸爸"
miloco-cli perceive devices         # 列出感知设备
miloco-cli perceive query --source <did> --query "客厅有人吗"
```

### perceive 鉴权

`miloco-cli perceive` 命令（包括 `devices` / `query` / `logs`）需要 `server.token` 配置才能正常访问后端 API。首次启动后端时会自动生成一个随机 token 写入 `~/.openclaw/miloco/config.json`，无需手动设置。

如果遇到访问 perceive 失败的情况，按下述顺序排查：

1. CLI 报 `cannot connect to Miloco backend at ...` → 后端没启，启动后端即可（首次启动时后端会自动生成 `server.token` 写入 config.json）。
2. CLI 报 `401 Unauthorized` → 后端在跑但 token 不匹配：
   - 打开 `~/.openclaw/miloco/config.json` 确认 `server.token` 非空；
   - 确认 CLI 与后端读到的是同一份配置（未通过 `MILOCO_HOME` 切到其它目录，也未通过 `MILOCO_SERVER__TOKEN` 单边覆盖）；
   - 若曾手改过其中一端的 token，把两端改回一致并重启后端。

## 6. 规则与通知

```bash
miloco-cli rule list                # 查看自动化规则
miloco-cli notify push --text "测试推送"   # 米家 App 推送
miloco-cli scope camera list        # 摄像头黑白名单
```

# 五、附录

## 1. 许可说明

本项目仅限非商业用途。未经小米公司书面授权，不得用于开发应用、Web 服务或其他形式的软件。完整许可见仓库 [LICENSE.md](LICENSE.md)。

## 2. 相关资源

- 代码仓库：[github.com/XiaoMi/xiaomi-miloco](https://github.com/XiaoMi/xiaomi-miloco)
- OpenClaw 官网：[openclaw.ai](https://openclaw.ai)
- MiMo 模型平台：[platform.xiaomimimo.com](https://platform.xiaomimimo.com)

---

本说明书会随 Miloco 2.0 持续更新。遇到未覆盖的问题，最快的方式永远是——直接问你的 AI 管家。
