"""SQLite 连接与四表 + view 初始化。

发布版 v1 新基线。Schema 版本通过 PRAGMA user_version 管理,
后续版本升级在此处补 _MIGRATIONS 注册表 + 步进迁移函数。
v0(无版本号老 db) → 要求删 db(无法判断列集)。

v2:新增 action_ledger 表(agent 控制设备 / 播 TTS / 触发场景的持久审计)。
纯 additive CREATE,对 v1 老库走 _MIGRATIONS 步进补表,无需删 db。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

_TRACES_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
  trace_id              TEXT    NOT NULL PRIMARY KEY,
  timestamp             INTEGER NOT NULL,
  device_count          INTEGER,
  skipped               INTEGER DEFAULT 0,
  in_delay_ms           REAL,
  out_delay_ms          REAL,
  decode_ms             REAL,
  collect_ms            REAL,
  convert_ms            REAL,
  log_ms                REAL,
  cycle_total_ms        REAL,
  pipeline_total_ms     REAL,
  window_duration_ms    REAL,
  window_first_frame_recv_ms INTEGER,
  stream_lag_ms         REAL,
  gate_ms               REAL,
  gate_video_ms         REAL,
  gate_audio_ms         REAL,
  gate_video_pass       INTEGER,
  gate_audio_pass       INTEGER,
  gate_hold_pass        INTEGER,
  identity_ms           REAL,
  omni_ms               REAL,
  omni_call_count       INTEGER,
  omni_error_count      INTEGER DEFAULT 0,
  timing_detail         TEXT,
  dropped_windows_total INTEGER DEFAULT 0,
  overflow_count_total  INTEGER DEFAULT 0,
  cycle_error_msg       TEXT
);
CREATE INDEX IF NOT EXISTS idx_traces_ts ON traces(timestamp);
"""

_TRACES_DEVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces_device (
  device_trace_id   TEXT    NOT NULL PRIMARY KEY,
  cycle_id          TEXT    NOT NULL,
  timestamp         INTEGER NOT NULL,
  device_id         TEXT    NOT NULL,
  room_name         TEXT,
  decode_video_avg_ms   REAL,
  decode_audio_avg_ms   REAL,
  video_frame_count     INTEGER,
  audio_frame_count     INTEGER,
  gate_ms           REAL,
  gate_video_ms     REAL,
  gate_audio_ms     REAL,
  gate_video_pass   INTEGER,
  gate_audio_pass   INTEGER,
  gate_hold_pass    INTEGER,
  gate_skipped      INTEGER DEFAULT 0,
  gate_video_score  REAL,
  gate_audio_energy REAL,
  identity_ms       REAL,
  omni_ms                REAL,
  omni_error_code        TEXT,
  omni_retry_count       INTEGER DEFAULT 0,
  dropped_windows_count  INTEGER DEFAULT 0,
  overflow_count         INTEGER DEFAULT 0,
  max_buffer_depth       INTEGER DEFAULT 0,
  last_overflow_action   TEXT
);
CREATE INDEX IF NOT EXISTS idx_td_cycle ON traces_device(cycle_id);
CREATE INDEX IF NOT EXISTS idx_td_device_ts ON traces_device(device_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_td_room_ts ON traces_device(room_name, timestamp);
CREATE INDEX IF NOT EXISTS idx_td_ts ON traces_device(timestamp);
"""

_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  event_id    TEXT    NOT NULL PRIMARY KEY,
  timestamp   INTEGER NOT NULL,
  event_type  TEXT    NOT NULL,
  trace_id    TEXT,
  source      TEXT,
  payload     TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_trace ON events(trace_id);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(event_type, timestamp);
"""

# 每次 agent turn 一行;同 trace_id 1:N 挂在 traces 下。
# source ∈ {rule, interaction, suggestion} 区分调用来源,避免覆盖。
_AGENT_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
  run_id            TEXT    NOT NULL PRIMARY KEY,
  trace_id          TEXT    NOT NULL,
  timestamp         INTEGER NOT NULL,
  source            TEXT    NOT NULL,
  query             TEXT,
  webhook_rtt_ms    REAL,
  duration_ms       REAL,
  llm_call_count    INTEGER,
  tool_call_count   INTEGER,
  llm_total_ms      REAL,
  tool_total_ms     REAL,
  tool_max_ms       REAL,
  slowest_tool_name TEXT,
  success           INTEGER,
  error_count       INTEGER,
  error_msg         TEXT,
  jsonl_path        TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_trace ON agent_runs(trace_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_ts ON agent_runs(timestamp);
CREATE INDEX IF NOT EXISTS idx_agent_runs_source_ts ON agent_runs(source, timestamp);
CREATE INDEX IF NOT EXISTS idx_agent_runs_success ON agent_runs(success) WHERE success IS NOT NULL;
"""

# agent 每次控制设备 / 播 TTS(speaker play-text 也是 call_action)/ 触发场景写一行。
# result_code / result_msg 是设备侧执行结果(负码即失败,详见 miot.result_codes);
# value_json 存 set 值或 action in_params —— TTS 全文落这里(日志只记长度,DB 存内容)。
# source ∈ {cli, rule} 区分触发源(v3)：cli=control_device 路径 / rule=RuleRunner 直控
# (source_id=rule_id)。trace_id 目前是**预留槽**(NULL)——尚未实际串联 agent turn,
# 后续经 CLI --trace-id / X-Miloco-Trace-Id → ContextVar 串联(见 PR 后续工作)。
_ACTION_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_ledger (
  id            TEXT    NOT NULL PRIMARY KEY,
  timestamp     INTEGER NOT NULL,
  action_type   TEXT    NOT NULL,
  did           TEXT    NOT NULL,
  device_name   TEXT,
  room          TEXT,
  iid           TEXT,
  value_json    TEXT,
  result_code   INTEGER,
  result_msg    TEXT,
  success       INTEGER NOT NULL,
  error         TEXT,
  trace_id      TEXT,
  source        TEXT,
  source_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_ledger_ts ON action_ledger(timestamp);
CREATE INDEX IF NOT EXISTS idx_action_ledger_source_ts ON action_ledger(source, timestamp);
"""

_TRACES_V_VIEW = """
CREATE VIEW IF NOT EXISTS traces_v AS
SELECT
  t.*,
  CASE WHEN EXISTS(SELECT 1 FROM agent_runs WHERE trace_id = t.trace_id) THEN 1 ELSE 0 END AS has_agent_turn,
  CASE WHEN window_duration_ms > 0 THEN cycle_total_ms / window_duration_ms ELSE NULL END AS rtf,
  CASE WHEN window_duration_ms > 0 THEN pipeline_total_ms / window_duration_ms ELSE NULL END AS rtf_pipeline,
  CASE WHEN window_duration_ms > 0
       THEN (cycle_total_ms + COALESCE(in_delay_ms, 0)) / window_duration_ms
       ELSE NULL END AS rtf_e2e,
  CASE WHEN window_duration_ms > 0
       THEN (cycle_total_ms + COALESCE(in_delay_ms, 0) + COALESCE(stream_lag_ms, 0)) / window_duration_ms
       ELSE NULL END AS rtf_stream_e2e,
  CASE WHEN window_duration_ms > 0 AND omni_ms IS NOT NULL
       THEN omni_ms / window_duration_ms ELSE NULL END AS rtf_omni,
  CASE WHEN gate_video_pass = 1 OR gate_audio_pass = 1 OR gate_hold_pass = 1 THEN 1 ELSE 0 END AS gate_passed
FROM traces t;
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """打开连接,只设 connection-level PRAGMA。

    db-level PRAGMA (``journal_mode`` / ``auto_vacuum``) 是持久状态,
    set 时需要 write lock。worker 15s 长事务期间,任何新连接初始化时撞这两条
    都会卡 busy_timeout 然后报 ``database is locked``。
    所以 db-level 设置移到 ``init_schema()`` 的 fresh-build 路径,一次性写入。
    """
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA user_version").fetchone()[0]

    if cur == SCHEMA_VERSION:
        return

    if cur > SCHEMA_VERSION:
        # 防降级:db 是新版代码写过的,旧版代码不该往里写。
        raise RuntimeError(
            f"db schema v{cur} 与代码 v{SCHEMA_VERSION} 不匹配,"
            "db 版本高于代码版本,可能用了更新的 backend 写过此 db;请用对应版本启动。"
        )

    if cur == 0:
        # 全新 db,或老的不带版本号的 db。
        # 后者意味着 schema 字段集可能与当前不一致,直接覆盖会埋雷,要求删 db。
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='traces'"
        ).fetchone()
        if row:
            raise RuntimeError(
                "检测到无 schema 版本号的老 observability db,"
                "目前不提供 v0 → 当前版本的 migration,请删除 db 文件后重启。"
            )
        # db-level PRAGMA 一次性写入。auto_vacuum 必须先于任何写操作 set。
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_TRACES_SCHEMA)
        conn.executescript(_TRACES_DEVICE_SCHEMA)
        conn.executescript(_EVENTS_SCHEMA)
        conn.executescript(_AGENT_RUNS_SCHEMA)
        conn.executescript(_ACTION_LEDGER_SCHEMA)
        conn.executescript(_TRACES_V_VIEW)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return

    # cur ∈ (0, SCHEMA_VERSION):步进迁移。每步是 additive DDL(建表/加列),
    # 幂等(CREATE ... IF NOT EXISTS),做完把 user_version 推到目标步。
    for step, migrate in sorted(_MIGRATIONS.items()):
        if cur < step:
            migrate(conn)
            conn.execute(f"PRAGMA user_version = {step}")
            cur = step

    if cur != SCHEMA_VERSION:
        raise RuntimeError(
            f"db schema v{cur} → v{SCHEMA_VERSION} 无 migration 注册,"
            "请删除 db 文件后重启。"
        )


def _migrate_v2_action_ledger(conn: sqlite3.Connection) -> None:
    """v1 → v2:additive 建 action_ledger 表。"""
    conn.executescript(_ACTION_LEDGER_SCHEMA)


def _migrate_v3_action_source(conn: sqlite3.Connection) -> None:
    """v2 → v3:给 action_ledger 补触发源列 source / source_id(幂等)。

    trace_id IS NULL 分不清「手动 CLI」与「rule static 直控」——加显式 source:
    ``cli``（control_device 路径）/ ``rule``（RuleRunner._execute_action，source_id=rule_id）。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(action_ledger)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE action_ledger ADD COLUMN source TEXT")
    if "source_id" not in cols:
        conn.execute("ALTER TABLE action_ledger ADD COLUMN source_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_action_ledger_source_ts "
        "ON action_ledger(source, timestamp)"
    )


# 步进迁移注册表:{target_version: fn}。fn 只做 additive DDL,须幂等。
_MIGRATIONS = {
    2: _migrate_v2_action_ledger,
    3: _migrate_v3_action_source,
}
