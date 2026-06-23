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

The install script is idempotent: it copies the 16 miloco-\* skills to `~/.hermes/skills/`, copies the plugin + inbound adapter to `~/.hermes/plugins/miloco/`, auto-detects which IM platform you have configured in `~/.hermes/{auth.json,config.yaml}` and writes `deliver.target` into the plugin's `state.json`, patches `$MILOCO_HOME/config.json::agent` (auto-backup, keep newest 3), writes `API_SERVER_KEY` to `~/.hermes/.env`, starts the adapter (PID + log at `~/.hermes/miloco-adapter.{pid,log}`), and runs `hermes plugins enable miloco` (idempotent). Re-running the script preserves the same Bearer and restarts the adapter.

**Adapter lifecycle**:
- **macOS** — adapter runs as a `launchd` LaunchAgent (`~/Library/LaunchAgents/com.xiaomi.miloco.hermes.adapter.plist`); survives shell exit, reboots, re-installs.
- **Linux / WSL** — adapter runs as a daemonized background process (`nohup` + `< /dev/null`, fully detached from install.sh's process group).

Lifecycle wrapper: `bash plugins/hermes/scripts/miloco-adapter.sh {start|stop|restart|status|logs|env}`.

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

Inbound side: the adapter process exposes `POST /miloco/webhook` (miloco's `{action, payload}` contract), translates `action:agent` into a synchronous Hermes `/v1/chat/completions` turn with `X-Hermes-Session-Id` for session continuity, and lets the agent pick the right skill (e.g. `miloco-notify`) to respond. See `knowledge/03-features/hermes-integration.md` for the architecture and differences vs. the OpenClaw version.

**Proactive notifications** (cron / perception / task-fire → user IM) work out of the box, the same way OpenClaw's `subagent.run({deliver: true})` does: at install time, `install-hermes.sh` auto-detects which IM platform you have configured in `~/.hermes/auth.json` (connected providers) or `~/.hermes/config.yaml` (token declarations), and writes the target into the plugin's `state.json::deliver.target`. At runtime, `miloco_im_push` reads it and calls Hermes' built-in `send_message` tool — no bind protocol, no LLM cooperation required, works in cron sessions. If no IM platform is configured yet, `miloco_im_push` returns a clear `ok:false, error:"no deliver target configured"` so you know to set one up.

## Configuration

Plugin settings can be overridden via `hermes plugins list` config page or the plugin's own state file (`~/.hermes/plugins/miloco/miloco-plugin/state.json`). Leave fields empty to fall back to `$MILOCO_HOME/config.json`.

The Miloco backend must be running for the plugin to work:

```bash
miloco-cli service start
```

The adapter process (port 18789 by default) must be running for inbound Miloco callbacks to reach the agent:

```bash
bash plugins/hermes/scripts/miloco-adapter.sh status   # check
bash plugins/hermes/scripts/miloco-adapter.sh start    # start
```

Environment variables (read by the adapter, all auto-set by `install-hermes.sh`):

| Variable              | Default                 | Notes                                                    |
| --------------------- | ----------------------- | -------------------------------------------------------- |
| `ADAPTER_PORT`        | `18789`                 | matches OpenClaw's default webhook port                  |
| `ADAPTER_HOST`        | `127.0.0.1`             | set to `0.0.0.0` for container/remote deploy             |
| `ADAPTER_AUTH_BEARER` | (empty)                 | must match `$MILOCO_HOME/config.json::agent.auth_bearer` |
| `HERMES_API_URL`      | `http://127.0.0.1:8642` | Hermes api_server root                                   |
| `HERMES_API_KEY`      | (empty)                 | must match `~/.hermes/.env::API_SERVER_KEY`              |

### Notification delivery (proactive push)

The plugin's `miloco_im_push` tool reads `~/.hermes/plugins/miloco/miloco-plugin/state.json::deliver.target` and calls Hermes' built-in `send_message` tool. `install-hermes.sh` auto-fills this at install time by scanning `~/.hermes/auth.json` (real connected providers, preferred) then `~/.hermes/config.yaml` (declared tokens) for IM platforms (weixin / feishu / wecom / telegram / discord / slack / 飞书 / 企微 / signal / mattermost / etc.). If no platform is configured, the tool returns `ok:false, error:"no deliver target configured"` — fix by connecting an IM platform in Hermes (`hermes config set telegram.bot_token ...`) then either rerun `install-hermes.sh` or manually edit `state.json` to add `{"deliver": {"target": "telegram"}}`.

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
