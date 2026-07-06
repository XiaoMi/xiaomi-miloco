---
name: install-miloco-hermes
description: Miloco Hermes Agent 兼容层安装引导，当用户说 "装 miloco for Hermes" 时激活。
metadata:
  author: Miloco Team
  last_updated: 2026-06-24
---

# Miloco × Hermes Agent 安装指南

把 miloco 装到一台**已经装了 Hermes Agent** 的机器上。Hermes 兼容层在 `XiaoMi/xiaomi-miloco` 主仓的 `plugins/hermes/` 下（PR #279 合并后直接用主仓，不再走 fork）。

## 与上游 OpenClaw 安装的差异

上游 Xiaomi/xiaomi-miloco 的 `scripts/install-guide.md`（3 步：Prepare → Ask → Finish）走的是 OpenClaw 插件 + 单脚本路线。Hermes fork 走的是 **plugin in fork 仓库**路线，最大的差异在模型同步和感知引擎验证：

| 项 | 上游（OpenClaw） | hermes fork | 状态 |
|---|---|---|---|
| 单脚本安装 | `install.sh --agent-prepare/finish` 一条龙 | `install-hermes.sh` + 引导式 Step 2 | ✅ 对齐 |
| 账号 / Omni 模型配置 | `install.sh` 内部 patch | 引导用户用 `miloco-cli account authorize` / `miloco-cli config set` | ✅ 对齐 |
| **本地感知 ONNX 模型同步** | **`install.sh --agent-finish` 自动从 upstream release 下到 `~/.openclaw/miloco/models/`** | **`install-hermes.sh` Step 4.7 自动从 fork 仓库的 `backend/miloco/src/miloco/perception/models/` cp 到 `~/.openclaw/miloco/models/`，并写 `config.json::models` 字段** | ✅ **本版本补齐** |
| Step 3 验证 | 上游只验 backend /health | hermes 7 步验证（多 6/6.5/7 三步，专门验感知引擎链路） | ✅ 严格 |
| 故障排除表 | 6 条（含"模型下载失败"） | 详见下方故障排除 | ✅ 对齐 |

**为什么要补 Step 4.7**：上游 install.sh 跑完 `--agent-finish` 会自动从 Xiaomi 上游 release 下 ~80MB 的 ONNX 模型（det_4C.onnx + human_body_reid_v2.onnx + bge + silero_vad）。Hermes fork 走的是"plugin 在 fork 仓库内"路线，复用不了上游下载逻辑——**但 fork 仓库的 `backend/miloco/src/miloco/perception/models/` 目录里其实打包了同一份模型**，直接 cp 即可。这一步漏掉的话，感知引擎会因为 `models_missing` 起不来，perceive query 永远 1000 报错，**智能触发链路全断但 /health 还显示 ok**，用户毫无感知。

**为什么 Step 3 验证要加 #7（`perceive query` 真调一次）**：上游 install.sh 只验 backend `/health`，**不验感知引擎**。即便 ONNX 模型齐了，如果 Omni 模型不支持 video_url 输入（比如纯文本模型被错填到 omni 位），感知链路还是断的。**7 步验证里的第 7 步直接调一次 perceive query**，能把这类问题暴露出来。

## 用户的 3 步

```bash
git clone https://github.com/XiaoMi/xiaomi-miloco.git
cd xiaomi-miloco
bash plugins/hermes/install-hermes.sh
hermes gateway restart
```

> **Windows 用户：** 不支持原生 Windows，先装 [WSL](https://learn.microsoft.com/zh-cn/windows/wsl/install)。

---

## 引导式流程总览（agent 必读）

**这个 skill 的核心设计：单轮只发一个动作，等用户回，再发下一个。** 不要把整个 Step 2 一次性贴出来 —— 用户操作时容易漏步骤，漏了你也不知道。

**agent 工作纪律**：

- **Step 1（环境准备）**：agent 自己跑，全程不问用户，6 步依次执行，每步看 exit code。
- **Step 2（敏感操作）**：拆成 **2.1 / 2.2 / 2.3** 三个子步骤。每发一个子步骤就**停下来等用户回复**。用户回「绑好了」「配好了」「继续」之类 → agent 跑 `miloco-cli account status` / `miloco-cli config get ...` 验证 → 验证通过才发下一个子步骤；验证失败就贴 stderr 让用户排查，不要替用户猜。
- **Step 3（验证）**：5 步验证全跑完，给状态报告 + 引导用户试真实动作。

**判断当前该发哪一步的算法**：

```
if 还没跑过 install-hermes.sh → 发 Step 1 摘要（agent 自己跑）
elif 装完了但还没绑账号 → 发 Step 2.1
elif 绑了但还没配模型 → 发 Step 2.2
elif 配了但还没重启 gateway → 发 Step 2.3
elif gateway 起来了 → 发 Step 3
```

**怎么判断「装完了」**：`ls ~/.hermes/plugins/miloco/miloco-plugin/plugin.yaml 2>/dev/null` 存在就算装完。

**怎么判断「绑了 / 配了 / 重启了」**：每发一个子步骤后，agent 自跑 `miloco-cli account status` / `miloco-cli config get model.omni.api_key` / `pgrep -f "hermes gateway" || lsof -iTCP:8642 -sTCP:LISTEN` 验证。

---

## Step 1：环境准备（agent 自己跑，**全程不问用户**）

依次执行下面 6 步。**每步都看 exit code，非 0 才停下来排查**。

### 1.1 装 hermes

```bash
command -v hermes || curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
test -d "$HOME/.hermes" || { echo "缺少 ~/.hermes"; exit 1; }
```

成功标志：`hermes --version` 输出 `Hermes Agent v...`。

### 1.2 装 miloco 后端（优先用仓库内代码，不从 upstream release 拉）

```bash
# 若已有 miloco-cli 就跳过安装
command -v miloco-cli && miloco-cli --version 2>/dev/null && echo "✅ 已有 miloco-cli,跳过安装" && return 0

# 否则从**本仓库**装（不是 upstream release！）
# 注意：下面命令用的是你 clone 下来的仓库路径，不是网上的 URL
REPO_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
if [ -f "$REPO_DIR/scripts/install.sh" ]; then
  bash "$REPO_DIR/scripts/install.sh" --agent-prepare
else
  curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash -s -- --agent-prepare
fi
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
  git clone https://github.com/XiaoMi/xiaomi-miloco.git && cd xiaomi-miloco
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

**Step 1 跑完后**：用一句话告诉用户"环境装好了，下面要你做 3 件事（绑米家账号 / 配模型 / 重启 gateway），我一件一件带你做"。然后**只发 Step 2.1**，等用户回。

---

## Step 2：用户自己跑 3 个敏感操作（agent 不代跑，**逐个发**）

**机制**：Hermes chat 自动 mask 敏感值（OAuth 授权码 / API key），agent 看不到也代跑不了。

**agent 工作纪律**（**最重要**）：

1. 每发一个子步骤就**停下来等用户回复**。不要把 2.1 + 2.2 + 2.3 一次性贴出来。
2. 用户回任意完成信号（「绑好了」「配好了」「继续」「ok」「搞定了」等）→ agent 跑对应的验证命令确认 → 确认通过才发下一个子步骤。
3. 验证失败 → 把 stderr 贴给用户 + 翻故障排除表。**不要替用户瞎猜**。
4. 用户回「不知道 / 不会 / 跳过」等 → 仍然发下一个子步骤的引导，但**提醒用户当前这一步没做**，等下一步做完再回来补。
5. 用户回「算了不装」 → 立即停止 Step 2 后续，告诉他卸载方法（`rm -rf ~/.hermes/plugins/miloco ~/.hermes/skills/miloco-*` + `hermes plugins disable miloco`）。

### 2.1 米家账号

**agent 跑一下确认状态：**

```bash
miloco-cli account status
```

**判定**（**不要在 chat 里贴 raw JSON**，直接按下面的分支）：

- **已绑定（输出含 `"is_bound": true`）**：直接进 2.2，不需要发任何内容给用户。或者发一句"米家账号已绑，跳到配模型"。
- **未绑定（输出含 `"is_bound": false`）**：发下面这整段（**一次发完**就停下等用户回）：

> 下一步要绑米家账号。打开这个链接授权：
>
> {bind_url}
>
> 授权完浏览器会跳到 `mico.api.mijia.tech/login_redirect`，**URL 里 `code=` 后面那串 base64** 复制下来。
>
> 立刻你自己终端跑（base64 5 分钟过期）：
>
> ```bash
> miloco-cli account authorize <粘进去的 base64 码>
> ```
>
> 跑完告诉我「绑好了」我接着配模型。

**用户回「绑好了」/「好了」/「ok」之类 → agent 验证**：

```bash
miloco-cli account status
```

- 验证通过（`is_bound: true`）→ 进 2.2。
- 验证失败 → 贴 stderr 给用户 + 提示 5 分钟过期，让他重试（`miloco-cli account authorize` 重新触发 `bind_url`，新版 URL 是新的 base64）。

### 2.2 Omni 模型

**agent 跑一下确认状态**（一次发三条命令，结果分三段看）：

```bash
miloco-cli config get model.omni.api_key
miloco-cli config get model.omni.model
miloco-cli config get model.omni.base_url
```

**判定**：

- **三项都非空**（api_key 不为 null 且 model/base_url 不为 null）→ 进 2.3，或者发一句"模型已配，跳到重启 gateway"。
- **任一项为 null / 报错** → 发下面这整段（**一次发完**就停下等用户回）：

> 下一步要配 Omni 模型（感知引擎用），二选一：
>
> **A. 默认小米 MiMo**（推荐）
>
> key 从 https://platform.xiaomimimo.com 拿。然后你自己终端跑：
>
> ```bash
> miloco-cli config set model.omni.api_key <你的_MiMo_Key>
> ```
>
> （model = `xiaomi/mimo-v2.5`、base_url = `https://api.xiaomimimo.com/v1` 是默认值，不用设）
>
> **B. 第三方**（OpenAI / Anthropic / 自建 / 任何 OpenAI 兼容 API）
>
> 你自己终端跑（一次写完三项）：
>
> ```bash
> miloco-cli config set model.omni.model <model_name> model.omni.base_url <base_url> model.omni.api_key <api_key>
> ```
>
> 例（用 OpenAI）：
>
> ```bash
> miloco-cli config set model.omni.model gpt-4o model.omni.base_url https://api.openai.com/v1 model.omni.api_key sk-xxx
> ```
>
> 跑完告诉我「配好了」我接着重启 gateway。

**用户回「配好了」/「好了」/「ok」之类 → agent 验证**：

```bash
miloco-cli config get model.omni.api_key
```

- 验证通过（api_key 不为 null）→ 进 2.3。
- 验证失败 → 贴 stderr 给用户 + 提示「是不是粘错了 / 是不是少粘了 base64 后面的等号」之类，让他重跑。

### 2.3 重启 Hermes gateway

**agent 跑一下确认状态**：

```bash
# gateway 在跑 = 端口 8642 LISTEN
lsof -nP -iTCP:8642 -sTCP:LISTEN 2>/dev/null | head -3
```

**判定**：

- **已 LISTEN（已重启过）** → 直接进 Step 3，或者发一句"gateway 在跑，进验证"。
- **未 LISTEN** → 发下面这整段（**一次发完**就停下等用户回）：

> 最后一步，重启 Hermes gateway 让插件生效。你自己终端跑（**不要让 agent 代跑**，Hermes 有 anti-restart-loop，会拒）：
>
> ```bash
> hermes gateway restart
> ```
>
> 跑完告诉我「好了」/「继续」，我跑 5 步验证出报告。

**用户回「好了」/「继续」/「ok」 → agent 验证**：

```bash
lsof -nP -iTCP:8642 -sTCP:LISTEN 2>/dev/null | head -3
```

- 验证通过（端口 LISTEN）→ 进 Step 3。
- 验证失败 → 贴 stderr 给用户 + 提示「是不是 hermes 路径不对 / gateway 是不是没装」之类。

---

## Step 3：验证 + 报告 + 给本地链接

`install-hermes.sh` 已经跑过了，gateway 也重启了。**agent 自跑下面 5 步验证**，每步把 PASS/FAIL 打印出来。

### 3.1 7 步验证

```bash
# 1. 插件 enabled？
hermes plugins list --plain --no-bundled 2>/dev/null | grep -E "^enabled.*miloco$"   # 应有 enabled ... miloco 这一行

# 2. 16 skill 装上？
ls -d ~/.hermes/skills/miloco-* 2>/dev/null | wc -l    # 应 16

# 3. 通知投递 target 已配？
test -f "$HOME/.hermes/plugins/miloco/miloco-plugin/state.json" && \
  python3 -c "import json; d=json.load(open(r'$HOME/.hermes/plugins/miloco/miloco-plugin/state.json')); print('deliver.target =', d.get('deliver',{}).get('target'))"

# 4. adapter 在跑 + /health 200？
bash plugins/hermes/scripts/miloco-adapter.sh status
curl -sS -o /dev/null -w "adapter /health: %{http_code}\n" http://127.0.0.1:18789/health    # 应 200

# 5. backend /health 200？
curl -sS -o /dev/null -w "backend /health: %{http_code}\n" http://127.0.0.1:1810/health     # 应 200

# 6. 本地感知 ONNX 模型齐？感知引擎启不来就这一项挂
#    上游 install.sh --agent-finish 会自动下载感知模型，hermes fork
#    install-hermes.sh Step 4.7 自动同步 fork 仓库里的 ONNX 到
#    ~/.openclaw/miloco/models/，所以这一步主要验证 Step 4.7 真生效了
test -d ~/.openclaw/miloco/models && \
  ls ~/.openclaw/miloco/models/{det_4C,human_body_reid_v2,bge-small-zh-v1.5-int8,silero_vad}.onnx ~/.openclaw/miloco/models/bge-small-zh-v1.5-tokenizer.json 2>/dev/null | wc -l    # 应 5
# 6.5 config.json::models 字段指向模型目录？
python3 -c "import json; print('config.json::models =', json.load(open(r'$HOME/.openclaw/miloco/config.json')).get('models','(unset)'))"

# 7. 感知引擎真能调通？用任意一个在线摄像头试一次 perceive query
#    这一步会真正调 Omni API，能直接验证整条链路通不通。
#    失败 → 排查 Omni 模型格式 / key 是否对 / 是不是支持 video_url 输入
miloco-cli perceive devices 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
cams = [c for c in d.get('data', []) if c.get('device_type') == 'camera' and c.get('online')]
print('first_online_did =', cams[0]['did'] if cams else 'NONE')
" | tee /tmp/miloco_first_cam.txt
FIRST_DID="$(grep first_online_did /tmp/miloco_first_cam.txt | awk '{print $3}')"
if [ -n "$FIRST_DID" ] && [ "$FIRST_DID" != "NONE" ]; then
  miloco-cli perceive query --source "$FIRST_DID" --query "画面里有什么？"
fi
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
> bash plugins/hermes/scripts/miloco-status.sh versions
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
| 2.1 绑完 `account status` 仍 `is_bound: false` | base64 过期（5 分钟），重新跑 `miloco-cli account status` 取最新 `bind_url`，重新授权 |
| 2.2 配完 `config get model.omni.api_key` 仍 null | 看是不是 key 里带特殊字符（`$`、`!`、空格等）需要 escape；或重跑 set 命令 |

---

## 不要做

- 不要把 OAuth 授权码 / API key / Bearer 当文本贴回 chat —— Hermes 会 mask，你也看不到，等于让用户卡住
- 不要问"你想现在配 / 以后补 / 只绑账号"这种 4 选 1 —— 用户已经给你 `bind_url` 了，直接发
- 不要替用户跑 `hermes gateway restart` —— Hermes anti-restart-loop 会拒
- **不要把 Step 2 的 2.1 / 2.2 / 2.3 一次性全贴给用户** —— 必须一个子步骤发完就停下等回。这是这个 skill 的核心改动。
- 不要在脚本失败时"先继续"，先排查清楚再继续
