---
name: install-miloco-hermes
description: Miloco Hermes Agent 兼容层安装引导，当用户说 "装 miloco for Hermes" 时激活。
metadata:
  author: Miloco Team
  version: "1.0"
  date: 2026-06-22
---

# Miloco × Hermes Agent 安装指南

把 miloco 装到一台**已经装了 Hermes Agent** 的机器上。Hermes 兼容层在社区 fork `n0tssss/xiaomi-miloco` 的 `plugins/hermes/` 下。

## 用户的 3 步

```bash
git clone https://github.com/n0tssss/xiaomi-miloco.git
cd xiaomi-miloco
bash plugins/hermes/install-hermes.sh
hermes gateway restart
```

> **Windows 用户：** 不支持原生 Windows，先装 [WSL](https://learn.microsoft.com/zh-cn/windows/wsl/install)。

---

## Step 1：环境准备（agent 自己跑，**全程不问用户**）

依次执行下面 6 步。**每步都看 exit code，非 0 才停下来排查**。

### 1.1 装 hermes

```bash
command -v hermes || curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
test -d "$HOME/.hermes" || { echo "缺少 ~/.hermes"; exit 1; }
```

成功标志：`hermes --version` 输出 `Hermes Agent v...`。

### 1.2 装 miloco 后端（走 upstream release，不是 fork）

```bash
command -v miloco-cli || curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash -s -- --agent-prepare
command -v miloco-cli || { echo "miloco-cli 仍找不到"; exit 1; }
```

成功标志：`miloco-cli --version` 输出 `miloco-cli ...`。

`--agent-prepare` 退出时**会自动 stop backend**（upstream 设计如此）。下面 1.6 会把它拉起来。

### 1.3 装 python 依赖

```bash
command -v python3 || command -v python
python3 -c "import aiohttp, httpx, croniter" 2>/dev/null || python3 -m pip install aiohttp httpx croniter
```

### 1.4 拉 fork

```bash
if [ -d xiaomi-miloco ]; then
  ( cd xiaomi-miloco && git fetch origin main && git reset --hard origin/main )
else
  git clone https://github.com/n0tssss/xiaomi-miloco.git && cd xiaomi-miloco
fi
```

成功标志：打印 `已装 commit: <short-sha>`。

### 1.5 装 fork 的插件（核心步骤）

```bash
cd xiaomi-miloco
bash plugins/hermes/install-hermes.sh
```

脚本会自动：

1. 前置检查（python / miloco-cli / Hermes / config.json）
2. 拿/复用 adapter Bearer
3. 同步 16 个 miloco-\* skill → `~/.hermes/skills/`
4. 复制插件 + adapter → `~/.hermes/plugins/miloco/`
5. 探测 IM 平台（auth.json → config.yaml），写 `deliver.target` 到 `state.json`
6. patch `$MILOCO_HOME/config.json` 的 `agent` 段（备份 `config.json.bak-<ts>`）
7. 写 `~/.hermes/.env` 的 `API_SERVER_KEY` + 启 adapter（macOS 走 launchd，其他 nohup）
8. `hermes plugins enable miloco` + 兜底清 `plugins.disabled` 里 miloco 残留
9. 记录版本到 `state.json::versions`

成功标志：脚本打印 `mark_done 9` 且 exit 0。

### 1.6 拉起 miloco backend

**为什么必须做**：1.2 跑的 `--agent-prepare` 退出时会 `atexit.register(_stop_service)` 把 backend 停掉。如果跳过这步，2.1 OAuth 会拿到"连接被拒"（CLI 误报成 `invalid JSON response: 502`）。

```bash
miloco-cli service start
miloco-cli service status    # 期望 {"running": true}
curl -sS http://127.0.0.1:1810/health   # 期望 {"status":"ok"}
```

成功标志：`/health` 返 200。

> fork 的 `install-hermes.sh` Step 1.5 在脚本里**也会自动** `service start`，跟这里等价。如果不想让它自动启，传 `--no-start-backend` 给 install-hermes.sh。

### 1.7 出错就排查

上面任何一步 exit ≠ 0，把**命令 + returncode + stderr** 贴给用户 + 翻下方"故障排除"表。**不要自己瞎猜**。

---

## Step 2：用户自己跑 3 个敏感操作（agent 不代跑）

**机制**：Hermes chat 自动 mask 敏感值（OAuth 授权码 / API key），agent 看不到也代跑不了。

agent 这一步只做一件事：**把命令贴给用户**。用户跑完回一句话。

### 2.1 米家账号

**agent 跑一下确认状态：**

```bash
miloco-cli account status
```

**已绑定（输出含 `is_bound: true`）：**

贴：

> 当前已绑定小米账号。继续用它。说"继续"我接着配模型。

**未绑定（输出含 `is_bound: false`）：**

贴（**整段原样发**，不多问）：

> 打开浏览器授权小米账号：
>
> {bind_url}
>
> 授权完浏览器跳到 mico.api.mijia.tech/login_redirect，URL 里 `code=` 后面那串 base64 复制下来。
> 立刻**你自己终端**跑（base64 5 分钟过期）：
>
> ```bash
> miloco-cli account authorize <粘进去的 base64 码>
> ```
>
> 跑完告诉我「绑好了」我接着配模型。

### 2.2 Omni 模型

**agent 跑一下确认状态：**

```bash
miloco-cli config get model.omni.api_key
miloco-cli config get model.omni.model
miloco-cli config get model.omni.base_url
```

**已配置（三项都非空）：**

贴：

> 模型已配：{model} @ {base_url}。继续用它。说"继续"我接着装。

**未配置（任一项为 null / 报错）：**

贴：

> 感知引擎需要 Omni 模型。**两个选项，挑一个跑**：
>
> **A. 用默认小米 MiMo**（推荐，国产多模态大模型）
>
> key 从 https://platform.xiaomimimo.com 拿。你自己终端跑：
>
> ```bash
> miloco-cli config set model.omni.api_key <你的_MiMo_Key>
> ```
>
> （model = `xiaomi/mimo-v2.5`、base_url = `https://api.xiaomimimo.com/v1` 是默认值，不用设）
>
> **B. 用第三方模型**（OpenAI / Anthropic / 自建 / 任何 OpenAI 兼容 API）
>
> 你自己终端跑（一次写完三项）：
>
> ```bash
> miloco-cli config set model.omni.model <model_name> model.omni.base_url <base_url> model.omni.api_key <api_key>
> ```
>
> 例（用 OpenAI）：
> ```bash
> miloco-cli config set model.omni.model gpt-4o model.omni.base_url https://api.openai.com/v1 model.omni.api_key sk-xxx
> ```
>
> 跑完告诉我「配好了」我接着装。

### 2.3 重启 Hermes gateway

`install-hermes.sh` 装好了，但 Hermes 自身有 anti-restart-loop，agent 代跑会被拒。**用户必须自己终端跑**：

```bash
hermes gateway restart
```

### 2.4 收尾

用户回「绑好了」/「配好了」/「继续」任意一句 → **直接进入 Step 3**，不再问。

---

## Step 3：验证 + 报告 + 给本地链接

`install-hermes.sh` 已经跑过了，gateway 也重启了。**agent 自跑下面 5 步验证**，每步把 PASS/FAIL 打印出来。

### 3.1 5 步验证

```bash
# 1. 插件 enabled？
hermes plugins list 2>/dev/null | grep -i miloco

# 2. 16 skill 装上？
ls ~/.hermes/skills/miloco-* 2>/dev/null | wc -l    # 应 16

# 3. 通知投递 target 已配？
test -f "$HOME/.hermes/plugins/miloco/miloco-plugin/state.json" && \
  python3 -c "import json; d=json.load(open(r'$HOME/.hermes/plugins/miloco/miloco-plugin/state.json')); print('deliver.target =', d.get('deliver',{}).get('target'))"

# 4. adapter 在跑 + /health 200？
bash plugins/hermes/scripts/miloco-adapter.sh status
curl -sS -o /dev/null -w "adapter /health: %{http_code}\n" http://127.0.0.1:18789/health    # 应 200

# 5. backend /health 200？
curl -sS -o /dev/null -w "backend /health: %{http_code}\n" http://127.0.0.1:1810/health     # 应 200
```

### 3.2 状态报告（**实际值填进去，不要占位符**）

把下面模板里的 `<...>` 替换成**真实输出值**，整段贴给用户：

```
✅ miloco × Hermes 兼容层已装好

  插件:        <hermes plugins list 的 miloco 行原文>
  16 skill:    <第 2 步的实际数字，应 16>
  主动通知:    deliver.target = <第 3 步的实际 target，如 feishu/telegram/(null)>
  adapter:     <miloco-adapter.sh status 的关键行 + PID + 端口>
  adapter 健康: http://127.0.0.1:18789/health → <第 4 步的 HTTP code>
  backend 健康: http://127.0.0.1:1810/health → <第 5 步的 HTTP code>

  本地链接：
    - miloco backend API:    http://127.0.0.1:1810
    - 入站 adapter webhook:  http://127.0.0.1:18789
    - Hermes gateway:        http://127.0.0.1:8642
    - 配置文件:              $MILOCO_HOME/config.json (备份: *.bak-<ts>)
    - 插件状态:              $HOME/.hermes/plugins/miloco/miloco-plugin/state.json
    - adapter 日志:          $HOME/.hermes/miloco-adapter.log
    - e2e 测试套:            bash plugins/hermes/tests/test_install_e2e.sh

  失败项: <如果有：列出来；没就写"无">
```

**任何一项 FAIL**：把对应命令 + stderr 贴给用户 + 翻下方"故障排除"表。

### 3.3 引导用户试一个真实动作

报告贴完后，主动引导用户跑第一个真实动作，证明链路通了：

> 试一个真实动作：在 Hermes 里说
> ```
> hermes -z "miloco_status"
> ```
> 应返 9 项自检 ✓（含 backend / adapter / state.json / versions）。

---

## 故障排除

| 现象 | 直接修法 |
|---|---|
| `miloco-cli: command not found` | 跑 `curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh \| bash -s -- --agent-prepare` |
| `~/.openclaw/miloco/config.json: not found` | `export MILOCO_HOME=$HOME/.openclaw/miloco`，再跑 `miloco-cli service start`（会自动 init） |
| `adapter 启动失败，端口 X 未监听` | `lsof -iTCP:X -sTCP:LISTEN` 找占用 kill；或 `export ADAPTER_PORT=18790` 换端口重跑 install-hermes.sh |
| `No module named aiohttp` | `pip install aiohttp httpx croniter` 后重跑 install-hermes.sh |
| `hermes cron list` 没见 4 个 miloco 任务 | `pip install croniter` → 重跑 install-hermes.sh |
| `hermes chat` 报 401 / model 错 | 检查 `~/.hermes/config.yaml::model.api_key` |
| `miloco_im_push` 报 `no deliver target` | 看 `state.json::deliver.target`；先在 Hermes 里连一个 IM（`hermes config set telegram.bot_token ...`）后重跑 install-hermes.sh |
| `hermes plugins list` 没 miloco | 重跑 `bash plugins/hermes/install-hermes.sh`（脚本 idempotent enable） |
| 安装到一半退出（adapter 没起 / plugin 没 enable） | 重跑 `bash plugins/hermes/install-hermes.sh`（幂等自动 recover） |
| `git clone` / `git pull` 失败 | `git config --global url."https://ghproxy.com/".insteadOf https://` 走镜像 |

---

## 不要做

- 不要把 OAuth 授权码 / API key / Bearer 当文本贴回 chat —— Hermes 会 mask，你也看不到，等于让用户卡住
- 不要问"你想现在配 / 以后补 / 只绑账号"这种 4 选 1 —— 用户已经给你 `bind_url` 了，直接发
- 不要替用户跑 `hermes gateway restart` —— Hermes anti-restart-loop 会拒
- 不要在脚本失败时"先继续"，先排查清楚再继续