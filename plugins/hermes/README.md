# miloco-hermes-plugin

Hermes Agent plugin for Xiaomi Miloco — brings smart home perception and automation into the open-source Hermes Agent runtime (community-maintained; the official Miloco installer only ships the OpenClaw plugin).

## Install

This plugin is **not bundled with the official Miloco installer** (Hermes is a third-party agent runtime, not part of the Miloco release archive). Install it from the community fork:

```bash
git clone https://github.com/n0tssss/xiaomi-miloco.git
cd xiaomi-miloco
bash plugins/hermes/install-hermes.sh
hermes gateway restart
```

The install script is idempotent: it copies the 16 miloco-\* skills to `~/.hermes/skills/`, copies the plugin to `~/.hermes/plugins/miloco/`, deploys the AgentPlatformAdapter to `$MILOCO_HOME/agent_platform/hermes/`, patches `$MILOCO_HOME/config.json::agent` (auto-backup, keep newest 3), writes `API_SERVER_KEY` to `~/.hermes/.env`, starts the backend (`miloco-cli service start`), and runs `hermes plugins enable miloco` (idempotent).

The backend runs under supervisord and is managed via `miloco-cli service {start,stop,restart,status,logs}`.

For a step-by-step guide written for an AI agent to follow (covers pre-flight checks, OAuth + API-key user-terminal steps, and verification), see [scripts/install-guide-hermes.md](../../scripts/install-guide-hermes.md).

> **Note:** the README's 3 commands install the fork, but you still need to do **3 user-terminal actions** that the agent cannot run for you (Hermes masks sensitive values + the gateway has an anti-restart-loop):
>
> 1. Bind your Xiaomi account — `miloco-cli account bind` (interactive; or browser OAuth + `miloco-cli account authorize <base64>`)
> 2. Set the Omni model API key — `miloco-cli config set model.omni.api_key "<your-key>"`
> 3. Restart Hermes gateway — `hermes gateway restart`
>
> Point Hermes at [scripts/install-guide-hermes.md](../../scripts/install-guide-hermes.md) and it will walk you through all three.

## What It Does

The plugin registers Miloco hooks and tools into Hermes, exposes an inbound webhook adapter for Miloco's callbacks, and ships the following AI skills:

| Skill                           | Description                                                      |
| ------------------------------- | ---------------------------------------------------------------- |
| `miloco-devices`                | Query and control IoT devices                                    |
| `miloco-perception`             | Visual perception and recognition                                |
| `miloco-miot-identity`          | Person / pet identity management                                 |
| `miloco-miot-admin`             | System administration and cost stats                             |
| `miloco-miot-scope`             | Permission scope management                                      |
| `miloco-miot-identity-register` | Register new identity                                            |
| `miloco-create-task`            | Task lifecycle: create / list / logs / enable / disable / update |
| `miloco-terminate-task`         | Task termination: audit log + cascade cleanup + cron pending     |
| `miloco-notify`                 | Perception anomaly response: grading + push notification         |
| `miloco-perception-digest`      | Periodic perception event digest (cron-driven)                   |
| `miloco-home-profile`           | Read/write family profile and memory                             |
| `miloco-home-observe`           | Observe home state, emit findings to memory                      |
| `miloco-home-promote`           | Promote observations into stable memory entries                  |
| `miloco-home-prune`             | Prune stale memory entries                                       |
| `miloco-home-patrol`            | Periodic home patrol (cron-driven)                               |
| `miloco-habit-suggest`          | Generate habit suggestions (cron-driven)                         |

Inbound side: the backend's `AgentPlatformAdapter` dispatches turns to Hermes via direct API calls, and `miloco_im_push` calls Hermes' `send_message` tool via `hermes send` CLI. See `knowledge/03-features/hermes-integration.md` for the architecture and differences vs. the OpenClaw version.

**Proactive notifications** (cron / perception / task-fire → user IM): `miloco_im_push` reads the plugin's `state.json::deliver.target` and calls the Hermes `send_message` tool via `hermes send` CLI. If no IM platform is configured yet, the tool returns `ok:false, error:"no deliver target configured"`. To configure a target, edit `state.json` manually or trigger `miloco_notify_bind` at runtime.

## Configuration

Plugin settings can be overridden via `hermes plugins list` config page or the plugin's own state file (`~/.hermes/plugins/miloco/miloco-plugin/state.json`). Leave fields empty to fall back to `$MILOCO_HOME/config.json`.

The Miloco backend must be running for the plugin to work:

```bash
miloco-cli service start
```

Environment variables (read by the plugin, all auto-set by `install-hermes.sh`):

| Variable              | Default                 | Notes                                                    |
| --------------------- | ----------------------- | -------------------------------------------------------- |
| `MILOCO_HOME`         | `~/.openclaw/miloco`    | miloco 后端数据目录                                      |

### Notification delivery (proactive push)

The plugin's `miloco_im_push` tool reads `~/.hermes/plugins/miloco/miloco-plugin/state.json::deliver.target` and calls `hermes send` CLI. 装好时 `deliver.target` 为空；首次调用 `miloco_im_push` 会走运行时 fallback + `needsBind` 绑定确认。也可手动编辑 `state.json` 直接写入。
target format: `platform[:chat_id[:thread_id]]` (e.g. `telegram`, `feishu:oc_xxx`, `discord:channel_id`).

## Development

```bash
# Python deps (one-time, for pytest)
pip install pytest aiohttp httpx

# Unit tests (Python contract)
pytest plugins/hermes/tests/test_*.py

# E2E install test (bash, exercises install-hermes.sh + adapter lifecycle)
bash plugins/hermes/tests/test_install_e2e.sh

# Re-sync skills from upstream source (after editing plugins/skills/miloco-*)
python plugins/hermes/scripts/sync-skills.py

# Manual / advanced install (no patch, no auto-start)
bash plugins/hermes/scripts/install.sh
```

## License

For license details, please see [LICENSE.md](https://raw.githubusercontent.com/XiaoMi/xiaomi-miloco/main/LICENSE.md).

**Important Notice**: This project is limited to non-commercial use only. Without written authorization from Xiaomi Corporation, this project may not be used for developing applications, web services, or other forms of software.
