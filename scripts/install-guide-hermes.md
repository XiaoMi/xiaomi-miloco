---
name: install-miloco-hermes
description: Miloco Hermes Agent 兼容层安装引导，当用户说 "装 miloco for Hermes" 时激活。
metadata:
  author: Miloco Team
  version: "1.0"
  date: 2026-06-22
---

# Miloco × Hermes Agent 安装指南

## 概述

本 skill 指导 agent 通过 3 个阶段把 miloco 装到一个**已经装了 Hermes Agent** 的机器上。Hermes 是开源第三方 agent 运行时，不在 Miloco 官方 release 里；Hermes 兼容层插件在社区 fork（`n0tssss/xiaomi-miloco`）的 `plugins/hermes/` 下。

**安装方式（3 步全自动，不打断用户）：**

```bash
git clone https://github.com/n0tssss/xiaomi-miloco.git
cd xiaomi-miloco
bash plugins/hermes/install-hermes.sh
hermes gateway restart
```

> **Windows 用户：** 当前不支持原生 Windows，请先安装 [WSL](https://learn.microsoft.com/zh-cn/windows/wsl/install)，在 WSL 终端执行上述命令。

---

## Step 1: Prepare — 环境准备（agent 自跑）

**目标：** 检查并装齐所有依赖。缺的自己装，不要问用户。

依次执行下列命令。**每条都先看 exit code，非 0 才停下问用户**。

### 1.1 检查并装 hermes

```bash
command -v hermes || curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
test -d "$HOME/.hermes" || { echo "缺少 ~/.hermes"; exit 1; }
```

### 1.2 检查并装 miloco 后端

miloco 后端是 Xiaomi 官方闭源 binary，必须走 upstream release（**不是 fork**）：

```bash
command -v miloco-cli || curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash -s -- --agent-prepare
command -v miloco-cli || { echo "miloco-cli 仍找不到"; exit 1; }
test -f "${MILOCO_HOME:-$HOME/.openclaw/miloco}/config.json" || miloco-cli init
```

`--agent-prepare` 输出 JSON 形如：

```json
{
  "status": "ok",
  "account": {
    "is_bound": false,
    "bind_url": "https://account.xiaomi.com/oauth2/authorize?..."
  },
  "model": {
    "configured": false,
    "model": "xiaomi/mimo-v2.5",
    "base_url": "https://api.xiaomimimo.com/v1"
  }
}
```

记下 `account.bind_url` 和 `model.configured` 状态，下面 Step 2 之后要用。

### 1.3 检查并装 python 依赖

```bash
command -v python3 || command -v python
# hermes v0.10.0 cron 调度需要 croniter；adapter 需要 aiohttp + httpx
python3 -c "import aiohttp, httpx, croniter" 2>/dev/null || python3 -m pip install aiohttp httpx croniter
```

### 1.4 拉 fork（Hermes 兼容层只在 fork）

```bash
# 老机器先清掉，避免本地旧 commit / 改动导致 pull 失败拿不到最新修复
if [ -d xiaomi-miloco ]; then
  ( cd xiaomi-miloco && git fetch origin main && git reset --hard origin/main )
else
  git clone https://github.com/n0tssss/xiaomi-miloco.git
  cd xiaomi-miloco
fi
echo "已装 commit: $(git -C xiaomi-miloco rev-parse --short HEAD)"
```

### 1.5 解析输出

如果上面任何一条 exit ≠ 0，**把错误信息贴给用户**（包括命令 + returncode + stderr），然后参考底部故障排除表。不要自己瞎猜。

### 1.6 后端服务常驻（关键！否则 Step 2 会 502）

⚠️ **upstream `install.py` 注册了 `atexit.register(_stop_service)`**（line 1614 / 1663）：无论 install 成败，退出时都自动 `miloco-cli service stop`。这是上游设计哲学，不是 bug，但 fork 集成必须自己补一步。

如果跳过这步，**Step 2.1 的 `miloco-cli account authorize` 会拿到连接被拒**——CLI 误把它报成 `"invalid JSON response: 502"`（实际没有 502 服务器可问，是 backend 没起）。

```bash
miloco-cli service start
miloco-cli service status    # 期望 {"running": true}
# 或直接 curl 健康检查：
curl -sS http://127.0.0.1:1810/health   # 期望 {"status":"ok"}
```

如果这一步 `service start` 失败（比如 macOS 上 .pkg 没装 supervisord、或 systemd 没装），看错误自己排查；常见修：

```bash
# macOS：确认上游 .pkg 装到位
ls /Applications/MiCo.app 2>/dev/null || ls /usr/local/bin/miloco-cli 2>/dev/null
# 或重新跑上游装：
curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash -s -- --agent-prepare
```

> **fork 自动行为**：`bash plugins/hermes/install-hermes.sh` 在 Step 1 前置检查里会**自动调 `miloco-cli service start`** 把 backend 拉起来（upstream atexit 杀掉的我们再补）。如果你不想它自动启，传 `--no-start-backend`。

---

## Step 2: Ask — 收集用户配置（不跑脚本）

**目标：** 根据 Step 1 输出的状态，**逐项**询问用户是否需要配置米家账号和模型。**禁止同时询问两项**，否则用户输入无法区分对应哪个配置。

> **重要：Hermes chat 层面会自动 mask 敏感字符串**（base64 OAuth 授权码、`sk-xxx` API key 等）——agent 看不到明文也代跑不了。所以下列 3 类操作必须让用户在自己终端里跑：① 小米账号 OAuth 授权 ② 模型 API key 填入 ③ `hermes gateway restart`（Hermes anti-restart-loop 也会拒）。

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
> 完成后，请打开**你自己的终端**跑：
>
> ```bash
> miloco-cli account authorize <粘进去的 base64 码>
> ```
>
> 授权码有效期只有几分钟，浏览器拿到 → 立刻粘到终端跑一气呵成。跑完告诉 agent「绑好了」。

### 2.2 Omni 模型配置

根据 `model.configured`：

**已配置：**

> 当前模型配置：
>
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
> 请打开**你自己的终端**跑：
>
> ```bash
> miloco-cli config set model.omni.api_key "<你的_MiMo_Key>"
> ```
>
> 从 https://platform.xiaomimimo.com 拿 key。key 走 `miloco-cli config set` 不会被 mask，存到 `~/.openclaw/miloco/config.json` 是文件落盘。

---

## Step 3: Finish — 跑 install-hermes.sh 并验证

**目标：** 跑 install-hermes.sh 自动 patch miloco config / .env / 启 adapter，再让用户手动重启 Hermes gateway。

### 3.1 跑 install-hermes.sh

```bash
cd xiaomi-miloco
bash plugins/hermes/install-hermes.sh
```

脚本会做完 9 件事（幂等，可重复执行）：

1. 前置检查（python / miloco-cli / Hermes / config.json）
2. 拿/复用 adapter Bearer
3. 同步 16 个 miloco-\* skill → `~/.hermes/skills/`
4. 复制插件 + adapter → `~/.hermes/plugins/miloco/`
5. 探测 IM 平台（auth.json → config.yaml），写 deliver.target 到 `state.json`
6. patch `$MILOCO_HOME/config.json` 的 `agent` 段（备份 `config.json.bak-<ts>-pid<nsec>`）
7. 写 `~/.hermes/.env` 的 `API_SERVER_KEY` + 启 adapter（20s retry loop）
8. `hermes plugins enable miloco`（idempotent，跳过已 enabled 的）
9. 记录版本（hermes / miloco-cli / plugin / git_commit）到 `state.json::versions`，升级时打印 diff

**如果脚本 exit ≠ 0**：看脚本打印的错误（前置检查 / patch / 启 adapter 任一步），跑 `bash plugins/hermes/scripts/miloco-adapter.sh logs` 看 adapter 日志，参考底部故障排除表。

### 3.2 用户手动重启 Hermes gateway

⚠️ **这步必须由用户自己终端跑，agent 代跑会被 Hermes anti-restart-loop 拒绝**：

```bash
hermes gateway restart
```

### 3.3 自动验证（agent 自跑）

```bash
# 1. 插件 enabled？
hermes plugins list | grep -i miloco

# 1.5 通知投递 target 已自动配置？
test -f "$HOME/.hermes/plugins/miloco/miloco-plugin/state.json" && \
  python -c "import json; d=json.load(open(r'$HOME/.hermes/plugins/miloco/miloco-plugin/state.json')); print('deliver.target =', d.get('deliver',{}).get('target'))"

# 2. 16 skill 装上？
ls ~/.hermes/skills/miloco-* | wc -l   # 应 16

# 3. adapter 在跑？
bash plugins/hermes/scripts/miloco-adapter.sh status   # 应 health OK

# 4. 入站 webhook 健康
curl -sS -o /dev/null -w "%{http_code}" http://127.0.0.1:18789/health   # 应 200

# 5. e2e 测试套（最稳）
cd xiaomi-miloco && bash plugins/hermes/tests/test_install_e2e.sh   # 应 PASS: 22, FAIL: 0
```

### 3.4 报告当前状态（给用户）

不管 3.3 结果如何，都给用户一份状态报告：

```
✅ miloco × Hermes 兼容层已装好
  - 插件: ~/.hermes/plugins/miloco/miloco-plugin (enabled)
  - 16 skill: ~/.hermes/skills/miloco-*
  - adapter: PID=<pid>，端口 18789，日志 ~/.hermes/miloco-adapter.log
  - miloco config.json: webhook_url + auth_bearer 已 patch，备份 config.json.bak-<ts>
  - Hermes .env: API_SERVER_KEY 已追加
  - 主动通知: deliver.target = <feishu/telegram/...>（自动配置），cron 触发即可送达
  - 验证: 22/22 e2e 通过 / 或列出失败项
```

### 3.5 **这 3 类只让用户自己终端操作**

| 触发条件     | 做什么                                                                                                       |
| ------------ | ------------------------------------------------------------------------------------------------------------ |
| 小米账号未绑 | 走 [Step 2.1](#21-米家账号)：给 `bind_url`，让用户浏览器授权 + **自己终端跑** `miloco-cli account authorize` |
| 模型未配     | 走 [Step 2.2](#22-omni-模型配置)：让用户**自己终端跑** `miloco-cli config set model.omni.api_key`            |
| 重启 hermes  | 走 [Step 3.2](#32-用户手动重启-hermes-gateway)：让用户**自己终端跑** `hermes gateway restart`                |

> **绝对不要**在 chat 里要 OAuth 授权码 / API key —— Hermes 会 mask，agent 看不到也无法替你跑。

**任何其它情况（git 报错、端口冲突、依赖装不上等）都自己排查 + 看脚本错误 + 参考故障表，不要先问用户。**

---

## 故障排除

| 问题                                     | 解决                                                                                                                                                                  |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `找不到 miloco-cli`                      | Step 1.2 应自动装；如果还缺，确认 `~/.local/bin` 在 PATH                                                                                                              |
| `找不到 ~/.hermes`                       | Step 1.1 应自动装；如果还缺，看 hermes 官方安装日志                                                                                                                   |
| `MILOCO_HOME` 没设 / config.json 找不到  | `export MILOCO_HOME=$HOME/.openclaw/miloco` 然后再跑，或 `miloco-cli init`                                                                                            |
| `adapter 启动失败，端口 X 未监听`        | `netstat -ano \| grep X`（Win）/ `lsof -iTCP:X -sTCP:LISTEN`（Mac/Linux）找占用 kill；或 `export ADAPTER_PORT=18790` 换端口重跑 install-hermes.sh                     |
| `No module named aiohttp`                | `pip install aiohttp httpx croniter` 后重跑 install-hermes.sh                                                                                                         |
| `hermes cron list` 没见 4 个 miloco 任务 | `pip install croniter` → `hermes gateway restart`                                                                                                                     |
| `hermes chat` 报 401 / model 错          | 检查 `~/.hermes/config.yaml` 的 `model.api_key` 或对应 provider env                                                                                                   |
| 装完发现 adapter 没在跑                  | `bash plugins/hermes/scripts/miloco-adapter.sh logs` 看日志；`status` 看 PID/端口/健康                                                                                |
| adapter 启动后端口还在占                 | 旧进程没杀干净：`bash plugins/hermes/scripts/miloco-adapter.sh stop` 再 start                                                                                         |
| `git clone` / `git pull` 失败            | 网络问题；可设 `git config --global url."https://ghproxy.com/".insteadOf https://` 走镜像                                                                             |
| `miloco_im_push` 报 `no deliver target`  | 看 `~/.hermes/plugins/miloco/miloco-plugin/state.json::deliver.target`；先在 Hermes 里连一个 IM（`hermes config set telegram.bot_token ...`）后重跑 install-hermes.sh |
| 装完 `hermes plugins list` 没 miloco     | `bash plugins/hermes/install-hermes.sh` 重跑（脚本会 idempotent enable）；或手动 `hermes plugins enable miloco`                                                       |

---

## Agent 执行要点

1. **严格按 3 步执行** — Step 1 → Step 2 → Step 3，不可跳步
2. **能不问就不问** — 装包、装 hermes、装 miloco、跑 install-hermes.sh、验证都自己跑
3. **必须让用户终端跑的事绝不替** — OAuth 授权、API key 填入、hermes gateway restart，详见 Step 2。原因：Hermes chat 层面 mask 敏感值 + Hermes 自身 anti-restart-loop
4. **出错先看错误本身** — 把命令 + returncode + stderr 贴给用户，参考故障表给建议，不要瞎猜
5. **不要回显敏感信息** — API key / 授权码不要 echo
6. **尊重用户选择** — 用户说"先不绑账号"也行，告诉他后续怎么补
7. **Windows 提醒用 WSL** — 原生 Windows 不支持

---

## 想回滚（用户要求时再跑）

```bash
# 1. 停 adapter
bash plugins/hermes/scripts/miloco-adapter.sh stop

# 2. 卸插件 + skill + disable
hermes plugins disable miloco
rm -rf ~/.hermes/plugins/miloco
rm -rf ~/.hermes/skills/miloco-*

# 3. 还原 miloco config.json
cp $MILOCO_HOME/config.json.bak-<ts> $MILOCO_HOME/config.json

# 4. .env 去掉 API_SERVER_KEY 那行

# 5. 重启 hermes
hermes gateway restart
```
