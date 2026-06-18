# Install Prompt for AI Agents

把下面整段（从 `---` 开始到 `---` 结束）发给 Hermes / Claude / Codex 等 AI agent，它就能照着把 miloco × Hermes 兼容层装好。

主 README 极简，agent 装的过程细节、验证步骤、Troubleshooting、回滚都写在这里。

---

请帮我把 `miloco × Hermes Agent` 兼容层装到本机的 Hermes 上。装好之后我会自己试。

**完整文档**：https://github.com/n0tssss/xiaomi-miloco/blob/main/plugins/hermes/README.md

**仓库**：https://github.com/n0tssss/xiaomi-miloco

**前置假设**（先 `command -v` 验证，缺什么告诉我）：

- 我已经装了 Hermes Agent（`~/.hermes` 存在，`hermes` CLI 在 PATH）
- 我已经装了 miloco 后端（`miloco-cli` 在 PATH，`$MILOCO_HOME/config.json` 存在，默认 `~/.openclaw/miloco/config.json`）
- `python3` 在 PATH
- 我要 `git clone` 仓库到本地（`/tmp` 或 `~/projects` 都行）

**你的步骤**：

1. `git clone https://github.com/n0tssss/xiaomi-miloco.git` 拉到本地
2. `cd xiaomi-miloco`
3. `bash plugins/hermes/install-hermes.sh` 跑一键安装。预期末尾输出 `✅ 安装完成（可重复执行，幂等）` + PID + 日志路径。这一步会做：
   - 同步 16 个 skill 到 `~/.hermes/skills/`
   - 复制插件到 `~/.hermes/plugins/miloco/miloco-plugin/`
   - 复制 adapter 到 `~/.hermes/plugins/miloco/adapter/`
   - **自动 patch** `$MILOCO_HOME/config.json` 的 `agent` 段（备份 `config.json.bak-<ts>`）
   - **自动**给 `~/.hermes/.env` 补 `API_SERVER_KEY`（如缺失则生成，存在则复用）
   - **自动 nohup 启** adapter，PID 写到 `~/.hermes/miloco-adapter.pid`，日志到 `~/.hermes/miloco-adapter.log`
4. `bash plugins/hermes/tests/test_install_e2e.sh` 跑 e2e 校验，**22 断言全过才算好**。预期末尾：`PASS: 22, FAIL: 0` + `全部通过 ✓`
5. `hermes gateway restart` 重启 Hermes 让插件和新 API_SERVER_KEY 生效
6. `hermes plugins list` 确认 `miloco` enabled
7. `ls ~/.hermes/skills/miloco-*` 确认 16 个 skill 目录
8. `bash plugins/hermes/scripts/miloco-adapter.sh status` 确认 adapter 在跑
9. **回报给我**，按下面格式：

```
✅ miloco × Hermes 兼容层已装好
  - 插件: ~/.hermes/plugins/miloco/miloco-plugin (enabled)
  - 16 个 skill: ~/.hermes/skills/miloco-*
  - adapter: PID=<pid>，端口 18789，日志 ~/.hermes/miloco-adapter.log
  - miloco config.json: webhook_url + auth_bearer 已 patch，备份 config.json.bak-<ts>
  - Hermes .env: API_SERVER_KEY 已追加

后续我试一下：
  hermes chat -q "把客厅灯打开" -Q     # 出站试
  miloco-cli device list              # 出站试（不带 hermes）
  # 真实设备控制链路需要我的 miloco 后端 + 小米账号，链路是
  # skill → miloco-cli → miloco 后端 → 小米设备，与 OpenClaw 版完全相同
```

**Troubleshooting**（如果步骤失败）：

| 现象 | 解决 |
|---|---|
| `找不到 miloco-cli` | 装 miloco 后端：`curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases/latest/download/install.sh \| bash` |
| `找不到 Hermes 目录` | 装 Hermes：`curl -fsSL https://hermes-agent.nousresearch.com/install.sh \| bash` |
| `找不到 config.json` | 设 `export MILOCO_HOME=/path/to/your/miloco/home` 再跑 |
| `adapter 启动失败，端口 X 未监听` | 端口被占：`netstat -ano \| grep X`（Windows）或 `lsof -iTCP:X -sTCP:LISTEN`（Mac/Linux）找占用者 kill；或设 `export ADAPTER_PORT=18790` 换端口 |
| `No module named aiohttp` | adapter 依赖：`pip install aiohttp httpx` |
| `No module named croniter` | Hermes v0.10.0 cron 调度依赖：`pip install croniter` |
| `hermes cron list` 没看到 4 个 miloco 任务 | `pip install croniter`，然后 `hermes gateway restart` |
| `hermes chat` 报 401 / API key 问题 | 检查 `~/.hermes/config.yaml` 的 `model.api_key` 或对应 provider 的 env |
| 装完发现 adapter 没在跑 | `bash plugins/hermes/scripts/miloco-adapter.sh logs` 看日志 |

**失败时如何回滚**（如果我让你回滚）：

```bash
# 1. 停 adapter
bash plugins/hermes/scripts/miloco-adapter.sh stop

# 2. 卸插件 + skill
rm -rf ~/.hermes/plugins/miloco
rm -rf ~/.hermes/skills/miloco-*

# 3. 还原 miloco config.json（patch 前的备份）
ls $MILOCO_HOME/config.json.bak-*
cp $MILOCO_HOME/config.json.bak-<ts> $MILOCO_HOME/config.json

# 4. 去掉 .env 里追加的 API_SERVER_KEY（编辑 ~/.hermes/.env 删最后一行 API_SERVER_KEY=...）

# 5. 重启 hermes
hermes gateway restart
```

**adapter 后续管理**：

```bash
bash plugins/hermes/scripts/miloco-adapter.sh start    # 启
bash plugins/hermes/scripts/miloco-adapter.sh stop     # 停
bash plugins/hermes/scripts/miloco-adapter.sh restart  # 重启
bash plugins/hermes/scripts/miloco-adapter.sh status   # PID / 端口 / /health
bash plugins/hermes/scripts/miloco-adapter.sh logs     # tail -f 日志
bash plugins/hermes/scripts/miloco-adapter.sh env      # 显当前 .env 里的环境变量
```

**不要做**：不要 commit、不要 push、不要改我仓库里的代码、除非我明确说要。

---
