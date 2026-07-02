---
name: install-miloco-hermes
description: Miloco Hermes Agent 兼容层安装引导，当用户说 "装 miloco for Hermes" 时激活。
metadata:
  author: Miloco Team
  last_updated: 2026-06-30
---

# Miloco × Hermes Agent 安装指南

把 Miloco 装到一台**已经装了 Hermes Agent** 的机器上。Hermes 兼容层在 fork `n0tssss/xiaomi-miloco` 的 `plugins/hermes/` 下。

> **Windows 用户：** 当前不支持原生 Windows，请先装 [WSL](https://learn.microsoft.com/zh-cn/windows/wsl/install)，在 WSL 终端中执行。

> **agent 是主操作员**：下面 4 步里 agent 自跑命令、不需要用户在终端复制粘贴（**唯一例外 Step 3 重启 gateway**，Hermes 有 anti-restart-loop，agent 不能代跑，下面会标出来）。

---

## Step 1：装插件（agent 自跑）

```bash
git clone https://github.com/n0tssss/xiaomi-miloco.git
cd xiaomi-miloco
bash plugins/hermes/install-hermes.sh
```

成功标志：脚本打印 `mark_done 9` 且 exit 0。脚本自带 `--diagnose` 子命令可重跑 12 项自检。重跑也是幂等的，自动 recover 半装残留。

---

## Step 2：配置米家账号 + Omni 模型

两个都要配，顺序：账号 → 模型。

### 2.1 米家账号

Agent 跑：

```bash
miloco-cli account status
```

- **`is_bound: true`**：进 2.2。
- **`is_bound: false`**：发用户 OAuth 链接（`miloco-cli account status` 输出里有 `bind_url`），等用户把授权后跳到 `mico.api.mijia.tech/login_redirect` 的 URL 里 `code=` 后那串 base64 贴回来。Agent 自跑：

  ```bash
  miloco-cli account authorize "<base64>"
  miloco-cli account status   # 验证 is_bound: true
  ```

base64 5 分钟过期，过期就再让用户重新拿一次。

### 2.2 Omni 模型

Agent 跑三条 `config get` 看已配状态：

```bash
miloco-cli config get model.omni.api_key
miloco-cli config get model.omni.model
miloco-cli config get model.omni.base_url
```

**未配置**：发用户下面这段让他选：

> Miloco 的感知引擎需要一个多模态大模型（Omni Model）来理解摄像头画面。
> 默认推荐 **小米 MiMo** 模型。
>
> **A. 默认 MiMo**：从 https://platform.xiaomimimo.com 拿 key，直接贴我（model = `mimo-v2.5`、base_url = `https://api.xiaomimimo.com/v1` 是默认值，不必设）。
>
> **B. 第三方多模态**（OpenAI / Anthropic / 自建 / 任何 OpenAI 兼容 API）：贴「model 名 / base_url / api_key」三个值。

用户回选 + 贴值，Agent 跑：

```bash
# A
miloco-cli config set model.omni.api_key "<key>"

# B
miloco-cli config set model.omni.model "<model>" model.omni.base_url "<base_url>" model.omni.api_key "<key>"
```

**已配置**：直接进 Step 3。

---

## Step 3：重启 gateway

Agent **不**跑这步（Hermes 有 anti-restart-loop，agent 调会把自己 session 也踢了）：

```bash
hermes gateway restart
```

用户跑完发「好了」给 agent。

---

## Step 4：验证

Agent 自跑：

```bash
bash plugins/hermes/install-hermes.sh --diagnose    # 12 项环境自检
bash plugins/hermes/scripts/miloco-status.sh        # 9 项 plugin 健康
```

可手动浏览 `http://127.0.0.1:1810/` 看 dashboard 效果。

---

## 故障排除

| 问题 | 解法 |
|---|---|
| `miloco-cli: command not found` | `curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh \| bash -s -- --agent-prepare` |
| `~/.openclaw/miloco/config.json: not found` | `export MILOCO_HOME=$HOME/.openclaw/miloco` + `miloco-cli service start`（自动 init） |
| `git clone` 失败 | `git config --global url."https://ghproxy.com/".insteadOf https://` 走镜像 |
| 装到一半退出 | `bash plugins/hermes/install-hermes.sh` 重跑（幂等自动 recover） |
| `No module named aiohttp` | `pip install aiohttp httpx croniter` 后重跑 install-hermes.sh |
| `hermes cron list` 没 4 个 miloco 任务 | `pip install croniter` + 重跑 install-hermes.sh |
| `miloco_im_push` 报 `no deliver target` | 在 Hermes 里连 IM（`hermes config set telegram.bot_token ...`）+ 重跑 install-hermes.sh |
