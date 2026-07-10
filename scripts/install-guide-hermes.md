---
name: install-miloco-hermes
description: Miloco Hermes Agent 兼容层安装引导。
metadata:
  fork: https://github.com/n0tssss/xiaomi-miloco
  branch: pr-hermes
  last_updated: 2026-07-09
---

# Miloco × Hermes 安装指南

把 miloco 装到一台**已装 Hermes Agent** 的机器上。fork 自 XiaoMi/xiaomi-miloco PR #279。

## 一行安装

```bash
git clone https://github.com/n0tssss/xiaomi-miloco.git -b pr-hermes ~/xiaomi-miloco && cd ~/xiaomi-miloco && bash plugins/hermes/install-hermes.sh
```

> HTTPS 超时可改用 SSH：`git clone git@github.com:n0tssss/xiaomi-miloco.git -b pr-hermes ~/xiaomi-miloco`

装完跑验证：

```bash
bash plugins/hermes/tests/test_acceptance.sh
```

## 引导式流程（agent 读）

### Step 1：环境准备（agent 自跑，全程不问用户）

依次执行，每步看 exit code。

```bash
# 1.1 确认 hermes
command -v hermes || curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

# 1.2 安装 miloco-cli + backend（从 fork 源码）
if ! command -v miloco-cli >/dev/null 2>&1; then
  cd "$(git rev-parse --show-toplevel)" && uv tool install ./cli --force 2>&1 | tail -3
fi
command -v miloco-cli || { echo "miloco-cli 未安装成功"; exit 1; }
# 安装 backend 包
cd "$(git rev-parse --show-toplevel)" && uv pip install -e backend/miloco/ 2>&1 | tail -3 || pip install -e backend/miloco/ 2>&1 | tail -3

# 1.3 clone + 安装（HTTPS 不通则配置 SSH 替换）
git config --global url."git@github.com:".insteadOf "https://github.com/" 2>/dev/null; true
git clone https://github.com/n0tssss/xiaomi-miloco.git -b pr-hermes ~/xiaomi-miloco
cd ~/xiaomi-miloco
bash plugins/hermes/install-hermes.sh
```

成功标志：exit 0 + `✅ 安装完成`。

**Step 1 完成后，agent 先自己绑定通知渠道。不要问用户，直接做：**

如果当前对话来自 IM 平台（飞书/微信/Telegram），agent 用自己的平台名调 miloco_notify_bind：
```
miloco_notify_bind action=switch target=<当前平台>
```
CLI 终端对话则跳过这步。

然后直接进 2.1。

### Step 2：收集用户配置（逐个询问，不可一次贴完）

此步骤 **不运行脚本**，由 agent 直接与用户交互。

> **关键纪律：必须按 2.1 → 2.2 → 2.3 顺序逐项询问。每项完成（用户回复"好了"/"ok"/"继续"）才发下一项。禁止一次性贴出全部步骤。**

agent 先跑：
```bash
miloco-cli account status
```

根据输出判定：

**已绑定（输出含 `"is_bound": true`）：**
> 当前已绑定米家账号：{user_info.nickname 或 user_info.uid}
> 是否继续使用当前账号？还是重新绑定？
>
> - 继续使用 → 进 2.2
> - 重新绑定 → 给用户 {bind_url}

**未绑定：**
> Miloco 需要绑定小米账号才能控制智能设备。
> 我先给你拿授权链接……

agent 跑：
```bash
miloco-cli account bind 2>&1 | grep -o 'https://[^ ]*'
```

取到的 URL 发给用户：
> 请在浏览器中打开这个链接登录小米账号授权：
> 
> {链接}
>
> 授权完后，把页面上显示的**授权码（base64 字符串）复制给我**。
>
> ⚠️ 授权码 5 分钟过期，请尽快操作。

用户回复授权码后，agent 跑：
```bash
miloco-cli account authorize "<用户给的授权码>"
```

验证通过后进 2.2。

#### 2.2 Omni 模型

agent 先跑：
```bash
miloco-cli config get model.omni.api_key
miloco-cli config get model.omni.model
miloco-cli config get model.omni.base_url
```

根据输出判定：

**三项都非空：**
> 当前模型配置：
> - Model: {model.omni.model 的值}
> - Base URL: {model.omni.base_url 的值}
> - API Key: {前4位}****{后4位}
> 
> 是否沿用当前配置？还是使用新的模型服务？
>
> - 沿用 → 进 2.3
> - 重新配置 → 收集新信息

**任一项为空，发：**
> Miloco 感知引擎需要一个多模态大模型来看懂摄像头画面。
> 默认推荐 **小米 MiMo**。
> - Model: `xiaomi/mimo-v2.5`
> - Base URL: `https://api.xiaomimimo.com/v1`
> 
> 你有 MiMo 的 API Key 吗？
> - **有**：直接发我 API Key，我帮你配好
> - **没有**：去 https://platform.xiaomimimo.com 申请一个，拿到 Key 发我
> - **用其他模型**（OpenAI / 自建 / 任何 OpenAI 兼容 API）：把 model 名、Base URL、API Key 一起发我

用户回复后，agent 一次性配置（避免逐条 config set 触发多次 restart 竞态）：

**用户只发了 API Key：**
```bash
miloco-cli config set model.omni.api_key "<key>"
```

**用户指定了 model / base_url：**
```bash
miloco-cli config set model.omni.api_key "<key>"
miloco-cli config set model.omni.model "<model>"
miloco-cli config set model.omni.base_url "<url>"
```

> 设置完成后不要立即 restart——等 2.3 统一 restart。

验证通过后进 2.3。

#### 2.3 重启 gateway

agent 先跑确认：
```bash
hermes cron list 2>&1 | grep -c miloco
```

**≥ 4 个 miloco cron：** → 后台插件已加载，进 Step 3。

**否则发：**
> 最后一步：重启 Hermes gateway 让新装的插件生效。
> 你在终端跑一下（agent 不能代跑，会被防重启循环拦截）：
> 
> ```bash
> hermes gateway restart
> ```
> 
> 跑完告诉我"好了"。

用户回复后 agent 验证：
```bash
hermes cron list 2>&1 | grep -c miloco
```
≥ 4 → 进 Step 3。

### Step 3：验证 + 完成安装

验证通过后，重启 backend 触发 onboarding（家庭初始化询问）：
```bash
miloco-cli service restart
```

**目标：** 确认安装完整可用。

```bash
bash plugins/hermes/tests/test_acceptance.sh
```

验证通过后告知用户：

> 安装完成！miloco 已就绪 ✓
> 
> 常用命令：
> - `miloco-cli service status` — 查看服务状态
> - `miloco-cli device list` — 查看设备列表
> - `miloco-cli config show` — 查看配置
> - `hermes cron list` — 查看定时任务
> 
> 试一下：`hermes chat -q "把客厅灯打开" -Q`

## 关键路径

| 内容 | 路径 |
|---|---|
| 配置文件 | `~/.hermes/miloco/config.json` |
| ONNX 模型 | `~/.hermes/miloco/models/` |
| 插件 | `~/.hermes/plugins/miloco/miloco-plugin/` |
| Adapter | `~/.hermes/miloco/agent_platform/hermes/adapter.py` |
| Skills | `~/.hermes/skills/miloco-*` |
| Backend 端口 | `127.0.0.1:1810` |

## Agent 执行要点

1. **严格按 3 步执行** — Step 1 → Step 2 → Step 3，不可跳步
2. **Step 1 全程不问用户** — 全部 agent 自己跑，只汇报结果
3. **Step 2 逐项引导** — 2.1 完成后等用户回复才发 2.2，禁止一次性贴出全部步骤
4. **不要代跑 `hermes gateway restart`** — Hermes 会拒绝，让用户自己终端跑
5. **尊重用户选择** — 可以跳过账号绑定或模型配置（但会影响功能）
6. **不要在聊天里回显 API Key 明文** — 仅通过 `miloco-cli config set` 传入

## 故障排除

| 现象 | 修法 |
|---|---|
| `miloco-cli: command not found` | 跑 Step 1.2 的 install.sh |
| backend 启动失败 | 看 `~/.hermes/miloco/log/miloco-backend.log` |
| 感知引擎 `no_omni_api_key` | 配 `miloco-cli config set model.omni.api_key <key>` |
| `hermes cron list` 崩溃 | 重跑 install-hermes.sh（cron deliver 修复） |
| trace 找不到 | gateway 进程需设 `MILOCO_HOME=~/.hermes/miloco` |
| im_push 报 needsBind | 在 Hermes 配 IM 后跑 `hermes gateway restart` |

| 现象 | 修法 |
|---|---|
| `miloco-cli: command not found` | 跑 Step 1.2 的 install.sh |
| backend 启动失败 | 看 `~/.hermes/miloco/log/miloco-backend.log` |
| 感知引擎 `no_omni_api_key` | 配 `miloco-cli config set model.omni.api_key <key>` |
| `hermes cron list` 崩溃 | 重跑 install-hermes.sh（cron deliver 修复） |
| trace 找不到 | gateway 进程需设 `MILOCO_HOME=~/.hermes/miloco` 到 launchd plist |
| im_push 报 needsBind | 在 Hermes 配 IM 后跑 `hermes gateway restart`，install-hermes.sh 会自动探测 |
