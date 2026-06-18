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

**安装方式（3 步全自动，不打断用户）：**

```bash
git clone https://github.com/XiaoMi/xiaomi-miloco.git
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

```bash
command -v miloco-cli || curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash
command -v miloco-cli || { echo "miloco-cli 仍找不到"; exit 1; }
test -f "${MILOCO_HOME:-$HOME/.openclaw/miloco}/config.json" || miloco-cli init
```

### 1.3 检查并装 python 依赖

```bash
command -v python3 || command -v python
# hermes v0.10.0 cron 调度需要 croniter；adapter 需要 aiohttp + httpx
python3 -c "import aiohttp, httpx, croniter" 2>/dev/null || python3 -m pip install aiohttp httpx croniter
```

### 1.4 拉 fork

```bash
test -d xiaomi-miloco || git clone https://github.com/XiaoMi/xiaomi-miloco.git
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

**如果脚本成功**，跑下面这条重启 hermes 让插件生效：

```bash
hermes gateway restart
```

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

### 3.3 **只在这 3 个条件下叫用户**（其它全自跑）

| 检查 | 命令 | 不通过时叫用户做什么 |
|---|---|---|
| 小米账号已绑？ | `miloco-cli account status` | 没绑就给出 `bind_url`，让用户在浏览器完成 OAuth，把授权码贴回来 |
| 模型已配？ | `miloco-cli config show model` | 没配就问 API key（推荐小米 MiMo） |
| Windows 不是 WSL？ | `uname -r \| grep -i microsoft` | 不在 WSL 就提醒用户用 WSL |

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

1. **严格按 3 步执行** — Step 1 → Step 2 → Step 3，不可跳步
2. **能不问就不问** — 只有 OAuth 授权码、API key 这种必须人提供的才停下来问用户；其它（装包、装 hermes、装 miloco、跑 install-hermes.sh、重启、验证）都自己跑
3. **出错先看错误本身** — 把命令 + returncode + stderr 贴给用户，参考故障表给建议，不要瞎猜
4. **不要回显敏感信息** — API key / 授权码只通过参数传，不要 echo
5. **尊重用户选择** — 用户说"先不绑账号"也行，告诉他后续怎么补
6. **Windows 提醒用 WSL** — 原生 Windows 不支持

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
