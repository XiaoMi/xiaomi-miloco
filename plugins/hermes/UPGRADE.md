---
name: upgrade-miloco-hermes
description: Miloco × Hermes 兼容层升级协同指南，三方（hermes / miloco / plugin）任意一个升级后的标准操作流程。
metadata:
  author: Miloco Team
  version: "1.0"
  date: 2026-06-22
---

# Miloco × Hermes 升级协同指南

> **三端解耦**：Hermes Agent（运行时） / miloco（小米闭源后端） / miloco-hermes-plugin（`XiaoMi/xiaomi-miloco` 主仓的 `plugins/hermes/`，PR #279 合并后直接走主仓）三者各自独立升级，互不阻塞。任意一方升级后，按对应章节跑一遍即可，不需要全量重装。

## 怎么判断该不该升级

跑 `bash plugins/hermes/scripts/miloco-status.sh versions` 看 `versions` 子项（确定性，绕开 LLM 推断）：

```json
{
  "versions": {
    "ok": false,
    "mismatches": [
      "hermes: 装时=Hermes Agent v0.10.0 (2026.4.16) 现在=Hermes Agent v0.11.0 (2026.6.1)",
      "plugin: 装时=0.3.0 现在=0.4.0"
    ]
  }
}
```

任意一项 mismatch 都按下面"升级 X"章节跑一遍。

---

## 升级场景 1 — Hermes Agent 升级

```bash
# 1. hermes 自带的升级命令（按官方文档）
hermes update           # 拉新版 hermes

# 2. 验证新版 hermes api URL 是否变了（默认仍是 http://127.0.0.1:8642）
hermes gateway start
hermes status           # 看 api_server.url

# 3. 重启 backend（让 gateway_watch 重新加载新 URL；或手动 restart）
miloco-cli service restart

# 4. 重装 plugin 触发版本记录更新
cd <fork dir>
git pull --ff-only
bash plugins/hermes/install-hermes.sh   # 幂等；会更新 state.json::versions

# 5. 验证
bash plugins/hermes/scripts/miloco-status.sh versions   # versions 应无 mismatch
hermes -z "miloco_test_push"  # 实际推送一次（仍是 LLM-mediated；或直接看 state.json::deliver.target）
```

**adapter 自动 reload**（Phase 2.3 引入）：`gateway_watch.py` 30s 检测 `~/.hermes/gateway_state.json::api_server.url`，URL 变自动 `os.execvp` 自重启，无需手动 `restart`。

---

## 升级场景 2 — Miloco 后端升级

```bash
# 1. 重装 miloco 后端（上游 release）
curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash -s -- --agent-finish --skip-openclaw

# 2. 验证新版 miloco-cli / config.json
miloco-cli --version
miloco-cli config show   # 看 agent.webhook_url 是不是 http://127.0.0.1:18789/miloco/webhook

# 3. 重启 miloco backend（让 config.json 生效）
miloco-cli service restart

# 4. 重装 plugin 触发版本记录更新
cd <fork dir>
bash plugins/hermes/install-hermes.sh   # 幂等；不会破坏 OAuth / IM 配置

# 5. 验证
bash plugins/hermes/scripts/miloco-status.sh versions
```

⚠️ **miloco 升级会重写 `config.json`**：`install-hermes.sh` 之前的 `cp config.json config.json.bak-<ts>-pid<nsec>` 备份仍保留，rollback 用。

---

## 升级场景 3 — miloco-hermes-plugin 升级

```bash
# 1. 拉 fork 最新
cd <fork dir>
git fetch origin main
git reset --hard origin/main   # 老机器用；新 clone 跳过这步

# 2. 重跑 install-hermes.sh（幂等）
bash plugins/hermes/install-hermes.sh

# 3. 装完重启 hermes gateway（必须你自己终端跑，agent 代跑被 anti-restart-loop 拒）
hermes gateway restart

# 4. 验证
bash plugins/hermes/scripts/miloco-status.sh versions   # plugin version 应更新
```

`install-hermes.sh` 幂等保证：

- 16 个 skill 重同步（覆盖）
- plugin 目录覆盖（**新增 tool 立即可用**，已有 tool 行为不变）
- adapter 进程替换（PID 变；state.json 保留 deliver.target）
- `.env` / `config.json` 不重写
- `state.json::versions` 自动更新（含 plugin / git_commit）

---

## 升级场景 4 — 全量重装（含 OAuth 重新绑）

OAuth 授权码丢失 / IM 平台换号 / 完全推倒重来：

```bash
# 1. 卸干净
hermes plugins disable miloco
miloco-cli service stop
rm -rf ~/.hermes/plugins/miloco/miloco-plugin/state.json   # 清 deliver.target
rm -rf ~/.hermes/skills/miloco-*

# 2. 重装
cd <fork dir>
git pull --ff-only
bash plugins/hermes/install-hermes.sh

# 3. 让用户在 Hermes 里连好 IM 后，手动编辑 state.json 或首次调 miloco_im_push 走运行时绑定

# 4. 重启 hermes gateway
hermes gateway restart

# 5. 重新 OAuth / 重新配 API key（按 install-guide-hermes.md Step 2）

# 6. 验证
bash plugins/hermes/scripts/miloco-status.sh versions
hermes -z "miloco_test_push"
```

⚠️ **miloco config.json 不动**——OAuth 状态在 miloco 后端，不在我们 fork。

---

## 故障：升级后没生效

按这个顺序排查：

```bash
# 1. 看版本对比
bash plugins/hermes/scripts/miloco-status.sh versions | grep -A 5 versions

# 2. 看 plugin enabled
hermes plugins list | grep miloco

# 3. 看 backend 日志
miloco-cli service status
miloco-cli service logs | tail -30

# 4. 看 miloco backend 在不在
miloco-cli service status

# 5. 看完整自检
bash plugins/hermes/install-hermes.sh --diagnose
```

---

## 卸载

```bash
hermes plugins disable miloco
miloco-cli service stop
rm -rf ~/.hermes/plugins/miloco
rm -rf ~/.hermes/skills/miloco-*
# 删 .env 里的 API_SERVER_KEY 行（可选）
# 还原 miloco config.json（可选）：
#   cp $MILOCO_HOME/config.json.bak-<ts> $MILOCO_HOME/config.json
hermes gateway restart
```

卸载**不动** miloco 后端——它继续跑，只是没人接 Hermes。
