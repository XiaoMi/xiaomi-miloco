---
name: install-miloco-hermes-test
description: 【仅本地测试用】Miloco 安装引导，强制走本地 pr-hermes 仓库代码。
---

# Miloco 安装测试（本地 pr-hermes 分支）

你是 Hermes Agent。先读一遍 `scripts/install-guide-hermes.md`（仓库根目录），**唯一改动**：不要从 upstream release 装 miloco-cli，改用本仓库的。

## 核心差异

| 步骤 | 正式版 | 测试版 |
|---|---|---|
| 装 miloco-cli | `curl .../XiaoMi/.../install.sh \| bash` | `command -v miloco-cli && return 0`（已装就跳过），否则 `uv tool install ./cli --force` |

## 操作

其他步骤与 `scripts/install-guide-hermes.md` 完全一致。只改上面这一个差异：

1. 如果机器已有 miloco-cli（`command -v miloco-cli` 成功），直接跳过安装
2. 如果没有，从本仓库 cli/ 目录装：`uv tool install ./cli --force`
3. 其余全部照 guide 走

开始安装。
