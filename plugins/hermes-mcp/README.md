# Miloco MCP Server (Hermes Agent)

MCP Server for Xiaomi Miloco — wraps the Miloco REST API into MCP tools for use with Hermes Agent.

## Features

- **Device Control** — Query and control Xiaomi IoT devices
- **Camera Perception** — Real-time visual understanding via cameras
- **Task Management** — Create/manage household automation tasks
- **Home Profile** — Family member preferences and habits
- **System Admin** — Status, token usage, diagnostics

## Architecture

```
Hermes Agent ──MCP(stdio)──▶ miloco-mcp-server ──REST──▶ miloco backend (:1810)
```

## Install

```bash
cd plugins/hermes-mcp
pip install -e .
```

## Usage

### Stdio mode (for Hermes)

```bash
python -m miloco_mcp.server
```

### HTTP mode

```bash
python -m miloco_mcp.server --http --port 8001
```

## Hermes Registration

Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  miloco:
    command: "python"
    args: ["-m", "miloco_mcp.server"]
    cwd: "/path/to/xiaomi-miloco/plugins/hermes-mcp"
    env:
      MILOCO_BASE_URL: "http://127.0.0.1:1810"
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MILOCO_BASE_URL` | `http://127.0.0.1:1810` | Miloco backend URL |
| `MILOCO_TOKEN` | (auto) | Bearer token (auto-loaded from Miloco config) |
| `MILOCO_TIMEOUT` | `30` | Request timeout (seconds) |
| `MILOCO_TLS_VERIFY` | `false` | Verify TLS certificates |

## Provided MCP Tools

### Device Control
| Tool | Description |
|------|-------------|
| `device_list` | List all Xiaomi IoT devices |
| `device_spec` | View device capability definitions |
| `device_status` | Read device properties |
| `device_control` | Control devices (set properties / call actions) |
| `scene_list` | List manual scenes |
| `scene_trigger` | Trigger a scene |
| `home_info` | Home overview |
| `user_info` | Current Xiaomi account info |

### Camera Perception
| Tool | Description |
|------|-------------|
| `perceive` | One-shot visual/audio perception |
| `perception_engine_status/start/stop` | Manage continuous perception engine |
| `perception_logs` | Historical perception logs |
| `perception_cameras` | List cameras with perception status |
| `camera_perception_enable/disable` | Toggle per-camera perception |

### Tasks & Rules
| Tool | Description |
|------|-------------|
| `task_summary` | Task list summary |
| `task_get` | Task details |
| `task_update` | Update task properties |
| `task_enable/disable` | Enable/disable tasks |
| `task_delete` | Delete a task |
| `rule_logs` | Rule engine trigger logs |
| `rule_trigger` | Manually trigger a rule |
| `task_record_get` | Get task accumulation records |
| `task_record_increment` | Increment task counter |
| `task_record_event_append` | Append event to task record |

### Home Profile & Members
| Tool | Description |
|------|-------------|
| `person_list/create/update/delete` | Family member CRUD |
| `person_samples` | View registered face/body samples |
| `home_profile_list/rendered` | View home profile |
| `home_profile_write/commit` | Write and commit profile entries |

### System
| Tool | Description |
|------|-------------|
| `miloco_status` | System status |
| `token_usage_summary/daily` | LLM token usage |

## License

MIT
