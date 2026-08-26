#!/usr/bin/env python3
"""Rule tester server (FastAPI, dev-only).

A small standalone web UI that:
- Drives the V3 ``miloco-create-task`` skill via an OpenAI-compatible LLM and runs
  ``miloco-cli rule create`` as a subprocess for stage C output.
- Mocks frame-level perception triggers (``runner.update_state``) and the
  debug ``runner.trigger_rule`` entry, both via in-process import of
  :mod:`miloco.rule.runner`.
- Lists rules + recent logs from the same SQLite the production backend uses.

Usage (run inside the ``backend/miloco`` uv environment so ``miloco`` imports
resolve)::

    uv run python tests/rule_tester/server.py --port 8090

The miloco backend must be running on its own port -- ``miloco-cli rule
create`` talks to it over HTTP. The tester only shares the SQLite file.

See README.md next to this file for env var configuration.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

# Make sibling files (llm_client.py, mock_miot.py) importable regardless of how
# this script is launched (`python server.py` / `python -m`).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from llm_client import run_create_task  # noqa: E402
from miloco.database.connector import init_database  # noqa: E402
from miloco.database.rule_repo import RuleLogRepo, RuleRepo  # noqa: E402
from miloco.rule.runner import RuleRunner  # noqa: E402
from miloco.rule.schema import SCENE_IID  # noqa: E402
from mock_miot import MockMiotProxy  # noqa: E402
from pydantic import BaseModel  # noqa: E402

logger = logging.getLogger("rule_tester")

_INDEX_HTML_PATH = _HERE / "templates" / "index.html"

# Global state ---------------------------------------------------------------

# Ensure rule / rule_log tables exist before constructing repos. Tester is
# meant to work standalone (without running the backend); backend startup
# normally calls init_database itself, but we re-run it here defensively --
# it is idempotent and just runs PRAGMA user_version migrations.
init_database()

_mock_miot = MockMiotProxy()
_rule_repo = RuleRepo()
_rule_log_repo = RuleLogRepo()
_runner = RuleRunner(
    rules=_rule_repo.get_all(enabled_only=False),
    miot_proxy=_mock_miot,
    rule_log_repo=_rule_log_repo,
)
# Most recent task traces from miloco-create-task runs (newest first)
_recent_traces: list[dict[str, Any]] = []
_TRACE_CAP = 20


def _sync_runner_from_repo() -> None:
    """Reload runner._rules from the SQLite source-of-truth.

    The tester and the production backend both hold their own RuleRunner,
    each with private state. After ``miloco-cli rule create`` lands a row,
    the tester's runner must pull it in or trigger lookups will miss.
    Likewise we must drop locally-cached rules that have been deleted.
    """
    rules = _rule_repo.get_all(enabled_only=False)
    fresh_ids = {r.id for r in rules}
    for r in rules:
        _runner.add_rule(r)
        _register_scenes(r)
    for rid in [r.id for r in _runner.get_all_rules()]:
        if rid not in fresh_ids:
            _runner.remove_rule(rid)


def _register_scenes(rule) -> None:
    """把规则里出现的 scene_id 登记进 mock，否则场景动作会按「场景不存在」失败。

    真实 MiotProxy 的场景表来自云端；tester 没有云端，只能从规则本身反推。
    """
    for slot in (rule.actions, rule.on_enter_actions, rule.on_exit_actions):
        for a in slot:
            if a.iid == SCENE_IID:
                _mock_miot.register_scene(a.did, f"mock-scene-{a.did}")


# Schemas --------------------------------------------------------------------


class CreateTaskRequest(BaseModel):
    query: str


class UpdateStateRequest(BaseModel):
    source_did: str
    current_bool: bool
    context: str = ""


class TriggerRequest(BaseModel):
    context: str = ""


# App ------------------------------------------------------------------------

app = FastAPI(title="Miloco rule tester", docs_url=None, redoc_url=None)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the static SPA. All data loads via /api/* fetch from JS."""
    return HTMLResponse(_INDEX_HTML_PATH.read_text(encoding="utf-8"))


# ---- API: miloco-create-task SOP --------------------------------------------------


@app.post("/api/create-task")
async def api_create_task(req: CreateTaskRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query is empty")
    trace = await run_create_task(req.query)
    payload = trace.to_dict()
    payload["finished_at_ms"] = int(time.time() * 1000)
    _recent_traces.insert(0, payload)
    del _recent_traces[_TRACE_CAP:]
    _sync_runner_from_repo()
    return JSONResponse(payload)


# ---- API: triggers (in-process via runner) ---------------------------------


@app.post("/api/rules/{rule_id}/update-state")
async def api_update_state(rule_id: str, req: UpdateStateRequest):
    _sync_runner_from_repo()
    rule = _runner.get_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"rule {rule_id} not found")
    await _runner.update_state(
        rule_id=rule_id,
        source_did=req.source_did,
        current_bool=req.current_bool,
        context=req.context,
    )
    return {
        "ok": True,
        "rule_id": rule_id,
        "source_did": req.source_did,
        "current_bool": req.current_bool,
    }


@app.post("/api/rules/{rule_id}/trigger")
async def api_trigger(rule_id: str, req: TriggerRequest):
    _sync_runner_from_repo()
    rule = _runner.get_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"rule {rule_id} not found")
    result = await _runner.trigger_rule(rule_id, req.context)
    return {
        "ok": True,
        "rule_id": rule_id,
        "execute_result": result.model_dump(mode="json") if result else None,
    }


# ---- API: read-only inspection ---------------------------------------------


@app.get("/api/rules")
async def api_rules():
    _sync_runner_from_repo()
    return {"rules": [_serialize_rule(r) for r in _runner.get_all_rules()]}


@app.get("/api/logs")
async def api_logs(limit: int = 30, rule_id: str | None = None):
    if rule_id:
        logs = _rule_log_repo.get_by_rule_id(rule_id, limit=limit)
    else:
        logs = _rule_log_repo.get_all(limit=limit)
    return {"logs": [_serialize_log(log) for log in logs]}


@app.get("/api/miot-calls")
async def api_miot_calls(limit: int = 30):
    return {"calls": _mock_miot.recent(limit)}


@app.post("/api/miot-calls/clear")
async def api_miot_clear():
    _mock_miot.clear()
    return {"ok": True}


@app.get("/api/traces")
async def api_traces():
    return {"traces": _recent_traces}


# ---- Serialization ---------------------------------------------------------


def _serialize_rule(rule) -> dict:
    d = rule.model_dump(mode="json")
    # Hint UI which sources this rule is currently observing (per-source state
    # cache lives in the runner)
    d["_observed_sources"] = sorted(
        {key[1] for key in _runner._last_source_state.keys() if key[0] == rule.id}
    )
    d["_last_rule_state"] = _runner._last_rule_state.get(rule.id, False)
    d["_pending_exit"] = rule.id in _runner._pending_exit
    return d


def _serialize_log(log) -> dict:
    return log.model_dump(mode="json")


# ---- Entry point -----------------------------------------------------------


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Miloco rule tester (dev-only)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument(
        "--log-level", default="info", choices=["debug", "info", "warning", "error"]
    )
    args = parser.parse_args()
    _setup_logging(args.log_level)
    logger.info("rule tester starting on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
