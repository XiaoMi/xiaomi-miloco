---
name: install-miloco
description: Miloco 安装引导入口，当用户说「安装 miloco」时激活，适用任意 agent 运行平台（OpenClaw / Hermes Agent）。
---

# Miloco 安装指南（入口）

## 人工命令行安装

```bash
curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash
```

安装程序会询问装给哪个 agent 平台（仅限直接终端执行时）。通过 `curl | bash` 管道安装时，加 `--agent-platform` 指定：

```bash
curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh | bash -s -- --agent-platform=hermes
```

## Agent 执行安装

Step 0 — 先确认你自己是什么 agent 运行时，再跳转到对应的完整安装指南：

- 如果你是 **OpenClaw** → `scripts/install-guide-openclaw.md`
- 如果你是 **Hermes Agent** → `scripts/install-guide-hermes.md`
- 如果你是其他平台 → 目前仅支持这两种，如实告知用户
