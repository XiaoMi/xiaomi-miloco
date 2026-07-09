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
git clone https://github.com/n0tssss/xiaomi-miloco.git -b pr-hermes ~/project/miloco/xiaomi-miloco && cd ~/project/miloco/xiaomi-miloco && bash plugins/hermes/install-hermes.sh
```

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

# 1.2 确认 miloco-cli（装 backend）  
command -v miloco-cli || curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash -s -- --agent-prepare

# 1.3 clone + 安装
git clone https://github.com/n0tssss/xiaomi-miloco.git -b pr-hermes ~/project/miloco/xiaomi-miloco
cd ~/project/miloco/xiaomi-miloco
bash plugins/hermes/install-hermes.sh
```

成功标志：exit 0 + `✅ 安装完成`。

### Step 2：用户操作（agent 逐个引导，不要一次贴完）

**2.1 绑米家账号** → `miloco-cli account status` 判是否已绑，未绑就引导 OAuth  
**2.2 配 Omni 模型** → `miloco-cli config get model.omni.api_key` 判是否已配  
**2.3 重启 gateway** → `hermes gateway restart`（用户自己跑，agent 不能代跑）

### Step 3：验证

```bash
bash plugins/hermes/tests/test_acceptance.sh
```

或手动逐项查：

```bash
hermes cron list              # 应有 4+ miloco cron，不抛异常
hermes plugins list | grep miloco  # enabled
ls ~/.hermes/skills/miloco-* | wc -l  # >=16
python3 -c "
import sys; sys.path.insert(0,'$HOME/.hermes/plugins/miloco-plugin')
from tools_notify import _detect_im_platforms_simple
print(_detect_im_platforms_simple())  # 应有非空列表
"
```

## 关键路径

| 内容 | 路径 |
|---|---|
| 配置文件 | `~/.hermes/miloco/config.json` |
| ONNX 模型 | `~/.hermes/miloco/models/` |
| 插件 | `~/.hermes/plugins/miloco/miloco-plugin/` |
| Adapter | `~/.hermes/miloco/agent_platform/hermes/adapter.py` |
| Skills | `~/.hermes/skills/miloco-*` |
| Backend 端口 | `127.0.0.1:1810` |
| 备份目录 | `~/project/miloco/backup/` |

## 故障排除

| 现象 | 修法 |
|---|---|
| `miloco-cli: command not found` | 跑 Step 1.2 的 install.sh |
| backend 启动失败 | 看 `~/.hermes/miloco/log/miloco-backend.log` |
| 感知引擎 `no_omni_api_key` | 配 `miloco-cli config set model.omni.api_key <key>` |
| `hermes cron list` 崩溃 | 重跑 install-hermes.sh（cron deliver 修复） |
| trace 找不到 | gateway 进程需设 `MILOCO_HOME=~/.hermes/miloco` 到 launchd plist |
| im_push 报 needsBind | 在 Hermes 配 IM 后跑 `hermes gateway restart`，install-hermes.sh 会自动探测 |
