# miloco

Python backend for Xiaomi Miloco — a smart home perception and automation server.

## Install

```bash
uv tool install miloco
```

## Quick Start

```bash
# Start the server
miloco-backend

# Or via CLI
miloco-cli service start
```

The server listens on `http://127.0.0.1:1810` by default.

## Configuration

Config lives in `$MILOCO_HOME/config.json` (default `~/.openclaw/miloco/config.json`).

```bash
# Set AI model API key
miloco-cli config set model.omni.api_key sk-xxxxx
```

## IoT Observation (opt-in)

IoT observation is disabled by default (`iot_observation.enabled=false`). When
enabled in `$MILOCO_HOME/config.json`, the backend continuously collects supported
writable boolean/enum changes into `iot-observation.sqlite3` in the configured
workspace directory. It does not control devices, notify users, or create rules,
recommendations, or profile memories.

The existing home-dreaming Observe step reads one bounded IoT fact packet with
`miloco-cli observe batch` and submits commonalities, differences and evidence
references with `miloco-cli observe submit`. No patrol inference hook or extra
cron is installed. Restart the host plugin after upgrading to reconcile managed
cron prompts and install the updated shared skills. The legacy `observe run`
endpoint returns `retired` without analysis.

The authenticated `/api/iot-observation/batch` and `/submit` endpoints keep a
separate evidence ledger. Only a successful submission (including an empty
result) acknowledges the packet. Retries are idempotent; failed validation does
not move the cursor. Agents see no accumulation targets or maturity state.
Backend defaults require three distinct supported dates for accumulation and
seven effective episode dates spanning seven calendar dates for review eligibility;
unknown coverage never counts, and differences block eligibility. Eligibility is
not promotion: the sidecar never writes home-profile candidates or official knowledge.

Packets contain at most 48 new events, 8 context events and 8 related descriptions,
within 12 KiB UTF-8. These are byte/record limits, not exact model-token budgets.
One batch per daily Observe run can leave a backlog (`has_more`); retention loss is
reported as `lost_before_id`. No real-time or exhaustive discovery is promised.
Current collection still supports writable boolean/enum properties, not continuous
brightness values. Transport source does not identify who operated a device.
Disabled/empty/error results and missing coverage are not evidence of inactivity.
See `docs/superpowers/specs/2026-11-13-iot-observation-design.md` for the contract.

Focused validation from the repository root:

```bash
cd backend
uv sync --all-packages --all-groups --locked
uv run pytest miloco/tests/test_iot_observation_*.py -q
uv run pytest miot/tests/test_observation_health.py -q
```

Run the two Python test packages separately: both use the top-level package name
`tests`, which collides when collected together with pytest's default import mode.
The capacity test uses temporary storage and synthetic data, not live devices.

## License

For license details, please see [LICENSE.md](https://raw.githubusercontent.com/XiaoMi/xiaomi-miloco/main/LICENSE.md).

**Important Notice**: This project is limited to non-commercial use only. Without written authorization from Xiaomi Corporation, this project may not be used for developing applications, web services, or other forms of software.
