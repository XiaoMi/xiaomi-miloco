# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
SQLite database connector
Responsible for database initialization, connection management and basic operations
"""

import logging
import sqlite3
from contextlib import contextmanager
from typing import Any

from miloco.config import get_settings

# Configure logging
logger = logging.getLogger(__name__)

# 发布版 v1 新基线。后续版本升级走 PRAGMA user_version 步进迁移。
_DB_SCHEMA_VERSION = 1


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
                        # WARNING 而非 INFO:老部署突然读到一个"文件在但零表"的空 db,几乎必是
                        # home/db 路径配错(读错了文件),而非正常首启——一旦静默 fresh init,
                        # person 表建空,已注册成员从 roster 消失、omni 渲染"暂无已注册成员"。
                        logger.warning(
                            "Empty pre-created db file, running fresh init: %s "
                            "(若本应有历史数据，请核实 MILOCO_HOME / database 路径是否漂移)",
                            self.db_path,
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

                    if "task_link" not in existing_tables:
                        logger.info("task_link table not found, creating...")
                        self._create_task_link_table(conn)
                        tables_created.append("task_link")

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

                    # 走到兜底分支的老库可能 user_version=0(无版本号),
                    # 钉到当前基线;已有版本号的库(未来 1.x 升级路径)不动。
                    current_version = cursor.execute(
                        "PRAGMA user_version"
                    ).fetchone()[0]
                    if current_version == 0:
                        conn.execute(
                            f"PRAGMA user_version = {_DB_SCHEMA_VERSION}"
                        )

                    # If new tables were created, commit transaction
                    if tables_created:
                        conn.commit()
                        logger.info("Created missing tables: %s", tables_created)
                    else:
                        conn.commit()
                        logger.info("All required tables already exist")

                    logger.info("Database loaded successfully: %s", self.db_path)
            else:
                # WARNING 而非 INFO:除首次装机,产品运行中突然新建 db 基本等于"读错了 home/
                # 路径"——新库 person 表为空,已注册成员从 roster 消失、omni 渲染"暂无已注册成员"。
                logger.warning(
                    "Database file does not exist, creating new database: %s "
                    "(非首次装机时请核实 MILOCO_HOME / database 路径是否漂移)",
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
        self._create_rule_table(conn)
        self._create_rule_log_table(conn)
        self._create_task_table(conn)
        self._create_task_link_table(conn)
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

        每次推理 = 一行 event(同窗口 N 摄像头合并 1 行;device_ids JSON 记录本行真正相关的
        摄像头——有 rule/suggestion/asr 命中时收窄到其 source_device_ids,否则退化为本次
        推理中成功出图(可落盘)的全部摄像头).
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
        """Create rule table for the rule system (V3 schema).

        See rule-design.md §4.1 for column semantics.

        actions / on_enter_actions / on_exit_actions store JSON lists of
        RuleAction objects ({did, iid, value/params, idempotent,
        cooldown_minutes}).
        """
        cursor = conn.cursor()
        # duration_ratio DEFAULT 0.8 是历史值,与应用层 settings 0.6 故意不同步;
        # 详见 rule_repo._DURATION_RATIO_DB_FALLBACK 注释。
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rule (
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
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rule_name ON rule(name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rule_task_id ON rule(task_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rule_enabled ON rule(enabled)")
        logger.info("Rule table created successfully")

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

    def _create_task_link_table(self, conn: sqlite3.Connection) -> None:
        """Create task_link table — task ↔ rule/cron/memory junction.

        复合主键 (task_id, link_kind, link_ref)；task_id FK→task.task_id
        ON DELETE CASCADE，删 task 时自动清 task_link 行。全局
        UNIQUE(link_kind, link_ref) 强制跨 task ref 唯一（同一 rule_id /
        cron jobId / memory ref 不能挂在两个 task 上）。
        """
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_link (
                task_id   TEXT NOT NULL,
                link_kind TEXT NOT NULL,
                link_ref  TEXT NOT NULL,
                PRIMARY KEY (task_id, link_kind, link_ref),
                FOREIGN KEY (task_id) REFERENCES task(task_id) ON DELETE CASCADE
            )
        """)
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_task_link_ref_unique "
            "ON task_link(link_kind, link_ref)"
        )
        logger.info("task_link table created successfully")

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
