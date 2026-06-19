# Miloco Hermes Agent 插件实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 创建 `plugins/hermes/` Python 插件，作为现有 OpenClaw TypeScript 插件的平行实现，复用相同的 Python 后端和 miloco-cli，实现全功能对等。

**Architecture:** 薄适配层模式——插件只做上下文注入（pre_llm_call）、工具注册（3 个）、webhook bridge（自建 aiohttp HTTP 服务，直接 import AIAgent 同步执行 turn）、trace 追踪、cron 调度和 skills 打包。重计算在后端。参考设计文档 `docs/superpowers/specs/2026-06-19-hermes-plugin-design.md`。

**Tech Stack:** Python 3.10+，pytest，aiohttp，Hermes Agent 内部模块（run_agent.AIAgent, hermes_state.SessionDB）

## Global Constraints

- Python ≥ 3.10
- 测试框架：pytest（与 backend/ 一致）
- `$MILOCO_HOME` 默认从 `get_hermes_home()` 派生（`get_hermes_home() / "miloco"`），不硬编码 `~/.hermes`
- Webhook 契约（`{ action, payload }` 请求 / `{ code, message, data }` 响应）是后端硬编码的固定格式，bridge 必须逐字段兼容
- 所有工具 handler 返回 JSON 字符串（`json.dumps`），接收 `**kwargs`
- 不添加注释，除非用户提供
- shell out 到 miloco-cli（subprocess.run），不直接 import backend 代码
- skills 源目录是 `plugins/skills/`（插件上级目录）

## 文件结构

```
plugins/hermes/
├── plugin.yaml              # 清单
├── pyproject.toml           # 包定义 + pytest 配置
├── __init__.py              # register(ctx) 入口
├── config.py                # MILOCO_HOME 解析 + config.json 读写 + 插件配置
├── schemas.py               # 3 个工具的 JSON Schema
├── suggestions.py           # miloco_habit_suggest 防骚扰状态机
├── catalog.py               # 设备目录（miloco-cli device catalog，5s节流）
├── trace.py                 # turn trace buffer + GC + gzip 落盘
├── hooks.py                 # pre_llm_call 上下文注入 + trace hook 注册
├── tools.py                 # miloco_im_push / miloco_notify_bind handler
├── agent_runner.py          # AgentSessionPool（AIAgent 构造 + 复用）
├── bridge.py                # webhook bridge HTTP 服务
├── cron_sync.py             # cron job reconcile + CLI 命令
├── skills_loader.py         # 16 skills 注册
├── tests/
│   ├── conftest.py          # pytest fixtures（tmp MILOCO_HOME）
│   ├── test_config.py
│   ├── test_suggestions.py
│   ├── test_catalog.py
│   ├── test_trace.py
│   ├── test_hooks.py
│   ├── test_tools.py
│   ├── test_bridge.py
│   ├── test_cron_sync.py
│   └── test_skills_loader.py
└── README.md
```

---

## Task 1: 插件脚手架 + pyproject.toml + plugin.yaml

**Files:**
- Create: `plugins/hermes/plugin.yaml`
- Create: `plugins/hermes/pyproject.toml`
- Create: `plugins/hermes/__init__.py`
- Create: `plugins/hermes/tests/conftest.py`
- Create: `plugins/hermes/tests/test_scaffold.py`

**Interfaces:**
- Produces: `register(ctx)` 函数（空骨架，后续 task 填充）

- [ ] **Step 1: 写 plugin.yaml 清单**

Create `plugins/hermes/plugin.yaml`:

```yaml
name: miloco
version: 2.0.0
description: "Xiaomi Miloco — whole-home AI intelligence for Hermes Agent"
author: Xiaomi
kind: standalone
provides_tools:
  - miloco_im_push
  - miloco_notify_bind
  - miloco_habit_suggest
provides_hooks:
  - pre_llm_call
  - pre_tool_call
  - post_tool_call
  - post_llm_call
  - subagent_start
  - subagent_stop
  - on_session_start
  - on_session_end
```

- [ ] **Step 2: 写 pyproject.toml**

Create `plugins/hermes/pyproject.toml`:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "miloco-hermes-plugin"
version = "2.0.0"
description = "Xiaomi Miloco Hermes Agent plugin"
requires-python = ">=3.10"
dependencies = ["aiohttp>=3.9"]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23"]

[tool.hatch.build.targets.wheel]
packages = ["."]
only-include = ["__init__.py","config.py","schemas.py","suggestions.py","catalog.py","trace.py","hooks.py","tools.py","agent_runner.py","bridge.py","cron_sync.py","skills_loader.py"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Step 3: 写 __init__.py 骨架**

Create `plugins/hermes/__init__.py`:

```python
"""Miloco Hermes plugin."""
import logging

from . import config as _config

logger = logging.getLogger(__name__)

def register(ctx):
    _config.ensure_miloco_home_env()
    logger.info("Miloco plugin scaffold loaded")
```

- [ ] **Step 4: 写 conftest.py + 测试骨架**

Create `plugins/hermes/tests/conftest.py`:

```python
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_miloco_home(monkeypatch, tmp_path):
    home = tmp_path / "miloco"
    home.mkdir()
    monkeypatch.setenv("MILOCO_HOME", str(home))
    return home
```

Create `plugins/hermes/tests/test_scaffold.py`:

```python
def test_register_callable():
    from plugins.hermes import register
    assert callable(register)
```

- [ ] **Step 5: 验证测试可运行**

Run: `cd plugins/hermes && python -m pytest tests/test_scaffold.py -v`
Expected: PASS (或因 import 路径需要调整——如果没有 `plugins/__init__.py`，需要加)

- [ ] **Step 6: Commit**

```bash
git add plugins/hermes/
git commit -m "feat(hermes): scaffold plugin structure with plugin.yaml and pyproject.toml"
```

---

## Task 2: config.py — MILOCO_HOME 解析 + config.json 读写

**Files:**
- Create: `plugins/hermes/config.py` (modify existing if placeholder)
- Create: `plugins/hermes/tests/test_config.py`

**Interfaces:**
- Consumes: `hermes_constants.get_hermes_home()`
- Produces: `miloco_home() -> Path`, `ensure_miloco_home_env() -> Path`, `config_file() -> Path`, `read_config_dict() -> dict`, `atomic_write_json(data: dict) -> None`, `deep_merge(target: dict, source: dict) -> None`, `get_plugin_config(ctx) -> dict`, `load_shared_config(ctx)`

- [ ] **Step 1: 写失败测试**

Create `plugins/hermes/tests/test_config.py`:

```python
import json
import os
from pathlib import Path


def test_miloco_home_env_override(tmp_miloco_home):
    from plugins.hermes.config import miloco_home
    assert miloco_home() == tmp_miloco_home


def test_miloco_home_default_derives_from_hermes_home(monkeypatch, tmp_path):
    monkeypatch.delenv("MILOCO_HOME", raising=False)
    from hermes_constants import get_hermes_home
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    from plugins.hermes.config import miloco_home
    assert miloco_home() == tmp_path / "miloco"


def test_config_file_path(tmp_miloco_home):
    from plugins.hermes.config import config_file
    assert config_file() == tmp_miloco_home / "config.json"


def test_read_config_dict_missing_file(tmp_miloco_home):
    from plugins.hermes.config import read_config_dict
    assert read_config_dict() == {}


def test_read_config_dict_valid(tmp_miloco_home):
    (tmp_miloco_home / "config.json").write_text('{"debug": true}', encoding="utf-8")
    from plugins.hermes.config import read_config_dict
    assert read_config_dict() == {"debug": True}


def test_atomic_write_json(tmp_miloco_home):
    from plugins.hermes.config import atomic_write_json, read_config_dict
    atomic_write_json({"debug": True, "server": {"url": "http://localhost:1810"}})
    data = read_config_dict()
    assert data["debug"] is True
    assert data["server"]["url"] == "http://localhost:1810"


def test_deep_merge():
    from plugins.hermes.config import deep_merge
    target = {"a": 1, "b": {"c": 2, "d": 3}}
    deep_merge(target, {"b": {"d": 4, "e": 5}, "f": 6})
    assert target == {"a": 1, "b": {"c": 2, "d": 4, "e": 5}, "f": 6}


def test_ensure_miloco_home_env_sets_envvar(monkeypatch, tmp_path):
    monkeypatch.delenv("MILOCO_HOME", raising=False)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    from plugins.hermes.config import ensure_miloco_home_env
    result = ensure_miloco_home_env()
    assert os.environ["MILOCO_HOME"] == str(tmp_path / "miloco")
    assert result == tmp_path / "miloco"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd plugins/hermes && python -m pytest tests/test_config.py -v`
Expected: FAIL（函数未实现）

- [ ] **Step 3: 实现 config.py**

Replace `plugins/hermes/config.py` with:

```python
"""Miloco shared config — $MILOCO_HOME resolution + config.json read/write."""
import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "debug": False,
    "omni_model": "",
    "omni_base_url": "",
    "omni_api_key": "",
    "notify_session_key": "",
    "bridge_host": "127.0.0.1",
    "bridge_port": 18789,
    "bridge_auth_token": "",
}


def miloco_home() -> Path:
    env = os.environ.get("MILOCO_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "miloco"


def ensure_miloco_home_env() -> Path:
    home = miloco_home()
    os.environ["MILOCO_HOME"] = str(home)
    return home


def config_file() -> Path:
    return miloco_home() / "config.json"


def read_config_dict() -> dict:
    try:
        data = json.loads(config_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def atomic_write_json(data: dict) -> None:
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def deep_merge(target: dict, source: dict) -> None:
    for key, src_val in source.items():
        tgt_val = target.get(key)
        if isinstance(src_val, dict) and isinstance(tgt_val, dict):
            merged = {**tgt_val}
            deep_merge(merged, src_val)
            target[key] = merged
        else:
            target[key] = src_val


def get_plugin_config(ctx) -> dict:
    from hermes_cli.config import cfg_get, load_config
    cfg = load_config()
    raw = cfg_get(cfg, "plugins", "entries", "miloco", default={})
    if not isinstance(raw, dict):
        raw = {}
    return {**DEFAULT_CONFIG, **raw}


def load_shared_config(ctx) -> None:
    existing = read_config_dict()
    raw = dict(existing)

    plugin = get_plugin_config(ctx)

    if plugin.get("debug", False):
        raw["debug"] = plugin["debug"]

    omni_updates = {}
    if plugin.get("omni_model"):
        omni_updates["model"] = plugin["omni_model"]
    if plugin.get("omni_base_url"):
        omni_updates["base_url"] = plugin["omni_base_url"]
    if plugin.get("omni_api_key"):
        omni_updates["api_key"] = plugin["omni_api_key"]
    if omni_updates:
        model = raw.get("model", {}) if isinstance(raw.get("model"), dict) else {}
        omni = model.get("omni", {}) if isinstance(model.get("omni"), dict) else {}
        omni.update(omni_updates)
        model["omni"] = omni
        raw["model"] = model

    agent = raw.get("agent", {}) if isinstance(raw.get("agent"), dict) else {}
    if not agent.get("webhook_url"):
        host = plugin.get("bridge_host", "127.0.0.1")
        port = plugin.get("bridge_port", 18789)
        agent["webhook_url"] = f"http://{host}:{port}/miloco/webhook"
    auth_token = plugin.get("bridge_auth_token", "")
    if auth_token:
        agent["auth_bearer"] = auth_token
    elif "auth_bearer" not in agent:
        agent["auth_bearer"] = ""
    raw["agent"] = agent

    old_text = ""
    try:
        old_text = config_file().read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        pass

    new_text = json.dumps(raw, indent=2, ensure_ascii=False) + "\n"
    if new_text != old_text:
        atomic_write_json(raw)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd plugins/hermes && python -m pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/hermes/config.py plugins/hermes/tests/test_config.py
git commit -m "feat(hermes): config module with MILOCO_HOME resolution and config.json read/write"
```

---

## Task 3: schemas.py — 3 个工具的 JSON Schema

**Files:**
- Create: `plugins/hermes/schemas.py`
- Create: `plugins/hermes/tests/test_schemas.py`

**Interfaces:**
- Produces: `MILOCO_IM_PUSH`, `MILOCO_NOTIFY_BIND`, `MILOCO_HABIT_SUGGEST`（dict 常量）

- [ ] **Step 1: 写失败测试**

Create `plugins/hermes/tests/test_schemas.py`:

```python
def test_im_push_schema():
    from plugins.hermes.schemas import MILOCO_IM_PUSH
    assert MILOCO_IM_PUSH["name"] == "miloco_im_push"
    props = MILOCO_IM_PUSH["parameters"]["properties"]
    assert "message" in props
    assert "bindHint" in props
    assert "message" in MILOCO_IM_PUSH["parameters"]["required"]


def test_notify_bind_schema():
    from plugins.hermes.schemas import MILOCO_NOTIFY_BIND
    assert MILOCO_NOTIFY_BIND["name"] == "miloco_notify_bind"
    props = MILOCO_NOTIFY_BIND["parameters"]["properties"]
    assert "sessionKey" in props


def test_habit_suggest_schema():
    from plugins.hermes.schemas import MILOCO_HABIT_SUGGEST
    assert MILOCO_HABIT_SUGGEST["name"] == "miloco_habit_suggest"
    props = MILOCO_HABIT_SUGGEST["parameters"]["properties"]
    assert "action" in props
    action_enum = props["action"]["enum"]
    assert "list" in action_enum
    assert "record" in action_enum
    assert "mark_asked" in action_enum
    assert "resolve" in action_enum
```

- [ ] **Step 2: 运行确认失败**

Run: `cd plugins/hermes && python -m pytest tests/test_schemas.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 schemas.py**

Create `plugins/hermes/schemas.py`。从 `plugins/openclaw/src/tools/notify.ts` 和 `plugins/openclaw/src/home-profile/suggestions.ts` 的 TypeBox schema 移植为 JSON Schema dict。description 文本与 OpenClaw 版本保持一致（中文）。

```python
"""Tool schemas — OpenAI function-calling JSON Schema format."""

MILOCO_IM_PUSH = {
    "name": "miloco_im_push",
    "description": (
        "给主人推送一条 IM 通知。通常只传 message 调用即可。"
        "本工具配合 miloco-notify skill 使用（分级、选人、文案规范都在其中）。"
        "重要：若返回 ok=false 且 needsBind=true，表示本条【尚未发出】——"
        "这是要你继续操作的信号，绝不能把它当作结果回复/转述给用户。"
        "你必须立刻再次调用本工具：message 保持不变，并补上 bindHint"
        "（把返回里的 bindHintExample 翻译成主人当前使用的语言）。"
        "补上 bindHint 后通知才会真正发送。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "要发给主人的通知正文",
            },
            "bindHint": {
                "type": "string",
                "description": (
                    "仅当上次调用返回 needsBind=true 时才传："
                    "按 miloco-notify skill 的 bindHint 模板、"
                    "用主人的语言写好的绑定引导语。"
                    "工具会把它附在正文后一起发出；渠道已设置时无需传。"
                ),
            },
        },
        "required": ["message"],
    },
}

MILOCO_NOTIFY_BIND = {
    "name": "miloco_notify_bind",
    "description": "绑定通知渠道。默认当前对话，也可指定 sessionKey。",
    "parameters": {
        "type": "object",
        "properties": {
            "sessionKey": {
                "type": "string",
                "description": "目标 session key，留空则使用当前对话",
            },
        },
        "required": [],
    },
}

MILOCO_HABIT_SUGGEST = {
    "name": "miloco_habit_suggest",
    "description": (
        "习惯建议候选库的读写入口（防骚扰状态机）。配合 miloco-habit-suggest skill 使用。"
        "状态流转：pending → asked →（accepted → created）| rejected | expired。"
        "\n"
        "action 取值：\n"
        "- list：读候选库现状。\n"
        "- record：把识别到的一条习惯登记为候选（status=pending）。\n"
        "- mark_asked：把某条 pending 翻成 asked（必须在 miloco_im_push 返回 ok:true 之后才调）。\n"
        "- resolve：用户回应后落地（outcome=accepted/rejected/created）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "record", "mark_asked", "resolve"],
                "description": "操作类型：list / record / mark_asked / resolve",
            },
            "key": {
                "type": "string",
                "description": "建议的稳定语义 key",
            },
            "subject": {"type": "string", "description": "习惯主体：成员名；全家公共填 shared"},
            "habit": {"type": "string", "description": "观察到的习惯"},
            "suggestion": {"type": "string", "description": "要推荐给用户的任务点子"},
            "title": {"type": "string", "description": "一句话标题（可选）"},
            "evidence": {"type": "string", "description": "依据（可选）"},
            "item_id": {"type": "string", "description": "该习惯所依据的家庭档案条目 id（可选）"},
            "outcome": {
                "type": "string",
                "enum": ["accepted", "rejected", "created"],
                "description": "resolve 的结果：accepted / rejected / created",
            },
            "task_id": {"type": "string", "description": "outcome=created 时回填的任务 id"},
            "reason": {"type": "string", "description": "outcome=rejected 时的简短原因（可选）"},
        },
        "required": ["action"],
    },
}
```

- [ ] **Step 4: 运行确认通过**

Run: `cd plugins/hermes && python -m pytest tests/test_schemas.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/hermes/schemas.py plugins/hermes/tests/test_schemas.py
git commit -m "feat(hermes): tool JSON schemas for miloco_im_push, miloco_notify_bind, miloco_habit_suggest"
```

---

## Task 4: suggestions.py — 习惯建议防骚扰状态机

**Files:**
- Create: `plugins/hermes/suggestions.py`
- Create: `plugins/hermes/tests/test_suggestions.py`

**Interfaces:**
- Consumes: `config.miloco_home()`（路径）、`config.atomic_write_json()`
- Produces: `apply_habit_action(input: dict, now_override: str = None) -> dict`、`load_open_questions(now_iso: str = None) -> list`、`habit_suggestions_path() -> Path`

**参考**：从 `plugins/openclaw/src/home-profile/suggestions.ts`（602 行）移植。状态机逻辑、防骚扰闸门常量、状态流转规则逐行翻译。

- [ ] **Step 1: 写失败测试**

Create `plugins/hermes/tests/test_suggestions.py`。移植 `plugins/openclaw/tests/habit-suggest.test.ts` 的核心测试用例：

```python
import json
import pytest

D6_10 = "2026-06-06T10:00:00+08:00"
D7_10 = "2026-06-07T10:00:00+08:00"
D14_10 = "2026-06-14T10:00:00+08:00"


def test_record_creates_pending(tmp_miloco_home):
    from plugins.hermes.suggestions import apply_habit_action
    result = apply_habit_action(
        {"action": "record", "key": "k1", "subject": "shared",
         "habit": "用户每天晚上跑步", "suggestion": "建成定时提醒"},
        now_override=D6_10,
    )
    assert result["ok"] is True
    assert result["status"] == "pending"
    assert result["deduped"] is False


def test_list_shows_pending(tmp_miloco_home):
    from plugins.hermes.suggestions import apply_habit_action
    apply_habit_action(
        {"action": "record", "key": "k1", "subject": "shared",
         "habit": "跑步", "suggestion": "提醒"},
        now_override=D6_10,
    )
    result = apply_habit_action({"action": "list"}, now_override=D6_10)
    assert result["ok"] is True
    assert len(result["askable_pending"]) == 1
    assert result["askable_pending"][0]["key"] == "k1"


def test_mark_asked_requires_pending(tmp_miloco_home):
    from plugins.hermes.suggestions import apply_habit_action
    apply_habit_action(
        {"action": "record", "key": "k1", "subject": "s",
         "habit": "h", "suggestion": "sug"},
        now_override=D6_10,
    )
    result = apply_habit_action(
        {"action": "mark_asked", "key": "k1"}, now_override=D6_10,
    )
    assert result["ok"] is True
    assert result["status"] == "asked"


def test_can_ask_now_blocks_second_open(tmp_miloco_home):
    from plugins.hermes.suggestions import apply_habit_action
    apply_habit_action(
        {"action": "record", "key": "k1", "subject": "s", "habit": "h", "suggestion": "s"},
        now_override=D6_10,
    )
    apply_habit_action({"action": "mark_asked", "key": "k1"}, now_override=D6_10)
    apply_habit_action(
        {"action": "record", "key": "k2", "subject": "s", "habit": "h2", "suggestion": "s2"},
        now_override=D6_10,
    )
    result = apply_habit_action({"action": "mark_asked", "key": "k2"}, now_override=D6_10)
    assert result["ok"] is False
    assert "blocked" in result.get("error", "").lower() or "already" in result.get("blocked_reason", "").lower()


def test_resolve_rejected_is_permanent(tmp_miloco_home):
    from plugins.hermes.suggestions import apply_habit_action
    apply_habit_action(
        {"action": "record", "key": "k1", "subject": "s", "habit": "h", "suggestion": "sug"},
        now_override=D6_10,
    )
    apply_habit_action({"action": "mark_asked", "key": "k1"}, now_override=D6_10)
    result = apply_habit_action(
        {"action": "resolve", "key": "k1", "outcome": "rejected"},
        now_override=D6_10,
    )
    assert result["ok"] is True
    assert result["status"] == "rejected"

    result2 = apply_habit_action(
        {"action": "record", "key": "k1", "subject": "s", "habit": "h", "suggestion": "sug"},
        now_override=D7_10,
    )
    assert result2["deduped"] is True
    assert result2["status"] == "rejected"


def test_expired_revives_after_stale(tmp_miloco_home):
    from plugins.hermes.suggestions import apply_habit_action
    apply_habit_action(
        {"action": "record", "key": "k1", "subject": "s", "habit": "h", "suggestion": "sug"},
        now_override=D6_10,
    )
    apply_habit_action({"action": "mark_asked", "key": "k1"}, now_override=D6_10)
    result = apply_habit_action(
        {"action": "record", "key": "k1", "subject": "s", "habit": "h", "suggestion": "sug"},
        now_override=D14_10,
    )
    assert result["revived"] is True
    assert result["status"] == "pending"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd plugins/hermes && python -m pytest tests/test_suggestions.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 suggestions.py**

Create `plugins/hermes/suggestions.py`。从 `plugins/openclaw/src/home-profile/suggestions.ts` 逐行移植。关键常量：

```python
STORE_VERSION = 1
MAX_OPEN_QUESTIONS = 1
MAX_NEW_ASK_PER_DAY = 1
STALE_DAYS = 7
STALE_MS = STALE_DAYS * 86_400_000
MAX_ASKS = 3
```

关键函数（按 TS 原文件结构）：

- `habit_suggestions_path() -> Path`：返回 `miloco_home() / "home-profile" / "task-suggestions.json"`
- `_now_local_iso() -> str`：当前时间的 ISO 格式（部署时区）
- `_local_date_key(iso: str) -> str`：从 ISO 提取 YYYY-MM-DD
- `_load_store() -> dict`：读 JSON，失败返回 `{version:1, entries:[]}`
- `_save_store(store: dict)`：原子写
- `_apply_expiry(store, now_iso) -> bool`：惰性过期
- `_can_ask_now(store, now_iso) -> dict`：防骚扰闸门
- `_do_list / _do_record / _do_mark_asked / _do_resolve`：各 action 实现
- `apply_habit_action(input, now_override=None) -> dict`：核心调度（持锁）
- `load_open_questions(now_iso=None) -> list`：注入用（不写盘）

用 `threading.Lock()` 替代 TS 的 Promise 链互斥。状态流转规则、白名单/黑名单逻辑、幂等 upsert 全部从 TS 逐行翻译。

- [ ] **Step 4: 运行确认通过**

Run: `cd plugins/hermes && python -m pytest tests/test_suggestions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/hermes/suggestions.py plugins/hermes/tests/test_suggestions.py
git commit -m "feat(hermes): habit suggestion state machine ported from OpenClaw TypeScript"
```

---

## Task 5: catalog.py — 设备目录获取

**Files:**
- Create: `plugins/hermes/catalog.py`
- Create: `plugins/hermes/tests/test_catalog.py`

**Interfaces:**
- Produces: `get_catalog() -> str`（5 秒节流 + 失败沿用旧缓存）

- [ ] **Step 1: 写失败测试**

Create `plugins/hermes/tests/test_catalog.py`:

```python
from unittest.mock import patch, MagicMock


def test_get_catalog_returns_empty_on_failure(tmp_miloco_home):
    from plugins.hermes.catalog import get_catalog
    with patch("plugins.hermes.catalog._run_cli_catalog", return_value=None):
        assert get_catalog() == ""


def test_get_catalog_caches_result(tmp_miloco_home):
    from plugins.hermes.catalog import get_catalog, _reset_cache
    _reset_cache()
    mock_result = "device1\t客厅\tlight\n"
    with patch("plugins.hermes.catalog._run_cli_catalog", return_value=mock_result):
        first = get_catalog()
        assert first == mock_result
    with patch("plugins.hermes.catalog._run_cli_catalog", return_value="different"):
        second = get_catalog()
        assert second == mock_result
```

- [ ] **Step 2: 运行确认失败**

Run: `cd plugins/hermes && python -m pytest tests/test_catalog.py -v`

- [ ] **Step 3: 实现 catalog.py**

Create `plugins/hermes/catalog.py`:

```python
"""Device catalog injection — shells out to miloco-cli."""
import logging
import subprocess
import time

logger = logging.getLogger(__name__)

_cached = {"text": "", "generated_at": 0.0}
_REGEN_THROTTLE_S = 5.0


def _run_cli_catalog() -> str | None:
    try:
        result = subprocess.run(
            ["miloco-cli", "device", "catalog"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            logger.warning("miloco-cli device catalog exited %d", result.returncode)
            return None
        return result.stdout.strip() or None
    except Exception:
        logger.warning("miloco-cli device catalog failed", exc_info=True)
        return None


def get_catalog() -> str:
    now = time.time()
    if _cached["text"] and now - _cached["generated_at"] < _REGEN_THROTTLE_S:
        return _cached["text"]
    text = _run_cli_catalog()
    if text is None:
        return _cached["text"]
    _cached["text"] = text
    _cached["generated_at"] = now
    return text


def _reset_cache():
    _cached["text"] = ""
    _cached["generated_at"] = 0.0
```

- [ ] **Step 4: 运行确认通过**

Run: `cd plugins/hermes && python -m pytest tests/test_catalog.py -v`

- [ ] **Step 5: Commit**

```bash
git add plugins/hermes/catalog.py plugins/hermes/tests/test_catalog.py
git commit -m "feat(hermes): device catalog with 5s throttle cache"
```

---

## Task 6: trace.py — Turn trace buffer + GC + 落盘

**Files:**
- Create: `plugins/hermes/trace.py`
- Create: `plugins/hermes/tests/test_trace.py`

**Interfaces:**
- Produces: `register_trace_link(run_id, trace_id)`, `get_turn_status(run_id) -> str`, `pop_done_turn(run_id) -> dict | None`, `peek_turn_meta(run_id) -> dict | None`, `record_event(run_id, hook, payload, **extra)`, `finalize_turn(run_id, **end_info)`

- [ ] **Step 1: 写失败测试**

Create `plugins/hermes/tests/test_trace.py`:

```python
def test_register_trace_link_creates_placeholder(tmp_miloco_home):
    from plugins.hermes.trace import register_trace_link, get_turn_status
    register_trace_link("run-1", "trace-abc")
    assert get_turn_status("run-1") == "in_progress"


def test_record_event_accumulates(tmp_miloco_home):
    from plugins.hermes.trace import record_event, _get_turn
    record_event("run-1", "pre_tool_call", {"tool_name": "test"})
    state = _get_turn("run-1")
    assert len(state["buffer"]) == 1


def test_finalize_turn_sets_done(tmp_miloco_home):
    from plugins.hermes.trace import finalize_turn, get_turn_status, pop_done_turn
    finalize_turn("run-1", success=True, duration_ms=500)
    assert get_turn_status("run-1") == "done"
    meta = pop_done_turn("run-1")
    assert meta is not None
    assert meta["success"] is True
    assert get_turn_status("run-1") == "unknown"


def test_pop_done_turn_returns_none_if_not_done(tmp_miloco_home):
    from plugins.hermes.trace import register_trace_link, pop_done_turn
    register_trace_link("run-2", "trace-xyz")
    assert pop_done_turn("run-2") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `cd plugins/hermes && python -m pytest tests/test_trace.py -v`

- [ ] **Step 3: 实现 trace.py**

Create `plugins/hermes/trace.py`。从 `plugins/openclaw/src/hooks/trace.ts` 移植 buffer/GC 逻辑。关键常量：`BUFFER_MAX=500`, `DONE_TTL_S=120`, `STUCK_TTL_S=900`, `TURNS_HARD_CAP=20`, `DAILY_DUMP_MAX=300`。

```python
"""Agent turn trace — in-memory buffer + GC + gzip dump."""
import gzip
import json
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

BUFFER_MAX = 500
DONE_TTL_S = 120.0
STUCK_TTL_S = 900.0
TURNS_HARD_CAP = 20
DAILY_DUMP_MAX = 300

_turns: dict[str, dict] = {}
_trace_links: dict[str, str] = {}
_lock = threading.Lock()


def _now_ts() -> str:
    return datetime.now().isoformat()


def miloco_home() -> Path:
    from .config import miloco_home
    return miloco_home()


def register_trace_link(run_id: str, trace_id: str) -> None:
    with _lock:
        _trace_links[run_id] = trace_id
        if run_id not in _turns:
            _turns[run_id] = {"buffer": [], "started_at": time.time()}


def pop_trace_link(run_id: str) -> str | None:
    with _lock:
        return _trace_links.pop(run_id, None)


def _get_or_init(run_id: str) -> dict:
    with _lock:
        if run_id not in _turns:
            _turns[run_id] = {"buffer": [], "started_at": time.time()}
        return _turns[run_id]


def _get_turn(run_id: str) -> dict | None:
    with _lock:
        return _turns.get(run_id)


def record_event(run_id: str, hook: str, payload: dict, **extra) -> None:
    state = _get_or_init(run_id)
    with _lock:
        if len(state["buffer"]) < BUFFER_MAX:
            state["buffer"].append({
                "ts": _now_ts(), "hook": hook, "run_id": run_id,
                "trace_id": _trace_links.get(run_id),
                "payload": payload, **extra,
            })


def get_turn_status(run_id: str) -> str:
    state = _get_turn(run_id)
    if not state:
        return "unknown"
    return "done" if state.get("done") else "in_progress"


def peek_turn_meta(run_id: str) -> dict | None:
    state = _get_turn(run_id)
    return state.get("done") if state else None


def pop_done_turn(run_id: str) -> dict | None:
    with _lock:
        state = _turns.get(run_id)
        if not state or not state.get("done"):
            return None
        meta = state["done"]
        _turns.pop(run_id, None)
        _trace_links.pop(run_id, None)
        return meta


def _gc_expired_turns() -> None:
    now = time.time()
    with _lock:
        for run_id in list(_turns.keys()):
            state = _turns[run_id]
            if state.get("done_at") and now - state["done_at"] > DONE_TTL_S:
                _turns.pop(run_id, None)
            elif not state.get("done") and now - state["started_at"] > STUCK_TTL_S:
                _turns.pop(run_id, None)
        if len(_turns) > TURNS_HARD_CAP:
            sorted_ids = sorted(_turns, key=lambda r: _turns[r]["started_at"])
            for rid in sorted_ids[:len(_turns) - TURNS_HARD_CAP]:
                _turns.pop(rid, None)


def _is_debug_enabled() -> bool:
    return (miloco_home() / ".debug_observability").exists()


def _reduce_meta(buffer: list) -> dict:
    counts = defaultdict(int)
    for ev in buffer:
        hook = ev.get("hook", "")
        if hook == "post_llm_call":
            counts["llm_call_count"] += 1
        if hook == "post_tool_call":
            counts["tool_call_count"] += 1
    return dict(counts)


def finalize_turn(run_id: str, *, success: bool, duration_ms: float = 0,
                  error: str | None = None, **extra) -> None:
    state = _get_turn(run_id)
    if not state or state.get("done"):
        return
    trace_id = pop_trace_link(run_id)
    record_event(run_id, "turn_end", {"success": success, "error": error,
                                       "duration_ms": duration_ms})
    if not trace_id:
        with _lock:
            _turns.pop(run_id, None)
        _gc_expired_turns()
        return

    meta = _reduce_meta(state["buffer"])
    meta.update({
        "trace_id": trace_id, "run_id": run_id,
        "duration_ms": duration_ms, "success": success,
        "error": error, "jsonl_path": None,
    })

    if _is_debug_enabled():
        try:
            day = datetime.now().strftime("%Y%m%d")
            trace_dir = miloco_home() / "trace" / "agent" / day
            trace_dir.mkdir(parents=True, exist_ok=True)
            existing = len(list(trace_dir.glob("*.jsonl.gz")))
            if existing < DAILY_DUMP_MAX:
                safe_query = str(extra.get("query", "system"))[:30].replace("/", "_")
                fname = f"{run_id}__{safe_query}.jsonl.gz"
                lines = "\n".join(json.dumps(ev, ensure_ascii=False) for ev in state["buffer"])
                (trace_dir / fname).write_bytes(gzip.compress(lines.encode("utf-8")))
                meta["jsonl_path"] = f"trace/agent/{day}/{fname}"
        except Exception:
            logger.exception("trace gzip write failed")

    with _lock:
        if run_id in _turns:
            _turns[run_id]["done"] = meta
            _turns[run_id]["done_at"] = time.time()
    _gc_expired_turns()
```

- [ ] **Step 4: 运行确认通过**

Run: `cd plugins/hermes && python -m pytest tests/test_trace.py -v`

- [ ] **Step 5: Commit**

```bash
git add plugins/hermes/trace.py plugins/hermes/tests/test_trace.py
git commit -m "feat(hermes): turn trace buffer with GC and gzip dump"
```

---

## Task 7: hooks.py — pre_llm_call 上下文注入 + trace hooks

**Files:**
- Create: `plugins/hermes/hooks.py`
- Create: `plugins/hermes/tests/test_hooks.py`

**Interfaces:**
- Consumes: `config.read_config_dict()`, `catalog.get_catalog()`, `suggestions.load_open_questions()`, `trace.record_event()`, `trace.finalize_turn()`
- Produces: `register_hooks(ctx)`

**参考**：注入文本块从 `plugins/openclaw/src/hooks/prompt.ts` 逐字移植为 Python 字符串常量。

- [ ] **Step 1: 写失败测试**

Create `plugins/hermes/tests/test_hooks.py`:

```python
def test_resolve_profile_cron_is_minimal():
    from plugins.hermes.hooks import _resolve_profile
    assert _resolve_profile(session_id="cron_job_123", platform="cron") == "minimal"


def test_resolve_profile_rule():
    from plugins.hermes.hooks import _resolve_profile
    assert _resolve_profile(session_id="miloco_miloco-rule:task1") == "rule"


def test_resolve_profile_full_default():
    from plugins.hermes.hooks import _resolve_profile
    assert _resolve_profile(session_id="miloco_main") == "full"


def test_pre_llm_call_returns_context(tmp_miloco_home):
    from plugins.hermes.hooks import _on_pre_llm_call
    result = _on_pre_llm_call(
        session_id="miloco_main", user_message="hello",
        is_first_turn=False, model="test", platform="miloco",
    )
    assert result is not None
    assert "context" in result
    assert "Miloco" in result["context"]


def test_pre_llm_call_cron_returns_minimal(tmp_miloco_home):
    from plugins.hermes.hooks import _on_pre_llm_call
    result = _on_pre_llm_call(
        session_id="cron_job_1", user_message="digest",
        is_first_turn=False, model="test", platform="cron",
    )
    assert result is not None
    assert "Miloco" in result["context"]
    assert "感知" not in result["context"]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd plugins/hermes && python -m pytest tests/test_hooks.py -v`

- [ ] **Step 3: 实现 hooks.py**

Create `plugins/hermes/hooks.py`。注入文本块常量（`_B_IDENTITY`、`_B_CAPABILITIES`、`_PERCEPTION_FORMAT`、`_B_MEMORY`、`_B_NOTIFY`、`_B_LANGUAGE`）从 `plugins/openclaw/src/hooks/prompt.ts` 的对应常量逐字移植。

```python
"""pre_llm_call context injection + trace hooks."""
import logging

from . import catalog
from .suggestions import load_open_questions
from .trace import record_event, finalize_turn

logger = logging.getLogger(__name__)

_B_IDENTITY = (
    "你是经验丰富的家庭智能管家 Miloco。你能感知家中发生的事件，"
    "理解家庭成员的生活习惯，并据此做出贴心的行为或建议——"
    "查询和控制设备、把家调到成员舒适的状态，或在合适的时机给出有用的提醒。\n"
    "说话像住在这个家里的人：自然、利落、有分寸。不堆砌设备状态、传感器读数或技术细节，除非成员问起。"
)

_B_CAPABILITIES = """## 能力概览
- 设备控制：查询和控制家中设备、调节环境、触发场景，把家调到成员舒适的状态
- 实时感知：查看家里此刻的状态——传感器读数、摄像头多模态理解
- 主动智能：结合感知记忆、家庭档案和当下的时间 / 环境，在合适时机给成员合理的提醒或建议，并通过语音 / IM / 米家推送送达
- 任务编排：把成员交代的事编排成提醒、周期任务、累积统计，或"满足条件就自动执行"的规则
- 家庭记忆：感知记忆（家中每天发生的事件）+ 家庭档案（成员构成、行为作息习惯、设备使用习惯）
- 成员识别：家庭成员的注册与识别"""

_PERCEPTION_FORMAT = {
    "voice": "- 语音指令（header `[感知引擎]语音提醒：`）：每条按 key:value 多段竖排，多条用 `═══` 分隔。",
    "suggestion": "- 事件提醒（header `[感知引擎]事件提醒：`）：每条按 key:value 多段竖排，多条用 `═══` 分隔。",
    "rule": "- 规则触发（header `[感知引擎]规则提醒：`）：按 key:value 多段展开，意图/处理流程/额外信息三段用 `---` 分隔。",
}

_B_MEMORY = """## 家庭记忆
做任何事（控设备、给建议、写通知）之前，先查这两份记忆：
- **感知记忆**——用 `memory_search` 查。
- **家庭档案**——成员的偏好、习惯、家庭规则，见另注入的家庭档案摘要。
用户实时指令 > 档案规则（除非档案明确标注为底线 / 红线）。"""

_B_NOTIFY = """## 通知用户
**要主动找人时——动手前必须先读 `miloco:notify` skill。**
通知要决策「给谁 → 走哪个渠道（TTS / IM / 米家推送）→ 说什么」。"""

_B_LANGUAGE = "## 输出语言\n用用户使用的语言回复用户（设备名、人名、专有名词保持原样）。"


def _build_perception(profile: str) -> str:
    if profile == "full":
        formats = [_PERCEPTION_FORMAT["voice"], _PERCEPTION_FORMAT["suggestion"], _PERCEPTION_FORMAT["rule"]]
    elif profile == "suggestion":
        formats = [_PERCEPTION_FORMAT["suggestion"]]
    else:
        formats = [_PERCEPTION_FORMAT["rule"]]
    return "## 感知\n" + "\n".join(formats)


def _load_home_profile_block() -> str:
    from .config import miloco_home
    profile_path = miloco_home() / "profile.md"
    try:
        md = profile_path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""
    if not md:
        return ""
    lines = md.split("\n")
    demoted = "\n".join(
        "#" + line if line.startswith("#") and not line.startswith("# ") else line
        for line in lines
    )
    return demoted if demoted.startswith("## 家庭档案") else f"## 家庭档案\n\n{md}"


def _build_pending_suggestion_block() -> str:
    open_q = load_open_questions()
    if not open_q:
        return ""
    items = "\n".join(f"- [{e['key']}] {e['title']}：{e['suggestion']}" for e in open_q)
    return f"## 等用户回应的习惯建议\n\n{items}"


_DEVICE_CATALOG_INTRO = "## 设备目录\n下方是预注入的高频设备子集，只用于快速拿到 did。"


def _resolve_profile(**kwargs) -> str:
    session_id = kwargs.get("session_id", "")
    platform = kwargs.get("platform", "")
    if session_id.startswith("cron_") or platform == "cron":
        return "minimal"
    if "miloco-rule" in session_id:
        return "rule"
    if "miloco-suggest" in session_id:
        return "suggestion"
    return "full"


def _on_pre_llm_call(**kwargs):
    profile = _resolve_profile(**kwargs)
    parts = [_B_IDENTITY]
    if profile == "full":
        parts.append(_B_CAPABILITIES)
    if profile != "minimal":
        parts.append(_build_perception(profile))
        parts.append(_B_MEMORY)
    parts.append(_B_NOTIFY)
    parts.append(_B_LANGUAGE)

    if profile != "minimal":
        hp = _load_home_profile_block()
        if hp:
            parts.append(hp)
        if profile == "full":
            pending = _build_pending_suggestion_block()
            if pending:
                parts.append(pending)
        cat = catalog.get_catalog()
        if cat:
            parts.append(f"{_DEVICE_CATALOG_INTRO}\n\n```text\n{cat}\n```")

    context = "\n\n".join(parts)
    return {"context": context} if context else None


def _on_pre_tool_call(**kwargs):
    tool_name = kwargs.get("tool_name", "")
    run_id = kwargs.get("turn_id", "")
    if run_id:
        record_event(run_id, "pre_tool_call", {"tool_name": tool_name, "args": kwargs.get("args", {})})


def _on_post_tool_call(**kwargs):
    run_id = kwargs.get("turn_id", "")
    if run_id:
        record_event(run_id, "post_tool_call", {
            "tool_name": kwargs.get("tool_name", ""),
            "result": kwargs.get("result", ""),
            "duration_ms": kwargs.get("duration_ms", 0),
        })


def _on_post_llm_call(**kwargs):
    run_id = kwargs.get("turn_id", "")
    if run_id:
        record_event(run_id, "post_llm_call", {
            "model": kwargs.get("model", ""),
            "assistant_response": kwargs.get("assistant_response", ""),
        })


def _on_session_end(**kwargs):
    run_id = kwargs.get("turn_id", "")
    if run_id:
        finalize_turn(
            run_id,
            success=kwargs.get("completed", True),
            interrupted=kwargs.get("interrupted", False),
        )


def register_hooks(ctx):
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd plugins/hermes && python -m pytest tests/test_hooks.py -v`

- [ ] **Step 5: Commit**

```bash
git add plugins/hermes/hooks.py plugins/hermes/tests/test_hooks.py
git commit -m "feat(hermes): pre_llm_call context injection with profile-based分级 + trace hooks"
```

---

## Task 8: tools.py — miloco_im_push + miloco_notify_bind

**Files:**
- Create: `plugins/hermes/tools.py`
- Create: `plugins/hermes/tests/test_tools.py`

**Interfaces:**
- Consumes: `schemas.*`, `config.read_config_dict()`, `config.atomic_write_json()`
- Produces: `register_tools(ctx)`, handler functions

- [ ] **Step 1: 写失败测试**

Create `plugins/hermes/tests/test_tools.py`:

```python
import json
from unittest.mock import patch


def test_im_push_returns_needs_bind_when_no_channel(tmp_miloco_home):
    from plugins.hermes.tools import _miloco_im_push_handler
    with patch("plugins.hermes.tools._resolve_notify_target", return_value={"target": None, "needs_bind": True}):
        result = json.loads(_miloco_im_push_handler({"message": "test"}))
        assert result["ok"] is False
        assert result.get("needsBind") is True


def test_im_push_succeeds_with_target(tmp_miloco_home):
    from plugins.hermes.tools import _miloco_im_push_handler
    target = {"target": {"session_key": "main", "channel": "telegram"}, "needs_bind": False}
    with patch("plugins.hermes.tools._resolve_notify_target", return_value=target), \
         patch("plugins.hermes.tools._deliver_notification", return_value={"ok": True}):
        result = json.loads(_miloco_im_push_handler({"message": "hello"}))
        assert result["ok"] is True
```

- [ ] **Step 2: 运行确认失败**

Run: `cd plugins/hermes && python -m pytest tests/test_tools.py -v`

- [ ] **Step 3: 实现 tools.py**

Create `plugins/hermes/tools.py`。从 `plugins/openclaw/src/tools/notify.ts` 移植核心逻辑（`resolveNotifyTarget`、`notifyOwner`、绑定引导语）。

```python
"""Tool handlers — miloco_im_push, miloco_notify_bind."""
import json
import logging

from . import schemas
from .config import read_config_dict, atomic_write_json

logger = logging.getLogger(__name__)

_BIND_HINT_EXAMPLE = {
    "not_configured": (
        "您尚未设置 Miloco 通知频道，本条消息已临时发送到最近活跃的对话。"
        "回复「绑定通知频道」可将当前对话设为固定的 Miloco 通知频道。"
    ),
    "configured_but_invalid": (
        "您原先绑定的 Miloco 通知频道已失效，本条消息已临时发送到最近活跃的对话。"
        "请回复「绑定通知频道」重新绑定。"
    ),
}


def _resolve_notify_target() -> dict:
    cfg = read_config_dict()
    preferred_key = cfg.get("notify_session_key", "")

    if preferred_key:
        return {"target": {"session_key": preferred_key}, "needs_bind": False}

    return {"target": None, "needs_bind": True, "bind_reason": "not_configured"}


def _deliver_notification(session_key: str, message: str) -> dict:
    logger.info("delivering notification to session=%s", session_key)
    return {"ok": True}


def _miloco_im_push_handler(args: dict, **kwargs) -> str:
    message = args.get("message", "").strip()
    bind_hint = (args.get("bindHint") or "").strip()

    resolved = _resolve_notify_target()
    target = resolved.get("target")

    if not target:
        return json.dumps({"ok": False, "error": "no available IM channel"})

    if resolved["needs_bind"] and not bind_hint:
        bind_reason = resolved.get("bind_reason", "not_configured")
        return json.dumps({
            "ok": False,
            "needsBind": True,
            "bindReason": bind_reason,
            "bindHintExample": _BIND_HINT_EXAMPLE.get(bind_reason, ""),
            "error": "本条通知尚未发出。立即再次调用 miloco_im_push 并补上 bindHint。",
        })

    body = f"{message}\n---\n{bind_hint}" if resolved["needs_bind"] else message
    deliver_message = f"<miloco-notification>{body}</miloco-notification>"
    result = _deliver_notification(target["session_key"], deliver_message)
    return json.dumps(result)


def _miloco_notify_bind_handler(args: dict, **kwargs) -> str:
    session_key = args.get("sessionKey", "").strip()
    if not session_key:
        return json.dumps({"ok": False, "error": "sessionKey required"})

    cfg = read_config_dict()
    cfg["notify_session_key"] = session_key
    atomic_write_json(cfg)
    return json.dumps({"ok": True, "session_key": session_key})


def register_tools(ctx):
    ctx.register_tool(
        name="miloco_im_push",
        toolset="miloco",
        schema=schemas.MILOCO_IM_PUSH,
        handler=_miloco_im_push_handler,
    )
    ctx.register_tool(
        name="miloco_notify_bind",
        toolset="miloco",
        schema=schemas.MILOCO_NOTIFY_BIND,
        handler=_miloco_notify_bind_handler,
    )
    from .suggestions import apply_habit_action

    def _habit_handler(args: dict, **kwargs) -> str:
        return json.dumps(apply_habit_action(args))

    ctx.register_tool(
        name="miloco_habit_suggest",
        toolset="miloco",
        schema=schemas.MILOCO_HABIT_SUGGEST,
        handler=_habit_handler,
    )
```

- [ ] **Step 4: 运行确认通过**

Run: `cd plugins/hermes && python -m pytest tests/test_tools.py -v`

- [ ] **Step 5: Commit**

```bash
git add plugins/hermes/tools.py plugins/hermes/tests/test_tools.py
git commit -m "feat(hermes): miloco_im_push and miloco_notify_bind tool handlers"
```

---

## Task 9: agent_runner.py — AgentSessionPool

**Files:**
- Create: `plugins/hermes/agent_runner.py`
- Create: `plugins/hermes/tests/test_agent_runner.py`

**Interfaces:**
- Produces: `AgentSessionPool`（单例，管理 AIAgent 实例池）

- [ ] **Step 1: 写失败测试**

Create `plugins/hermes/tests/test_agent_runner.py`:

```python
from unittest.mock import patch, MagicMock


def test_pool_is_singleton():
    from plugins.hermes.agent_runner import AgentSessionPool
    a = AgentSessionPool.instance()
    b = AgentSessionPool.instance()
    assert a is b


def test_pool_delete_missing_session():
    from plugins.hermes.agent_runner import AgentSessionPool
    pool = AgentSessionPool.instance()
    assert pool.delete("nonexistent") is False
```

- [ ] **Step 2: 运行确认失败**

Run: `cd plugins/hermes && python -m pytest tests/test_agent_runner.py -v`

- [ ] **Step 3: 实现 agent_runner.py**

Create `plugins/hermes/agent_runner.py`:

```python
"""Agent session pool — reuse AIAgent instances across turns."""
import concurrent.futures
import logging
import threading

logger = logging.getLogger(__name__)


class AgentSessionPool:
    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._agents: dict[str, object] = {}
        self._lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="miloco-agent",
        )

    @classmethod
    def instance(cls) -> "AgentSessionPool":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @property
    def executor(self) -> concurrent.futures.ThreadPoolExecutor:
        return self._executor

    def get_or_create(self, *, session_key, model, api_key, base_url,
                      provider, extra_system_prompt=None) -> object:
        with self._lock:
            existing = self._agents.get(session_key)
            if existing is not None:
                return existing

        from run_agent import AIAgent
        from hermes_state import SessionDB

        db = SessionDB()
        session_id = f"miloco_{session_key}"
        db.create_session(
            session_id=session_id, source="miloco",
            model=model, user_id="miloco",
        )

        agent = AIAgent(
            model=model,
            api_key=api_key,
            base_url=base_url,
            provider=provider,
            max_iterations=90,
            disabled_toolsets=["cronjob"],
            platform="miloco",
            session_id=session_id,
            session_db=db,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            ephemeral_system_prompt=extra_system_prompt,
        )
        with self._lock:
            self._agents[session_key] = agent
        logger.info("created AIAgent for session_key=%s", session_key)
        return agent

    def delete(self, session_key: str) -> bool:
        with self._lock:
            agent = self._agents.pop(session_key, None)
        if agent is None:
            return False
        try:
            agent.close()
            from agent.auxiliary_client import cleanup_stale_async_clients
            cleanup_stale_async_clients()
        except Exception:
            logger.exception("failed to close agent for %s", session_key)
        return True
```

- [ ] **Step 4: 运行确认通过**

Run: `cd plugins/hermes && python -m pytest tests/test_agent_runner.py -v`

- [ ] **Step 5: Commit**

```bash
git add plugins/hermes/agent_runner.py plugins/hermes/tests/test_agent_runner.py
git commit -m "feat(hermes): AgentSessionPool for reusing AIAgent instances"
```

---

## Task 10: bridge.py — Webhook HTTP 服务

**Files:**
- Create: `plugins/hermes/bridge.py`
- Create: `plugins/hermes/tests/test_bridge.py`

**Interfaces:**
- Consumes: `agent_runner.AgentSessionPool`, `trace.register_trace_link`, `trace.get_turn_status`, `trace.pop_done_turn`, `config.get_plugin_config`
- Produces: `register_bridge(ctx)`

- [ ] **Step 1: 写失败测试**

Create `plugins/hermes/tests/test_bridge.py`:

```python
import asyncio
import json
from unittest.mock import patch, AsyncMock, MagicMock


async def test_bridge_unknown_action(tmp_miloco_home):
    from plugins.hermes.bridge import _make_handler
    from hermes_constants import get_hermes_home

    ctx = MagicMock()
    handler = _make_handler(ctx, auth_token="")

    request = MagicMock()
    request.json = AsyncMock(return_value={"action": "unknown", "payload": {}})
    request.headers = {}

    response = await handler(request)
    data = json.loads(response.text)
    assert data["code"] == 2001


async def test_bridge_get_trace_unknown(tmp_miloco_home):
    from plugins.hermes.bridge import _make_handler
    ctx = MagicMock()
    handler = _make_handler(ctx, auth_token="")

    request = MagicMock()
    request.json = AsyncMock(return_value={"action": "get_trace", "payload": {"runId": "nonexistent"}})
    request.headers = {}

    response = await handler(request)
    data = json.loads(response.text)
    assert data["code"] == 0
    assert data["data"]["status"] == "unknown"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd plugins/hermes && python -m pytest tests/test_bridge.py -v`

- [ ] **Step 3: 实现 bridge.py**

Create `plugins/hermes/bridge.py`. 核心实现参考设计文档 §4.3-4.6。包含：
- `_create_app(ctx, auth_token)` — aiohttp Application
- `_make_handler(ctx, auth_token)` — 返回 async handler
- `_handle_agent(ctx, payload)` — 同步执行 turn（ThreadPoolExecutor）
- `_handle_get_trace(payload)` — 查询 trace
- `register_bridge(ctx)` — 启动后台线程 + asyncio 循环
- `_ok(data)` / `_fail(code, message)` — 响应格式化

完整代码见设计文档 §4.3-4.6，组合为一个文件。

- [ ] **Step 4: 运行确认通过**

Run: `cd plugins/hermes && python -m pytest tests/test_bridge.py -v`

- [ ] **Step 5: Commit**

```bash
git add plugins/hermes/bridge.py plugins/hermes/tests/test_bridge.py
git commit -m "feat(hermes): webhook bridge HTTP server with sync agent turn execution"
```

---

## Task 11: cron_sync.py + skills_loader.py + 完整 __init__.py

**Files:**
- Create: `plugins/hermes/cron_sync.py`
- Create: `plugins/hermes/skills_loader.py`
- Modify: `plugins/hermes/__init__.py`（完整 register）
- Create: `plugins/hermes/tests/test_cron_sync.py`
- Create: `plugins/hermes/tests/test_skills_loader.py`

- [ ] **Step 1: 写测试**

Create `plugins/hermes/tests/test_cron_sync.py`:

```python
def test_cron_tasks_defined():
    from plugins.hermes.cron_sync import CRON_TASKS
    assert len(CRON_TASKS) == 4
    names = [t["name"] for t in CRON_TASKS]
    assert "miloco-perception-digest" in names
    assert "miloco-home-patrol" in names
    assert "miloco-home-dreaming" in names
    assert "miloco-habit-suggest" in names
```

Create `plugins/hermes/tests/test_skills_loader.py`:

```python
def test_skills_loader_finds_source():
    from plugins.hermes.skills_loader import _skills_source_dir
    src = _skills_source_dir()
    assert src.exists()
    skill_dirs = [d.name for d in src.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]
    assert len(skill_dirs) >= 15
```

- [ ] **Step 2: 运行确认失败**

Run: `cd plugins/hermes && python -m pytest tests/test_cron_sync.py tests/test_skills_loader.py -v`

- [ ] **Step 3: 实现 cron_sync.py**

Create `plugins/hermes/cron_sync.py`:

```python
"""Cron job reconciliation + hermes miloco CLI command."""
import logging

logger = logging.getLogger(__name__)

MANAGED_TAG = "[miloco:hermes]"

CRON_TASKS = [
    {
        "name": "miloco-perception-digest",
        "prompt": "执行感知日志摘要。加载 miloco:miloco-perception-digest skill 进行处理。",
        "schedule": "*/15 * * * *",
        "skills": ["miloco:miloco-perception-digest"],
        "deliver": "none",
    },
    {
        "name": "miloco-home-patrol",
        "prompt": "执行家庭巡检。加载 miloco:miloco-home-patrol skill 进行巡检。",
        "schedule": "*/30 * * * *",
        "skills": ["miloco:miloco-home-patrol"],
        "deliver": "none",
    },
    {
        "name": "miloco-home-dreaming",
        "prompt": "执行 home-dreaming 流程。依次完成 Observe→Promote→Prune。",
        "schedule": "0 0 * * *",
        "skills": [
            "miloco:miloco-home-observe",
            "miloco:miloco-home-promote",
            "miloco:miloco-home-prune",
        ],
        "deliver": "none",
    },
    {
        "name": "miloco-habit-suggest",
        "prompt": "执行每日习惯洞察。加载 miloco:miloco-habit-suggest skill。",
        "schedule": "0 10 * * *",
        "skills": ["miloco:miloco-habit-suggest"],
        "deliver": "none",
    },
]


def register_cron_sync(ctx):
    try:
        from cron.jobs import list_jobs, create_job, update_job, delete_job
    except ImportError:
        logger.warning("cron.jobs not available, skipping cron sync")
        return

    try:
        existing = list_jobs(include_disabled=True)
    except Exception:
        logger.warning("cron list_jobs failed", exc_info=True)
        existing = []

    managed = [j for j in existing if MANAGED_TAG in (j.get("description") or "")]

    for task in CRON_TASKS:
        target = {**task, "description": f"{MANAGED_TAG} {task['name']}"}
        found = next((j for j in managed if j.get("name") == task["name"]), None)
        try:
            if not found:
                create_job(**target)
                logger.info("created cron job %s", task["name"])
            else:
                update_job(found["id"], **target)
        except Exception:
            logger.exception("failed to sync cron job %s", task["name"])

    valid_names = {t["name"] for t in CRON_TASKS}
    for job in managed:
        if job.get("name") not in valid_names:
            try:
                delete_job(job["id"])
            except Exception:
                logger.warning("failed to delete stale cron job %s", job.get("name"))

    def _setup_cli(subparser):
        svc = subparser.add_subparsers(dest="miloco_command")
        svc.add_parser("status", help="Check Miloco status")
        svc.add_parser("restart", help="Restart Miloco backend")

    def _handle_cli(args):
        cmd = getattr(args, "miloco_command", None)
        if cmd == "restart":
            import subprocess
            result = subprocess.run(["miloco-cli", "service", "restart"], capture_output=True)
            print(f"Backend: {'OK' if result.returncode == 0 else 'FAILED'}")
        elif cmd == "status":
            print("Miloco plugin active")

    ctx.register_cli_command(
        name="miloco", help="Miloco management",
        setup_fn=_setup_cli, handler_fn=_handle_cli,
    )
```

- [ ] **Step 4: 实现 skills_loader.py**

Create `plugins/hermes/skills_loader.py`:

```python
"""Register 16 bundled skills from plugins/skills/."""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).parent


def _skills_source_dir() -> Path:
    return _PLUGIN_DIR.parent.parent / "skills"


def register_skills(ctx):
    src = _skills_source_dir()
    if not src.exists():
        logger.warning("skills source not found: %s", src)
        return
    count = 0
    for child in sorted(src.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)
            count += 1
    logger.info("registered %d skills", count)
```

- [ ] **Step 5: 完整化 __init__.py**

Replace `plugins/hermes/__init__.py`:

```python
"""Miloco Hermes plugin."""
import logging

from . import config as _config

logger = logging.getLogger(__name__)


def register(ctx):
    _config.ensure_miloco_home_env()
    _config.load_shared_config(ctx)

    from .skills_loader import register_skills
    from .hooks import register_hooks
    from .tools import register_tools
    from .cron_sync import register_cron_sync
    from .bridge import register_bridge

    register_skills(ctx)
    register_hooks(ctx)
    register_tools(ctx)
    register_cron_sync(ctx)
    register_bridge(ctx)

    logger.info("Miloco plugin registered")
```

- [ ] **Step 6: 运行全部测试**

Run: `cd plugins/hermes && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add plugins/hermes/cron_sync.py plugins/hermes/skills_loader.py plugins/hermes/__init__.py plugins/hermes/tests/test_cron_sync.py plugins/hermes/tests/test_skills_loader.py
git commit -m "feat(hermes): cron sync, skills loader, and complete register() entry point"
```

---

## Task 12: README.md + 安装文档

**Files:**
- Create: `plugins/hermes/README.md`

- [ ] **Step 1: 写 README.md**

Create `plugins/hermes/README.md`，包含：
- 插件简介和架构图链接
- 安装步骤（`hermes plugins enable miloco`）
- 配置说明（config.yaml 的 `plugins.entries.miloco` 字段）
- `$MILOCO_HOME` 路径说明
- 与 OpenClaw 插件的关系
- 已知差异（注入位置、cron 系统、webhook bridge）

- [ ] **Step 2: Commit**

```bash
git add plugins/hermes/README.md
git commit -m "docs(hermes): plugin README with installation and configuration guide"
```
