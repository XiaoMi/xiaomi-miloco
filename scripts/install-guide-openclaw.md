---
name: install-miloco
description: Miloco OpenClaw 插件安装引导，当用户说 "安装 miloco" 时激活。
metadata:
  author: Miloco Team
  version: "1.0"
  date: 2026-05-19
---

# Miloco 安装指南

## 概述

本 skill 指导 agent 通过 3 个阶段完成 Miloco 安装。脚本以 `agent` 模式运行，专为非交互终端设计，通过 JSON 输出与 agent 通信。

**安装方式：**

```bash
curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash
```

> **Windows 用户：** 当前不支持原生 Windows，请先安装 [WSL](https://learn.microsoft.com/zh-cn/windows/wsl/install)，在 WSL 终端中执行上述命令。

---

## Step 1: Prepare — 环境准备与依赖安装

**目标：** 完成环境检查、核心包安装、服务启动，并获取当前账号和模型配置状态。

### 1.1 运行 Prepare

```bash
curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash -s -- --agent-prepare
```

### 1.2 解析输出

脚本会输出包裹在 `--- AGENT_JSON_START ---` / `--- AGENT_JSON_END ---` 之间的 JSON：

```json
{
  "status": "ok",
  "account": {
    "is_bound": true,
    "bind_url": "https://account.xiaomi.com/oauth2/authorize?...",
    "user": "user@example.com"
  },
  "model": {
    "configured": true,
    "model": "xiaomi/mimo-v2.5",
    "base_url": "https://api.xiaomimimo.com/v1",
    "api_key_masked": "sk-1****abcd"
  }
}
```

如果 `status` 为 `"error"`，输出格式为：

```json
{
  "status": "error",
  "command": "失败的命令",
  "returncode": 1,
  "stderr": "错误信息"
}
```

**出错处理：** 将错误信息展示给用户，参考底部故障排除表给出建议。

---

## Step 2: Ask — 收集用户配置

**目标：** 根据 Step 1 输出的状态，询问用户是否需要配置米家账号和模型。

此步骤 **不运行脚本**，由 agent 直接与用户交互。

> **重要：** 必须按顺序逐项询问 — 先完成账号配置（或确认跳过），再询问模型配置。**禁止同时询问两项**，否则用户输入无法区分对应哪个配置。

### 2.1 米家账号

根据 `account.is_bound`：

**已绑定：**
> 当前已绑定米家账号：{account.user}
> 是否继续使用当前账号？还是重新绑定？

- 继续使用 → Step 3 不传 `--account-auth`
- 重新绑定 → 给用户 `account.bind_url`，等用户完成授权后提供授权码

**未绑定：**
> Miloco 需要绑定小米账号才能控制智能设备。
> 请在浏览器中打开以下链接完成授权：
>
> {account.bind_url}
>
> 完成后，请将页面上显示的授权码（base64 字符串）复制给我。

收集到授权码后记录，Step 3 传入 `--account-auth "<授权码>"`。

### 2.2 Omni 模型配置

根据 `model.configured`：

**已配置：**
> 当前模型配置：
> - Model: {model.model}
> - Base URL: {model.base_url}
> - API Key: {model.api_key_masked}
>
> 是否沿用当前配置？还是使用新的模型服务？

- 沿用 → Step 3 不传模型参数
- 重新配置 → 收集新的 API Key（及可选的 model/base_url）

**未配置：**
> Miloco 的感知引擎需要一个多模态大模型（Omni Model）来理解摄像头画面。
> 默认推荐 **小米 MiMo** 模型。
>
> 请提供：
> 1. **API Key** — 从 https://platform.xiaomimimo.com 获取
> 2. **Model 名称**（可选，默认 `xiaomi/mimo-v2.5`）
> 3. **Base URL**（可选，默认 `https://api.xiaomimimo.com/v1`）
>
> 也支持其他兼容 OpenAI 接口的模型服务。

---

## Step 3: Finish — 完成配置与安装

**目标：** 重启服务、写入账号/模型配置、下载感知模型、安装 OpenClaw 插件。

### 3.1 运行 Step 3

根据 Step 2 收集到的信息组装命令：

```bash
curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash -s -- --agent-finish \
  --account-auth "<授权码>" \
  --omni-api-key "<API Key>" \
  --omni-model "xiaomi/mimo-v2.5" \
  --omni-base-url "https://api.xiaomimimo.com/v1"
```

**参数说明：**

| 参数 | 必需 | 说明 |
|------|------|------|
| `--account-auth` | 仅新绑定时 | 用户提供的 base64 授权码 |
| `--omni-api-key` | 仅需配置模型时 | API Key |
| `--omni-model` | 否 | 模型名，默认 `xiaomi/mimo-v2.5` |
| `--omni-base-url` | 否 | Base URL，默认 `https://api.xiaomimimo.com/v1` |
| `--skip-openclaw` | 否 | 跳过 OpenClaw 插件安装 |

- 如果用户选择沿用已有账号，省略 `--account-auth`
- 如果用户选择沿用已有模型配置，省略所有 `--omni-*` 参数

### 3.2 验证安装

脚本成功退出后，告知用户：

> 安装完成！执行 `openclaw gateway restart` 重启网关即可使用。
>
> 常用命令：
> - `miloco-cli service status` — 查看服务状态
> - `miloco-cli service logs -f` — 实时日志
> - `miloco-cli device list` — 查看设备列表
> - `miloco-cli config show` — 查看配置

---

## 故障排除

| 问题 | 解法 |
|------|------|
| `uv` 未找到 | `curl -LsSf https://astral.sh/uv/install.sh \| sh && export PATH="$HOME/.local/bin:$PATH"` |
| Python 版本不满足 | `uv python install 3.14` |
| miloco-cli 未找到 | 确认 `~/.local/bin` 在 PATH 中 |
| 服务启动失败 | `miloco-cli service logs` 查看日志 |
| 账号绑定失败 | 确认服务正在运行：`miloco-cli service status` |
| 模型下载失败 | 检查网络，可设置 `MILOCO_DOWNLOAD_URL` 指定镜像 |
| openclaw 未安装或版本过低 | 需 >= 2026.5.2，参考 https://openclaw.ai 安装 |

---

## Agent 执行要点

1. **严格按 3 步执行** — Step 1 → Step 2 → Step 3，不可跳步
2. **解析 JSON 输出** — 在 stdout 中找 `AGENT_JSON_START`/`AGENT_JSON_END` 之间的内容
3. **出错时反馈用户** — 展示错误命令和 stderr，参考故障排除表
4. **不要在脚本中传入用户敏感信息的明文回显** — API Key 等仅通过参数传入，不要 echo 到终端
5. **尊重用户选择** — 可以跳过账号绑定或模型配置（但会影响功能）
6. **Windows 用户提示使用 WSL** — 当前不支持原生 Windows，需在 WSL 中执行
