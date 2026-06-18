---
name: install-miloco-hermes
description: Miloco × Hermes Agent 兼容层安装引导，当用户说"装 miloco for Hermes"时激活。
metadata:
  author: n0tssss
  version: "1.0"
  date: 2026-06-19
---

# Miloco × Hermes Agent 安装指南

## 概述

本 skill 指导 agent 通过 3 个阶段把 miloco 装到一个**已经装了 Hermes Agent** 的机器上。agent 自己跑所有非人介入的命令，**只在小米账号 OAuth / API key 等必须人操作的环节才停下来问用户**。命令跑完打印清晰的最终状态。

**关键原则**：Hermes（以及 Claude / Cursor 等通用 AI 工具）在 chat 层面会自动 mask 敏感字符串（base64 OAuth 授权码、`sk-xxx` API key、`Bearer xxx` 等）——agent 看不到明文也代跑不了涉及这些值的命令。**所以以下 3 类操作必须让用户自己在终端跑**：① 小米账号 OAuth 授权 ② 模型 API key 填入 ③ `hermes gateway restart`（Hermes 自身的 anti-restart-loop 也会拒）。详见 [Step 2.5: 强制用户终端操作](#step-25-强制用户终端操作agent-不要自己跑)。

**安装方式（3 步全自动，不打断用户）：**

```bash
git clone https://github.com/n0tssss/xiaomi-miloco.git
cd xiaomi-miloco
bash plugins/hermes/install-hermes.sh
hermes gateway restart
```

> **Windows 用户：** 原生 Windows 不支持，请先装 [WSL](https://learn.microsoft.com/zh-cn/windows/wsl/install) 在 WSL 终端执行。

---

## Step 1: Prepare — 环境准备（agent 自跑，不问用户）

**目标：** 检查并装齐所有依赖。缺的自己装，不要问用户。

依次执行下列命令。**每条都先看 exit code，非 0 才停下问用户**。

### 1.1 检查并装 hermes

```bash
command -v hermes || curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
test -d "$HOME/.hermes" || { echo "缺少 ~/.hermes"; exit 1; }
```

### 1.2 检查并装 miloco 后端

⚠️ **必须用 `--agent-prepare` 模式**，否则上游 `install.sh` 在非交互终端里检测到 stdin 不是 tty 会直接退出。`--agent-prepare` 跑准备 + 探活，输出 JSON 含 `account.bind_url` 和 `model.configured` 状态。

```bash
command -v miloco-cli || curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash -s -- --agent-prepare
command -v miloco-cli || { echo "miloco-cli 仍找不到"; exit 1; }
test -f "${MILOCO_HOME:-$HOME/.openclaw/miloco}/config.json" || miloco-cli init
```

`--agent-prepare` 输出 JSON 形如：

```json
{
  "status": "ok",
  "account": {"is_bound": false, "bind_url": "https://account.xiaomi.com/oauth2/authorize?..."},
  "model":   {"configured": false, "model": "xiaomi/mimo-v2.5", "base_url": "https://api.xiaomimimo.com/v1"}
}
```

记下 `account.bind_url` 和 `model.configured` 状态，下面 Step 2 之后要用。

### 1.3 检查并装 python 依赖

```bash
command -v python3 || command -v python
# hermes v0.10.0 cron 调度需要 croniter；adapter 需要 aiohttp + httpx
python3 -c "import aiohttp, httpx, croniter" 2>/dev/null || python3 -m pip install aiohttp httpx croniter
```

### 1.4 拉 fork

```bash
test -d xiaomi-miloco || git clone https://github.com/n0tssss/xiaomi-miloco.git
cd xiaomi-miloco && git pull --ff-only
```

### 1.5 解析输出

如果上面任何一条 exit ≠ 0，**把错误信息贴给用户**（包括命令 + returncode + stderr），然后参考底部故障排除表。不要自己瞎猜。

---

## Step 2: Install — 装兼容层（agent 自跑，不问用户）

**目标：** 跑 install-hermes.sh，自动 patch miloco config / .env / 启 adapter，全部幂等。

```bash
cd xiaomi-miloco
bash plugins/hermes/install-hermes.sh
```

脚本会做完 7 件事（同 OpenClaw 装 OpenClaw 插件的体验）：
1. 前置检查
2. 同步 16 个 miloco-* skill 到 `~/.hermes/skills/`
3. 复制插件到 `~/.hermes/plugins/miloco/miloco-plugin/`
4. 复制 adapter 到 `~/.hermes/plugins/miloco/adapter/`
5. patch `$MILOCO_HOME/config.json` 的 `agent` 段（备份 `config.json.bak-<ts>`）
6. 写 `~/.hermes/.env` 的 `API_SERVER_KEY`（缺失则生成，存在则复用）
7. nohup 启 adapter，PID 写到 `~/.hermes/miloco-adapter.pid`

**如果脚本 exit ≠ 0**：
- 看脚本打印的错误（前置检查 / patch / 启 adapter 任一步）
- 跑 `bash plugins/hermes/scripts/miloco-adapter.sh logs` 看 adapter 日志
- 参考底部故障排除表，不要瞎猜

**如果脚本成功**，**先验证 adapter 在不在**（**不要现在就跑 `hermes gateway restart`**，见 [Step 2.5](#step-25-强制用户终端操作agent-不要自己跑)）：

```bash
bash plugins/hermes/scripts/miloco-adapter.sh status
# 期望：adapter 在跑，端口 PID=xxxxx，health OK
```

---

## Step 2.5: 强制用户终端操作（agent 不要自己跑）

⚠️ **这 3 件事必须由用户在自己终端里执行，agent 不要尝试在 chat 里代收或代跑**。

原因有两层：
1. **Hermes 安全机制**会在 chat 消息里自动 mask 敏感字符串（base64 授权码、API key 等）——agent 看不到真值
2. **某些命令**（如 `hermes gateway restart`）Hermes 自身的 anti-restart-loop 会拒绝在 gateway 进程内执行

### 1. 小米账号 OAuth 授权

agent 从 Step 1.2 的 `--agent-prepare` JSON 拿到 `account.bind_url`，把链接给用户：

> 请在浏览器打开下面链接完成小米账号授权：
> https://account.xiaomi.com/oauth2/authorize?...
>
> 授权后页面会显示一串 base64 字符串（**不要贴到 chat 里**——会被 mask）。
> 请打开**你自己的终端**跑：
>
> ```bash
> cd ~/xiaomi-miloco   # 或你 clone 的位置
> miloco-cli account authorize --code "<从浏览器复制的 base64 码>"
> ```
>
> 跑完告诉 agent「绑好了」即可。

OAuth 授权码有效期只有几分钟，所以**浏览器拿到码 → 立刻粘到终端跑**一气呵成。

### 2. 模型 API Key 配置（如果 Step 1.2 的 `model.configured=false`）

agent 不要在 chat 里要 API key（用户贴过来你也是看到 mask 后的串）。直接告诉用户：

> 请打开**你自己的终端**跑：
>
> ```bash
> miloco-cli config set model.omni.api_key "<你的_MiMo_Key>"
> ```
>
> 从 https://platform.xiaomimimo.com 拿 key。key 走 `miloco-cli config set` 不会被 mask，存到 `~/.openclaw/miloco/config.json` 是文件落盘。

### 3. 重启 Hermes Gateway

agent 内部跑 `hermes gateway restart` 会被 Hermes 自身的 anti-restart-loop 拒绝。告诉用户：

> 上面 install-hermes.sh 已经把插件和 API_SERVER_KEY 装好了，**请打开你自己的终端跑**：
>
> ```bash
> hermes gateway restart
> ```
>
> 跑完告诉 agent「重启好了」。

### 用户回执后 agent 做什么

用户做完上面 3 件事，告诉你「绑好了 / 重启好了」之类，你再继续 Step 3 验证。**不要催用户、不要替用户执行、不要 echo 任何敏感值**。

---

## Step 3: Verify + Ask（agent 自验，仅必要时叫人）

**目标：** 自动验证全链路；只在小米账号 / 模型 API key 真正缺失时才叫人。

### 3.1 自动验证（agent 自跑）

```bash
# 1. 插件 enabled？
hermes plugins list | grep -i miloco

# 2. 16 skill 装上？
ls ~/.hermes/skills/miloco-* | wc -l   # 应 16

# 3. adapter 在跑？
bash plugins/hermes/scripts/miloco-adapter.sh status   # 应 health OK

# 4. 入站 webhook 健康
curl -sS -o /dev/null -w "%{http_code}" http://127.0.0.1:18789/health   # 应 200

# 5. e2e 测试套（最稳）
cd xiaomi-miloco && bash plugins/hermes/tests/test_install_e2e.sh   # 应 PASS: 22, FAIL: 0
```

### 3.2 报告当前状态（给用户）

不管 3.1 结果如何，都给用户一份状态报告：

```
✅ miloco × Hermes 兼容层已装好
  - 插件: ~/.hermes/plugins/miloco/miloco-plugin (enabled)
  - 16 skill: ~/.hermes/skills/miloco-*
  - adapter: PID=<pid>，端口 18789，日志 ~/.hermes/miloco-adapter.log
  - miloco config.json: webhook_url + auth_bearer 已 patch，备份 config.json.bak-<ts>
  - Hermes .env: API_SERVER_KEY 已追加
  - 验证: 22/22 e2e 通过 / 或列出失败项
```

### 3.3 **这 3 类只让用户自己终端操作**（其它全自跑）

| 触发条件 | 做什么 |
|---|---|
| 小米账号未绑 | 走 [Step 2.5-1](#1-小米账号-oauth-授权)：给 `bind_url`，让用户浏览器授权 + **自己终端跑** `miloco-cli account authorize` |
| 模型未配 | 走 [Step 2.5-2](#2-模型-api-key-配置如果-step-12-的-model_configuredfalse)：让用户**自己终端跑** `miloco-cli config set model.omni.api_key` |
| 重启 hermes | 走 [Step 2.5-3](#3-重启-hermes-gateway)：让用户**自己终端跑** `hermes gateway restart` |

> **绝对不要**在 chat 里要 OAuth 授权码 / API key —— Hermes 会 mask，agent 看不到也无法替你跑。

**任何其它情况（git 报错、端口冲突、依赖装不上等）都自己排查 + 看脚本错误 + 参考故障表，不要先问用户。**

---

## 故障排除

| 问题 | 解决 |
|---|---|
| `找不到 miloco-cli` | Step 1.2 应自动装；如果还缺，确认 `~/.local/bin` 在 PATH |
| `找不到 ~/.hermes` | Step 1.1 应自动装；如果还缺，看 hermes 官方安装日志 |
| `MILOCO_HOME` 没设 / config.json 找不到 | `export MILOCO_HOME=$HOME/.openclaw/miloco` 然后再跑，或 `miloco-cli init` |
| `adapter 启动失败，端口 X 未监听` | `netstat -ano \| grep X`（Win）/ `lsof -iTCP:X -sTCP:LISTEN`（Mac/Linux）找占用 kill；或 `export ADAPTER_PORT=18790` 换端口重跑 install-hermes.sh |
| `No module named aiohttp` | `pip install aiohttp httpx croniter` 后重跑 install-hermes.sh |
| `hermes cron list` 没见 4 个 miloco 任务 | `pip install croniter` → `hermes gateway restart` |
| `hermes chat` 报 401 / model 错 | 检查 `~/.hermes/config.yaml` 的 `model.api_key` 或对应 provider env |
| 装完发现 adapter 没在跑 | `bash plugins/hermes/scripts/miloco-adapter.sh logs` 看日志；`status` 看 PID/端口/健康 |
| adapter 启动后端口还在占 | 旧进程没杀干净：`bash plugins/hermes/scripts/miloco-adapter.sh stop` 再 start |
| `git clone` / `git pull` 失败 | 网络问题；可设 `git config --global url."https://ghproxy.com/".insteadOf https://` 走镜像 |

---

## Agent 执行要点

1. **严格按 3 步执行** — Step 1 → Step 2 → [Step 2.5 用户终端操作] → Step 3，不可跳步
2. **能不问就不问** — 装包、装 hermes、装 miloco、跑 install-hermes.sh、验证都自己跑
3. **必须让用户终端跑的事绝不替** — OAuth 授权、API key 填入、hermes gateway restart，详见 Step 2.5。原因：Hermes chat 层面 mask 敏感值 + Hermes 自身 anti-restart-loop
4. **出错先看错误本身** — 把命令 + returncode + stderr 贴给用户，参考故障表给建议，不要瞎猜
5. **不要回显敏感信息** — API key / 授权码不要 echo
6. **尊重用户选择** — 用户说"先不绑账号"也行，告诉他后续怎么补
7. **Windows 提醒用 WSL** — 原生 Windows 不支持

---

## 想回滚（用户要求时再跑）

```bash
# 1. 停 adapter
bash plugins/hermes/scripts/miloco-adapter.sh stop

# 2. 卸插件 + skill
rm -rf ~/.hermes/plugins/miloco
rm -rf ~/.hermes/skills/miloco-*

# 3. 还原 miloco config.json
cp $MILOCO_HOME/config.json.bak-<ts> $MILOCO_HOME/config.json

# 4. .env 去掉 API_SERVER_KEY 那行

# 5. 重启 hermes
hermes gateway restart
```
