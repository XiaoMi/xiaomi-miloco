# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
SQLite database connector
Responsible for database initialization, connection management and basic operations
"""

import logging
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from miloco.config import get_settings

# Configure logging
logger = logging.getLogger(__name__)

# 当前 schema 版本。fresh-build 直接落到此值; 老库启动时按 _SCHEMA_MIGRATIONS
# 步进跑到此值。历史基线 v1 (cron 挪出 task_link + rule 加 FK CASCADE 前)。
_DB_SCHEMA_VERSION = 2


class SQLiteConnector:
    """SQLite database connector class"""

    def __init__(self):
        """Initialize database connector"""
        settings = get_settings()
        self.db_path = settings.database_path
        self.timeout = settings.database.timeout
        self.check_same_thread = settings.database.check_same_thread
        self.isolation_level = settings.database.isolation_level
        self._connection: sqlite3.Connection | None = None

    def initialize_database(self) -> None:
        """Initialize database, create necessary directories and tables"""
        try:
            # Ensure database directory exists
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

            # Check if database file already exists
            if self.db_path.exists():
                logger.info("Database file already exists: %s", self.db_path)
                # Verify existing database connectivity and check necessary tables
                with self.get_connection() as conn:
                    # Get list of tables in current database
                    cursor = conn.cursor()
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    existing_tables = {row[0] for row in cursor.fetchall()}
                    logger.info(
                        "Found %d tables in existing database: %s",
                        len(existing_tables),
                        existing_tables,
                    )

                    # 文件存在但表为空 → 预创建的空文件(运维 touch 占位 / 测试 fixture)。
                    # 走 fresh 路径:_create_tables 会先跑 db-level PRAGMA
                    # (auto_vacuum 只对空库生效,缺表兜底建一旦先建了表就来不及了)。
                    if not existing_tables:
                        logger.info(
                            "Empty pre-created db file, running fresh init"
                        )
                        self._create_tables(conn)
                        logger.info(
                            "Database initialized successfully: %s", self.db_path
                        )
                        return

                    # Check if necessary tables exist, create if not
                    tables_created = []

                    if "kv" not in existing_tables:
                        logger.info("KV table not found, creating...")
                        self._create_kv_table(conn)
                        tables_created.append("kv")

                    if "person" not in existing_tables:
                        logger.info("Person table not found, creating...")
                        self._create_person_table(conn)
                        tables_created.append("person")

                    if "biometric" not in existing_tables:
                        logger.info("Biometric table not found, creating...")
                        self._create_biometric_table(conn)
                        tables_created.append("biometric")

                    if "perception_log" not in existing_tables:
                        logger.info("Perception log table not found, creating...")
                        self._create_perception_log_table(conn)
                        tables_created.append("perception_log")

                    if "meaningful_events" not in existing_tables:
                        logger.info("Meaningful events table not found, creating...")
                        self._create_meaningful_events_table(conn)
                        tables_created.append("meaningful_events")

                    if "rule" not in existing_tables:
                        logger.info("Rule table not found, creating...")
                        self._create_rule_table(conn)
                        tables_created.append("rule")

                    if "rule_log" not in existing_tables:
                        logger.info("Rule log table not found, creating...")
                        self._create_rule_log_table(conn)
                        tables_created.append("rule_log")

                    if "task" not in existing_tables:
                        logger.info("task table not found, creating...")
                        self._create_task_table(conn)
                        tables_created.append("task")

                    # task_link 表在 v2 已废弃 (rule 关联搬到 rule.task_id,
                    # cron 关联搬到 cron 表)。老 v1 库启动时 task_link 表已存在,
                    # 会在 _migrate_v1_to_v2 里被 DROP; v2 fresh-build 直接不建。

                    if "cron" not in existing_tables:
                        logger.info("cron table not found, creating...")
                        self._create_cron_table(conn)
                        tables_created.append("cron")

                    if "device_lru" not in existing_tables:
                        logger.info("device_lru table not found, creating...")
                        self._create_device_lru_table(conn)
                        tables_created.append("device_lru")

                    if "token_usage" not in existing_tables:
                        logger.info("token_usage table not found, creating...")
                        self._create_token_usage_table(conn)
                        tables_created.append("token_usage")

                    if "token_usage_daily" not in existing_tables:
                        logger.info("token_usage_daily table not found, creating...")
                        self._create_token_usage_daily_table(conn)
                        tables_created.append("token_usage_daily")

                    # task_record_* + task_terminate_log：跟 task / task_link 同款
                    # 缺表兜底建（不走 user_version 步进迁移）
                    for tbl, create_fn in (
                        (
                            "task_record_progress",
                            self._create_task_record_progress_table,
                        ),
                        (
                            "task_record_duration",
                            self._create_task_record_duration_table,
                        ),
                        (
                            "task_record_duration_session",
                            self._create_task_record_duration_session_table,
                        ),
                        (
                            "task_record_event",
                            self._create_task_record_event_table,
                        ),
                        (
                            "task_record_event_entry",
                            self._create_task_record_event_entry_table,
                        ),
                        (
                            "task_terminate_log",
                            self._create_task_terminate_log_table,
                        ),
                    ):
                        if tbl not in existing_tables:
                            logger.info("%s table not found, creating...", tbl)
                            create_fn(conn)
                            tables_created.append(tbl)

                    # If new tables were created, commit transaction
                    if tables_created:
                        conn.commit()
                        logger.info("Created missing tables: %s", tables_created)
                    else:
                        conn.commit()
                        logger.info("All required tables already exist")

                    # PRAGMA user_version 步进迁移: v1 老库跑一次 _migrate_v1_to_v2
                    # 到 v2, 未来 v3 时补 {3: _migrate_v2_to_v3}。函数内部
                    # 单事务原子 (业务 DML + PRAGMA user_version 同 COMMIT),
                    # 抛异常 → backend fail-fast, 运维介入。
                    current_version = cursor.execute(
                        "PRAGMA user_version"
                    ).fetchone()[0]
                    if current_version == 0:
                        # v1 老库从没显式写过 PRAGMA user_version, 但 task_link
                        # 表必然存在 (v1 schema 包含它); 反之若既没版本号也没
                        # task_link, 说明是"partial 缺表兜底 / fresh 后手工干预"
                        # 场景, 直接钉到当前基线, 不跑迁移。
                        if "task_link" in existing_tables:
                            current_version = 1
                            conn.execute("PRAGMA user_version = 1")
                            conn.commit()
                        else:
                            conn.execute(
                                f"PRAGMA user_version = {_DB_SCHEMA_VERSION}"
                            )
                            conn.commit()
                            current_version = _DB_SCHEMA_VERSION
                    for target in range(
                        current_version + 1, _DB_SCHEMA_VERSION + 1
                    ):
                        logger.info(
                            "running schema migration to v%d", target
                        )
                        _SCHEMA_MIGRATIONS[target](conn)

                    logger.info("Database loaded successfully: %s", self.db_path)
            else:
                logger.info(
                    "Database file does not exist, creating new database: %s",
                    self.db_path,
                )
                # Create database connection and initialize table structure
                with self.get_connection() as conn:
                    self._create_tables(conn)
                    logger.info("Database initialized successfully: %s", self.db_path)

        except Exception as e:
            logger.error("Database initialization failed: %s", e)
            raise

    def _create_tables(self, conn: sqlite3.Connection) -> None:
        """Create database table structure"""
        # db-level PRAGMA 在 fresh-build 路径一次性写入。
        # auto_vacuum 必须先于任何写操作 set 且只对空库生效;
        # journal_mode 是 db header 持久状态,设一次永久。
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("PRAGMA journal_mode=WAL")
        self._create_kv_table(conn)
        self._create_person_table(conn)
        self._create_biometric_table(conn)
        self._create_perception_log_table(conn)
        self._create_meaningful_events_table(conn)
        self._create_task_table(conn)
        self._create_rule_table(conn)
        self._create_rule_log_table(conn)
        self._create_cron_table(conn)
        self._create_device_lru_table(conn)
        self._create_token_usage_table(conn)
        self._create_token_usage_daily_table(conn)
        self._create_task_record_progress_table(conn)
        self._create_task_record_duration_table(conn)
        self._create_task_record_duration_session_table(conn)
        self._create_task_record_event_table(conn)
        self._create_task_record_event_entry_table(conn)
        self._create_task_terminate_log_table(conn)
        conn.execute(f"PRAGMA user_version = {_DB_SCHEMA_VERSION}")
        conn.commit()
        logger.info("Database table structure created successfully")

    def _create_kv_table(self, conn: sqlite3.Connection) -> None:
        """Create key-value table"""
        cursor = conn.cursor()

        # Create key-value table for storing general key-value data
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS kv (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                value TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)

        # Create index to improve query performance
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kv_key ON kv(key)")
        logger.info("KV table created successfully")

    def _create_person_table(self, conn: sqlite3.Connection) -> None:
        """Create person table"""
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS person (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                role TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
        # name 是人物真名、应用层唯一标识，用唯一索引在 SQL 层钉死单一事实源；
        # role 是可空的家庭角色（爸爸/妈妈），允许多人重复，不建唯一索引。
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_person_name_unique ON person(name)"
        )
        logger.info("Person table created successfully")

    def _create_biometric_table(self, conn: sqlite3.Connection) -> None:
        """Create biometric table.

        v2 起本表实际**已停用**:新流程"该人是否录了人脸/身形"由文件系统
        ``identity_lib/persons/<id>/tier_a/{body,face}_*`` 图像表达,**没有
        任何代码再往这张表里写入**,person_repo 也不再 JOIN 它。建表代码保
        留是为了不强制做 schema migration(老 DB 已经创建过,留着零成本);
        新 DB 可以建空表,不影响任何功能。后续如需重启人脸/声纹独立注册流
        程可复用此 schema。
        """
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS biometric (
                id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                type TEXT NOT NULL,  -- v2 已废弃, 'face' | 'voice' | 'body_appearance'
                created_at INTEGER,
                FOREIGN KEY (person_id) REFERENCES person(id) ON DELETE CASCADE
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_biometric_person_id ON biometric(person_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_biometric_type ON biometric(type)"
        )
        logger.info("Biometric table created successfully")

    def _create_perception_log_table(self, conn: sqlite3.Connection) -> None:
        """Create perception log table"""
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS perception_log (
                id TEXT PRIMARY KEY,
                timestamp INTEGER NOT NULL,
                descriptions TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_perception_log_timestamp ON perception_log(timestamp)"
        )
        logger.info("Perception log table created successfully")

    def _create_meaningful_events_table(self, conn: sqlite3.Connection) -> None:
        """Create meaningful_events table.

        每次推理 = 一行 event(同窗口 N 摄像头合并 1 行,device_ids JSON 记录参与摄像头).
        schema_version 字段(行级)标识本行按哪个 schema 版本写入;本期 INSERT 恒写 1,
        后续 ALTER TABLE 加字段时新行写 2,DAO 读取按版本走兼容分支.
        """
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS meaningful_events (
                id              TEXT PRIMARY KEY,
                schema_version  INTEGER NOT NULL DEFAULT 1,
                timestamp       INTEGER NOT NULL,
                text            TEXT NOT NULL,
                payload_json    TEXT NOT NULL,
                has_rule_hit    INTEGER NOT NULL DEFAULT 0,
                has_suggestion  INTEGER NOT NULL DEFAULT 0,
                has_asr         INTEGER NOT NULL DEFAULT 0,
                snapshot_count  INTEGER NOT NULL DEFAULT 0,
                device_ids      TEXT NOT NULL DEFAULT '[]',
                rule_names      TEXT NOT NULL DEFAULT '{}',
                home_id         TEXT,
                created_at      INTEGER NOT NULL
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_meaningful_events_created_at "
            "ON meaningful_events(created_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_meaningful_events_timestamp "
            "ON meaningful_events(timestamp DESC)"
        )
        logger.info("Meaningful events table created successfully")

    def _create_rule_table(self, conn: sqlite3.Connection) -> None:
        """Create rule table for the rule system (V3 schema, v2 form).

        See rule-design.md §4.1 for column semantics.

        actions / on_enter_actions / on_exit_actions store JSON lists of
        RuleAction objects ({did, iid, value/params, idempotent,
        cooldown_minutes}).

        v2 form: task_id NOT NULL + FK CASCADE to task. Old v1 databases go
        through _migrate_v1_to_v2 table-rebuild.
        """
        cursor = conn.cursor()
        # duration_ratio DEFAULT 0.8 是历史值,与应用层 settings 0.6 故意不同步;
        # 详见 rule_repo._DURATION_RATIO_DB_FALLBACK 注释。
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rule (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                task_id TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'event',
                lifecycle TEXT NOT NULL DEFAULT 'permanent',
                enabled BOOLEAN DEFAULT 1,
                condition TEXT NOT NULL,
                actions TEXT NOT NULL DEFAULT '[]',
                action_descriptions TEXT NOT NULL DEFAULT '[]',
                on_enter_actions TEXT NOT NULL DEFAULT '[]',
                on_enter_desc TEXT,
                on_exit_actions TEXT NOT NULL DEFAULT '[]',
                on_exit_desc TEXT,
                on_target_desc TEXT,
                terminate_when TEXT,
                exit_debounce_seconds INTEGER NOT NULL DEFAULT 60,
                duration_seconds INTEGER,
                duration_ratio REAL NOT NULL DEFAULT 0.8,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (task_id) REFERENCES task(task_id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rule_name ON rule(name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rule_task_id ON rule(task_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rule_enabled ON rule(enabled)")
        logger.info("Rule table created successfully")

    def _create_cron_table(self, conn: sqlite3.Connection) -> None:
        """Create cron table — schedule (cron / at / every) 主表。

        dispatch_owner='internal' 表示 backend 完整管理 (APScheduler 建 in-memory
        job 触发); ='external' 表示 backend 只存引用, 触发仍走 openclaw 老通路。
        internal 行 name/kind/message 等业务字段严格 NOT NULL; external 允许 NULL。
        schema 详见 _local/schedule-migration-plan.md § 3.4。
        """
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cron (
                cron_id       TEXT PRIMARY KEY,
                task_id       TEXT,
                dispatch_owner TEXT NOT NULL DEFAULT 'internal'
                    CHECK (dispatch_owner IN ('internal', 'external')),

                name          TEXT,
                kind          TEXT,
                cron_expr     TEXT,
                at_ms         INTEGER,
                every_ms      INTEGER,
                anchor_ms     INTEGER,
                tz            TEXT,
                message       TEXT,

                light_context INTEGER NOT NULL DEFAULT 0
                    CHECK (light_context IN (0, 1)),
                max_delay_seconds INTEGER,

                enabled       INTEGER NOT NULL DEFAULT 1
                    CHECK (enabled IN (0, 1)),
                fired_at      INTEGER,
                retry_attempt INTEGER NOT NULL DEFAULT 0
                    CHECK (retry_attempt >= 0),
                created_at    INTEGER NOT NULL,
                updated_at    INTEGER NOT NULL,

                CHECK (
                    dispatch_owner = 'external' OR
                    (name IS NOT NULL AND kind IS NOT NULL AND message IS NOT NULL AND
                     ((kind='cron'  AND cron_expr IS NOT NULL AND at_ms IS NULL AND every_ms IS NULL AND anchor_ms IS NULL) OR
                      (kind='at'    AND at_ms     IS NOT NULL AND cron_expr IS NULL AND every_ms IS NULL AND anchor_ms IS NULL) OR
                      (kind='every' AND every_ms  IS NOT NULL AND every_ms >= 60000 AND tz IS NULL AND cron_expr IS NULL AND at_ms IS NULL)))
                ),
                CHECK (max_delay_seconds IS NULL OR max_delay_seconds >= 0),
                CHECK (fired_at IS NULL OR (kind = 'at' AND dispatch_owner = 'internal')),
                CHECK (retry_attempt = 0 OR (kind = 'at' AND dispatch_owner = 'internal')),

                FOREIGN KEY (task_id) REFERENCES task(task_id) ON DELETE CASCADE
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_cron_task_id ON cron(task_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_cron_dispatch_owner "
            "ON cron(dispatch_owner)"
        )
        logger.info("cron table created successfully")

    def _create_task_table(self, conn: sqlite3.Connection) -> None:
        """Create task table — task metadata SSOT (description / status)."""
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task (
                task_id     TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'active',
                paused_at   INTEGER,
                created_at  INTEGER NOT NULL
            )
        """)
        logger.info("task table created successfully")

    def _create_rule_log_table(self, conn: sqlite3.Connection) -> None:
        """Create rule_log table for rule execution logs (V3 schema).

        See rule-design.md §4.2 for column semantics.
        """
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rule_log (
                id TEXT PRIMARY KEY,
                timestamp INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'RULE_TRIGGER_SUCCESS',
                rule_id TEXT NOT NULL,
                rule_name TEXT NOT NULL,
                rule_query TEXT NOT NULL,
                trigger_context TEXT NOT NULL DEFAULT '',
                execute_result TEXT,
                created_at INTEGER NOT NULL
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_rule_log_timestamp ON rule_log(timestamp)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_rule_log_rule_id ON rule_log(rule_id)"
        )
        logger.info("Rule log table created successfully")

    def _create_device_lru_table(self, conn: sqlite3.Connection) -> None:
        """Create device_lru table for per-device type_name LRU history."""
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS device_lru (
                did TEXT NOT NULL,
                key TEXT NOT NULL,
                touched_at INTEGER NOT NULL,
                PRIMARY KEY (did, key)
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_device_lru_did_touched "
            "ON device_lru(did, touched_at DESC)"
        )
        logger.info("device_lru table created successfully")

    def _create_token_usage_table(self, conn: sqlite3.Connection) -> None:
        """Create token_usage table for per-API-call token usage events (last 3 days).

        Field semantics:
          input_tokens  = prompt_tokens (total input, all modalities)
          cache_tokens  = cached portion (⊆ input)
          video_tokens  = video portion  (⊆ input)
          audio_tokens  = audio portion  (⊆ input)
          output_tokens = completion_tokens
        """
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                model TEXT NOT NULL,
                type TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_tokens INTEGER NOT NULL DEFAULT 0,
                video_tokens INTEGER NOT NULL DEFAULT 0,
                audio_tokens INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_usage_timestamp ON token_usage(timestamp)"
        )
        logger.info("token_usage table created successfully")

    def _create_token_usage_daily_table(self, conn: sqlite3.Connection) -> None:
        """Create token_usage_daily table holding per-day rollup of older events.

        Rows are keyed by (date, model, type) so historical trend / model / type
        breakdown all stay queryable after the live table is pruned.
        Field semantics identical to token_usage (modality columns ⊆ input_tokens).
        """
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS token_usage_daily (
                date TEXT NOT NULL,
                model TEXT NOT NULL,
                type TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_tokens INTEGER NOT NULL DEFAULT 0,
                video_tokens INTEGER NOT NULL DEFAULT 0,
                audio_tokens INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (date, model, type)
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_usage_daily_date ON token_usage_daily(date)"
        )
        logger.info("token_usage_daily table created successfully")

    def _create_task_record_progress_table(self, conn: sqlite3.Connection) -> None:
        """Create task_record_progress table — progress kind 主表。

        同一 task 在 rollover 之后会有多行（活跃 + N 条归档历史快照）。
        `uniq_progress_active` partial unique index 保证任意时刻只有一行
        archived_at IS NULL（活跃行）。FK 挂 task(task_id) ON DELETE CASCADE，
        terminate 时一笔级联清。
        """
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_record_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                target INTEGER NOT NULL,
                current INTEGER NOT NULL DEFAULT 0,
                unit TEXT NOT NULL,
                window TEXT NOT NULL,
                recurring_pattern TEXT,
                expires_at INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                archived_at INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (task_id) REFERENCES task(task_id) ON DELETE CASCADE
            )
        """)
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uniq_progress_active "
            "ON task_record_progress(task_id) WHERE archived_at IS NULL"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_record_progress_archived "
            "ON task_record_progress(task_id, archived_at) "
            "WHERE archived_at IS NOT NULL"
        )
        logger.info("task_record_progress table created successfully")

    def _create_task_record_duration_table(self, conn: sqlite3.Connection) -> None:
        """Create task_record_duration table — duration kind 主表。

        active_session_start_at NULL = 无活跃 session；非 NULL 表示有正在进行
        的 session（用户开始看电视但还没结束）。rollover 会把活跃行打 archived
        并 INSERT 新活跃行。
        """
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_record_duration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                target_minutes INTEGER,
                active_session_start_at INTEGER,
                recurring_pattern TEXT,
                expires_at INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                archived_at INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (task_id) REFERENCES task(task_id) ON DELETE CASCADE
            )
        """)
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uniq_duration_active "
            "ON task_record_duration(task_id) WHERE archived_at IS NULL"
        )
        logger.info("task_record_duration table created successfully")

    def _create_task_record_duration_session_table(
        self, conn: sqlite3.Connection
    ) -> None:
        """Create task_record_duration_session table — duration 子表。

        子表 FK 直接挂 task(task_id) 而非主表 id——terminate 时 CASCADE 一笔
        清完，避免业务层维护"主表 id → 子表 record_id"映射。session-end 时
        INSERT 一行；session-start 不写子表，只更新主表 active_session_start_at。
        """
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_record_duration_session (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                start_at INTEGER NOT NULL,
                end_at INTEGER NOT NULL,
                duration_seconds INTEGER NOT NULL,
                archived_at INTEGER,
                FOREIGN KEY (task_id) REFERENCES task(task_id) ON DELETE CASCADE
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_record_duration_session_task "
            "ON task_record_duration_session(task_id, archived_at)"
        )
        logger.info("task_record_duration_session table created successfully")

    def _create_task_record_event_table(self, conn: sqlite3.Connection) -> None:
        """Create task_record_event table — event kind 主表（longterm 单行）。

        event 是 longterm 设计——一个 task 只有一行（普通 UNIQUE，不需要
        partial index），不参与 rollover，无 archived_at 字段；terminate 时
        FK CASCADE 物理删。recurring_pattern 列保留供 web 面板展示与未来
        扩展（当前 backend 不读）。
        """
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_record_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL UNIQUE,
                recurring_pattern TEXT,
                expires_at INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (task_id) REFERENCES task(task_id) ON DELETE CASCADE
            )
        """)
        logger.info("task_record_event table created successfully")

    def _create_task_record_event_entry_table(
        self, conn: sqlite3.Connection
    ) -> None:
        """Create task_record_event_entry table — event 子表。

        event_append 每次 INSERT 一行（O(log n)，杜绝文件方案的 list 读写退化）。
        description 是单条事件的描述（如"喝了一杯水"），跟 task.description
        语义独立。FK CASCADE 跟主表对齐。
        """
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_record_event_entry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                description TEXT NOT NULL,
                at INTEGER NOT NULL,
                FOREIGN KEY (task_id) REFERENCES task(task_id) ON DELETE CASCADE
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_record_event_entry_task_at "
            "ON task_record_event_entry(task_id, at)"
        )
        logger.info("task_record_event_entry table created successfully")

    def _create_task_terminate_log_table(self, conn: sqlite3.Connection) -> None:
        """Create task_terminate_log table — terminate 审计快照（30 天滚动）。

        task_id 列**不加 FK**——task 即将在同事务被删，挂 FK 反而阻塞 DELETE。
        每条 terminate 写一行（含 kind / reason / description / final_snapshot），
        30 天滚动清理（terminate 事务里同步 DELETE 过期行）。
        """
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_terminate_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                reason TEXT NOT NULL,
                description TEXT NOT NULL,
                final_snapshot TEXT NOT NULL,
                terminated_at INTEGER NOT NULL
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_terminate_log_at "
            "ON task_terminate_log(terminated_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_terminate_log_reason_at "
            "ON task_terminate_log(reason, terminated_at)"
        )
        logger.info("task_terminate_log table created successfully")

    @contextmanager
    def get_connection(self):
        """Get database connection context manager.

        只设 connection-level PRAGMA。db-level (auto_vacuum / journal_mode)
        在 _create_tables 的 fresh-build 路径一次性写入;每次连接重设需要
        write lock,会跟其他 writer 撞。
        wal_autocheckpoint 默认就是 1000,无需显式 set。
        busy_timeout 由 sqlite3.connect(timeout=self.timeout) 设置。
        """
        conn = None
        try:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=self.timeout,
                check_same_thread=self.check_same_thread,
                isolation_level=self.isolation_level,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error("Database connection error: %s", e)
            raise
        finally:
            if conn:
                conn.close()

    def execute_query(
        self, query: str, params: tuple | None = None
    ) -> list[dict[str, Any]]:
        """Execute query statement and return results"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)

                # Convert Row objects to dictionaries
                rows = cursor.fetchall()
                return [dict(row) for row in rows]

        except Exception as e:
            logger.error("Query execution failed: %s, SQL: %s", e, query)
            raise

    def execute_update(self, query: str, params: tuple | None = None) -> int:
        """Execute update statement and return number of affected rows"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                conn.commit()
                return cursor.rowcount

        except Exception as e:
            logger.error("Update execution failed: %s, SQL: %s", e, query)
            raise

    def execute_many(self, query: str, params_list: list[tuple]) -> int:
        """Batch execute statements"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.executemany(query, params_list)
                conn.commit()
                return cursor.rowcount

        except Exception as e:
            logger.error("Batch execution failed: %s, SQL: %s", e, query)
            raise

    def get_database_info(self) -> dict[str, Any]:
        """Get database information"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                # Get database size
                db_size = self.db_path.stat().st_size if self.db_path.exists() else 0

                # Get table information
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [row[0] for row in cursor.fetchall()]

                # Get database version
                cursor.execute("PRAGMA user_version")
                version = cursor.fetchone()[0]

                return {
                    "path": str(self.db_path),
                    "size": db_size,
                    "tables": tables,
                    "version": version,
                    "exists": self.db_path.exists(),
                }

        except Exception as e:
            logger.error("Failed to get database info: %s", e)
            return {}


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2 schema step.

    一次事务原子完成:
      (a0) 预扫 cron dangling (task_link.cron 指向已删 task) → 全字段 log + 跳过
           理由: task 已不存在, 自动化上下文无, 迁移不该为此卡启动;
           openclaw 侧残留 cron 由 openclaw 自行清理, 与 backend 无关。
      (a)  搬 task_link cron 行 → cron 表 external 分区 (跳过 dangling)
      (b1) 预扫 D 型 rule 归属冲突 (rule.task_id != task_link.task_id) → 全字段 log,
           一律取 task_link 侧 (迁移前 UI 从 task_link 读归属, 保持零意外)
      (b2) 预扫 A/E 型 orphan rule 完整字段 log (自动删除, log 便于事后审计)
      (c)  建 rule_new (task_id NOT NULL + FK CASCADE) + INSERT SELECT
           (COALESCE(tl.task_id, r.task_id) 优先 task_link 侧,
            A/E 型 EXISTS/COALESCE 双滤 = 等价 DELETE)
      (d)  DROP rule → RENAME rule_new → 重建 indices
      (e)  DROP task_link (dangling 行已在 (a) 单独删)
      (f)  三重不变量断言 (foreign_key_check rule/cron 空 + rule.task_id 无 NULL)
      (g)  PRAGMA user_version = 2 (同事务)
      COMMIT

    crash 语义: 单事务原子, COMMIT 前 crash → rollback 到 v1, 重启从头再跑;
    COMMIT 后 crash → user_version=2, 外层步进循环跳过, 不重入。

    fail-soft 哲学: 数据冲突永不阻塞启动, 按确定性规则就地处置 + 全字段 log。
    A/E 删 / dangling 跳 / D 取 tl 侧均不可逆, 复原靠 log + 冷备份 restore。
    """
    now = int(time.time() * 1000)
    counts: dict[str, int] = {
        "cron_planned": 0,
        "cron_migrated": 0,
        "cron_skipped_existing": 0,
        "cron_dangling_skipped": 0,
        "rule_backfilled": 0,
        "rule_orphan_deleted": 0,
        "rule_d_conflict_took_link": 0,
        "task_link_rule_deleted": 0,
    }
    cursor = conn.cursor()

    # SQLite 语法约束: PRAGMA foreign_keys 只能事务外切换
    # OFF 期间 table-rebuild 可打破 FK; COMMIT 后再 ON 让运行时生效
    cursor.execute("PRAGMA foreign_keys=OFF")

    cursor.execute("BEGIN IMMEDIATE")
    try:
        # ── (a0) cron dangling 全字段 log + 跳过 ────────────────────
        cron_dangling = cursor.execute("""
            SELECT tl.*
              FROM task_link tl
             WHERE tl.link_kind='cron'
               AND NOT EXISTS(
                     SELECT 1 FROM task t WHERE t.task_id = tl.task_id
                   )
        """).fetchall()
        counts["cron_dangling_skipped"] = len(cron_dangling)
        if cron_dangling:
            logger.warning(
                "v1→v2 skipping %d dangling cron task_link row(s); "
                "full content follows",
                len(cron_dangling),
            )
            for row in cron_dangling:
                logger.warning("v1→v2 skipping dangling cron: %s", dict(row))
            # 单独删掉 dangling task_link 行, 后续 (a) 主循环拿到的都是 valid
            cursor.execute("""
                DELETE FROM task_link
                 WHERE link_kind='cron'
                   AND NOT EXISTS(
                         SELECT 1 FROM task t WHERE t.task_id = task_link.task_id
                       )
            """)

        # ── (a) task_link cron 行 → cron 表 external 分区 ──────────
        cron_rows = cursor.execute(
            "SELECT task_id, link_ref FROM task_link WHERE link_kind='cron'"
        ).fetchall()
        counts["cron_planned"] = len(cron_rows)

        for row in cron_rows:
            cron_id = row["link_ref"]
            task_id = row["task_id"]

            existing = cursor.execute(
                "SELECT task_id, dispatch_owner FROM cron WHERE cron_id=?",
                (cron_id,),
            ).fetchone()

            if existing is not None:
                # user_version=2 后本函数永久跳过, 理论不该有已存在的 cron_id;
                # defensive: 若命中且校验 external + task_id 一致 → 安全 skip;
                # 冲突时 log 后仍以 cron 表现值为准, 不阻塞启动
                if (
                    existing["dispatch_owner"] != "external"
                    or existing["task_id"] != task_id
                ):
                    logger.warning(
                        "v1→v2 cron pre-existing conflict at cron_id=%s: "
                        "existing (task_id=%s, dispatch_owner=%s) "
                        "vs task_link (task_id=%s); keeping cron table value",
                        cron_id,
                        existing["task_id"],
                        existing["dispatch_owner"],
                        task_id,
                    )
                counts["cron_skipped_existing"] += 1
            else:
                cursor.execute(
                    "INSERT INTO cron (cron_id, task_id, dispatch_owner, "
                    "enabled, created_at, updated_at) "
                    "VALUES (?, ?, 'external', 1, ?, ?)",
                    (cron_id, task_id, now, now),
                )
                counts["cron_migrated"] += 1

            cursor.execute(
                "DELETE FROM task_link WHERE link_kind='cron' AND link_ref=?",
                (cron_id,),
            )

        # ── (b1) D 型 rule 归属冲突全字段 log, 取 task_link 侧 ─────
        # 只统计「link 侧 task 真实存在」的 D 型: 与 (c) 处置口径一致。
        # link 侧 task 也已删的行由 (b2) 作 orphan 记录并 drop, 不重复计入。
        d_conflicts = cursor.execute("""
            SELECT r.*, tl.task_id AS link_task_id
              FROM rule r
              JOIN task_link tl
                ON tl.link_kind='rule' AND tl.link_ref=r.id
             WHERE r.task_id IS NOT NULL
               AND tl.task_id IS NOT NULL
               AND r.task_id != tl.task_id
               AND EXISTS(SELECT 1 FROM task t WHERE t.task_id = tl.task_id)
        """).fetchall()
        counts["rule_d_conflict_took_link"] = len(d_conflicts)
        if d_conflicts:
            logger.warning(
                "v1→v2 D-type rule ownership conflict: %d row(s) taking "
                "task_link side; full content follows",
                len(d_conflicts),
            )
            for row in d_conflicts:
                logger.warning("v1→v2 D-type conflict took link: %s", dict(row))

        # ── (b2) A/E 型 orphan rule 预扫 + 完整字段 log ────────────
        # A 型: r.task_id IS NULL 且 task_link 无关联
        # E 型: COALESCE(tl.task_id, r.task_id) 指向已不存在的 task
        # 每条 rule 一行 log (dict(row) 拿全部业务字段), 便于用户上报时手工复原
        orphan_rows = cursor.execute("""
            SELECT r.*, tl.task_id AS link_task_id
              FROM rule r
              LEFT JOIN task_link tl
                ON tl.link_kind='rule' AND tl.link_ref=r.id
             WHERE COALESCE(tl.task_id, r.task_id) IS NULL
                OR NOT EXISTS(
                     SELECT 1 FROM task t
                      WHERE t.task_id = COALESCE(tl.task_id, r.task_id)
                   )
        """).fetchall()
        counts["rule_orphan_deleted"] = len(orphan_rows)
        if orphan_rows:
            logger.warning(
                "v1→v2 will drop %d orphan rule(s); full content follows",
                len(orphan_rows),
            )
            for row in orphan_rows:
                logger.warning("v1→v2 dropping orphan rule: %s", dict(row))

        # ── (c) 建 rule_new + INSERT SELECT ────────────────────────
        # 迁移前 UI 从 task_link 读归属, effective task_id 优先 task_link 侧:
        #   A 型 (r.task_id NULL & tl 无): COALESCE=NULL → WHERE 排除
        #   B 型 (r.task_id NULL & tl 有): COALESCE=tl.task_id → 回填 INSERT
        #   C 型 (r.task_id NOT NULL & tl 无): COALESCE=r.task_id → 原样 INSERT
        #   D 型 (两者都有且冲突): COALESCE=tl.task_id → 取 task_link 侧, 与
        #     迁移前 UI 可见归属一致 (b1 已 log 全字段)
        #   E 型 (COALESCE 指向已删 task): EXISTS 失败 → WHERE 排除
        cursor.execute("""
            CREATE TABLE rule_new (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                task_id TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'event',
                lifecycle TEXT NOT NULL DEFAULT 'permanent',
                enabled BOOLEAN DEFAULT 1,
                condition TEXT NOT NULL,
                actions TEXT NOT NULL DEFAULT '[]',
                action_descriptions TEXT NOT NULL DEFAULT '[]',
                on_enter_actions TEXT NOT NULL DEFAULT '[]',
                on_enter_desc TEXT,
                on_exit_actions TEXT NOT NULL DEFAULT '[]',
                on_exit_desc TEXT,
                on_target_desc TEXT,
                terminate_when TEXT,
                exit_debounce_seconds INTEGER NOT NULL DEFAULT 60,
                duration_seconds INTEGER,
                duration_ratio REAL NOT NULL DEFAULT 0.8,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (task_id) REFERENCES task(task_id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            INSERT INTO rule_new
                SELECT r.id, r.name,
                       COALESCE(tl.task_id, r.task_id) AS task_id,
                       r.mode, r.lifecycle, r.enabled, r.condition,
                       r.actions, r.action_descriptions,
                       r.on_enter_actions, r.on_enter_desc,
                       r.on_exit_actions, r.on_exit_desc,
                       r.on_target_desc, r.terminate_when,
                       r.exit_debounce_seconds, r.duration_seconds, r.duration_ratio,
                       r.created_at, r.updated_at
                FROM rule r
                LEFT JOIN task_link tl
                  ON tl.link_kind='rule' AND tl.link_ref=r.id
                WHERE COALESCE(tl.task_id, r.task_id) IS NOT NULL
                  AND EXISTS(
                        SELECT 1 FROM task t
                         WHERE t.task_id = COALESCE(tl.task_id, r.task_id)
                      )
        """)

        counts["rule_backfilled"] = cursor.execute("""
            SELECT COUNT(*) FROM rule_new rn
              JOIN rule r ON r.id=rn.id
             WHERE r.task_id IS NULL
        """).fetchone()[0]

        # ── (d) DROP + RENAME + 重建 indices ────────────────────────
        cursor.execute("DROP TABLE rule")
        cursor.execute("ALTER TABLE rule_new RENAME TO rule")
        cursor.execute("CREATE INDEX idx_rule_name ON rule(name)")
        cursor.execute("CREATE INDEX idx_rule_task_id ON rule(task_id)")
        cursor.execute("CREATE INDEX idx_rule_enabled ON rule(enabled)")

        # ── (e) DROP task_link ──────────────────────────────────────
        rule_row_count = cursor.execute(
            "SELECT COUNT(*) FROM task_link WHERE link_kind='rule'"
        ).fetchone()[0]
        counts["task_link_rule_deleted"] = rule_row_count
        cursor.execute("DROP TABLE task_link")

        # ── (f) 三重不变量断言 ──────────────────────────────────────
        rule_violations = cursor.execute("PRAGMA foreign_key_check(rule)").fetchall()
        if rule_violations:
            raise RuntimeError(
                f"v1→v2 migration invariant broken: {len(rule_violations)} "
                f"rule FK violation(s) remain after A/E deletion; "
                f"details={rule_violations}"
            )
        cron_violations = cursor.execute("PRAGMA foreign_key_check(cron)").fetchall()
        if cron_violations:
            raise RuntimeError(
                f"v1→v2 migration invariant broken: {len(cron_violations)} "
                f"cron FK violation(s) remain (should have been caught by "
                f"(a0) pre-scan); details={cron_violations}"
            )
        null_count = cursor.execute(
            "SELECT COUNT(*) FROM rule WHERE task_id IS NULL"
        ).fetchone()[0]
        if null_count > 0:
            raise RuntimeError(
                f"v1→v2 migration invariant broken: {null_count} rule(s) "
                f"with NULL task_id remain after A/E deletion"
            )

        # ── (g) PRAGMA user_version 与业务 DML 同事务 ─────────────
        cursor.execute("PRAGMA user_version = 2")
        conn.commit()
        logger.info("v1→v2 migration done: %s", counts)
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.execute("PRAGMA foreign_keys=ON")


# schema 步进迁移登记表; 未来加 v3 时新增 {3: _migrate_v2_to_v3} 条目
_SCHEMA_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_v1_to_v2,
}


def rollback_v2_to_v1() -> dict[str, int]:
    """v2 → v1 反向迁移 (人工触发, 不在正常启动路径).

    与正向 _migrate_v1_to_v2 对称, 单事务原子回退所有 v2 引入的 schema 变化:
    重建 task_link + rule 反向 table-rebuild + cron external 反写 + DROP cron。

    **不可逆的部分**: A/E 型 orphan rule 在正向迁移已被 DELETE, log 里有完整
    字段。如需恢复必须从 log 手工提取 SQL 重建, 本函数不管。

    **前置条件**: internal cron 必须已被 caller 手工清空 (v1 无 cron 表, rollback
    语义 = 彻底回到迁移前状态; internal 是 backend 建的用户数据, 不能盲目丢弃)。
    函数内断言 internal_count == 0, 否则 raise。
    """
    stats: dict[str, int] = {
        "rule_reverted_to_link": 0,
        "cron_reverted_to_link": 0,
        "cron_skipped_existing": 0,
        "conflicts": 0,
    }
    with get_db_connector().get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN IMMEDIATE")
        try:
            internal_count = cursor.execute(
                "SELECT COUNT(*) FROM cron WHERE dispatch_owner='internal'"
            ).fetchone()[0]
            if internal_count > 0:
                raise RuntimeError(
                    f"rollback_v2_to_v1 refused: {internal_count} internal "
                    f"cron row(s) remain (v1 schema has no cron table). Clear "
                    f"internal cron via user re-provisioning or manual export "
                    f"before rollback."
                )

            # 重建 task_link 表 (v1 形态)
            cursor.execute("""
                CREATE TABLE task_link (
                    task_id TEXT NOT NULL,
                    link_kind TEXT NOT NULL,
                    link_ref TEXT NOT NULL,
                    PRIMARY KEY (task_id, link_kind, link_ref),
                    FOREIGN KEY (task_id) REFERENCES task(task_id) ON DELETE CASCADE
                )
            """)
            cursor.execute(
                "CREATE UNIQUE INDEX idx_task_link_ref_unique "
                "ON task_link(link_kind, link_ref)"
            )

            # rule 关联反写 task_link (v2 后 rule.task_id 均非 NULL, 全部反写)
            cursor.execute(
                "INSERT INTO task_link (task_id, link_kind, link_ref) "
                "SELECT task_id, 'rule', id FROM rule"
            )
            stats["rule_reverted_to_link"] = cursor.rowcount

            # rule 表反向 table-rebuild: 恢复 v1 形态 (无 FK, task_id NULLABLE)
            cursor.execute("""
                CREATE TABLE rule_v1 (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    task_id TEXT,
                    mode TEXT NOT NULL DEFAULT 'event',
                    lifecycle TEXT NOT NULL DEFAULT 'permanent',
                    enabled BOOLEAN DEFAULT 1,
                    condition TEXT NOT NULL,
                    actions TEXT NOT NULL DEFAULT '[]',
                    action_descriptions TEXT NOT NULL DEFAULT '[]',
                    on_enter_actions TEXT NOT NULL DEFAULT '[]',
                    on_enter_desc TEXT,
                    on_exit_actions TEXT NOT NULL DEFAULT '[]',
                    on_exit_desc TEXT,
                    on_target_desc TEXT,
                    terminate_when TEXT,
                    exit_debounce_seconds INTEGER NOT NULL DEFAULT 60,
                    duration_seconds INTEGER,
                    duration_ratio REAL NOT NULL DEFAULT 0.8,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            cursor.execute("INSERT INTO rule_v1 SELECT * FROM rule")
            cursor.execute("DROP TABLE rule")
            cursor.execute("ALTER TABLE rule_v1 RENAME TO rule")
            cursor.execute("CREATE INDEX idx_rule_name ON rule(name)")
            cursor.execute("CREATE INDEX idx_rule_task_id ON rule(task_id)")
            cursor.execute("CREATE INDEX idx_rule_enabled ON rule(enabled)")

            # cron external 行反写 task_link
            cron_rows = cursor.execute(
                "SELECT cron_id, task_id FROM cron WHERE dispatch_owner='external'"
            ).fetchall()
            for row in cron_rows:
                cron_id = row["cron_id"]
                task_id = row["task_id"]

                existing = cursor.execute(
                    "SELECT task_id FROM task_link "
                    "WHERE link_kind='cron' AND link_ref=?",
                    (cron_id,),
                ).fetchone()

                if existing is not None:
                    if existing["task_id"] != task_id:
                        stats["conflicts"] += 1
                        raise RuntimeError(
                            f"rollback conflict at cron_id={cron_id}: "
                            f"existing task_link.task_id={existing['task_id']} "
                            f"vs cron.task_id={task_id}. Human review required."
                        )
                    stats["cron_skipped_existing"] += 1
                else:
                    cursor.execute(
                        "INSERT INTO task_link (task_id, link_kind, link_ref) "
                        "VALUES (?, 'cron', ?)",
                        (task_id, cron_id),
                    )
                    stats["cron_reverted_to_link"] += 1

            cursor.execute("DROP TABLE cron")
            cursor.execute("PRAGMA user_version = 1")
            conn.commit()
            logger.info("v2→v1 rollback done: %s", stats)
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.execute("PRAGMA foreign_keys=ON")
    return stats


# Global database connector instance
db_connector = None


def init_database() -> None:
    """Convenience function to initialize database"""
    get_db_connector().initialize_database()


def get_db_connector() -> SQLiteConnector:
    """Get database connector instance"""
    global db_connector
    if db_connector is None:
        db_connector = SQLiteConnector()
    return db_connector
