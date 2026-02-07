# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
SQLite database connector
Responsible for database initialization, connection management and basic operations
"""

import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

from miloco_server.config import DATABASE_CONFIG

# Configure logging
logger = logging.getLogger(__name__)


class SQLiteConnector:
    """SQLite database connector class"""

    def __init__(self):
        """Initialize database connector"""
        self.db_path = DATABASE_CONFIG["path"]
        self.timeout = DATABASE_CONFIG["timeout"]
        self.check_same_thread = DATABASE_CONFIG["check_same_thread"]
        self.isolation_level = DATABASE_CONFIG["isolation_level"]
        self._connection: Optional[sqlite3.Connection] = None

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
                    cursor.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")
                    existing_tables = {row[0] for row in cursor.fetchall()}
                    logger.info(
                        "Found %d tables in existing database: %s",
                        len(existing_tables), existing_tables
                    )

                    # Check if necessary tables exist, create if not
                    tables_created = []

                    if "kv" not in existing_tables:
                        logger.info("KV table not found, creating...")
                        self._create_kv_table(conn)
                        tables_created.append("kv")

                    if "trigger_rule" not in existing_tables:
                        logger.info(
                            "Trigger rule table not found, creating...")
                        self._create_trigger_rule_table(conn)
                        tables_created.append("trigger_rule")

                    if "trigger_rule_log" not in existing_tables:
                        logger.info(
                            "Trigger rule log table not found, creating...")
                        self._create_trigger_rule_log_table(conn)
                        tables_created.append("trigger_rule_log")

                    if "model_vendor" not in existing_tables:
                        logger.info(
                            "Model vendor table not found, creating...")
                        self._create_model_vendor_table(conn)
                        tables_created.append("model_vendor")

                    if "chat_history" not in existing_tables:
                        logger.info(
                            "Chat history table not found, creating...")
                        self._create_chat_history_table(conn)
                        tables_created.append("chat_history")

                    if "mcp_config" not in existing_tables:
                        logger.info("MCP config table not found, creating...")
                        self._create_mcp_config_table(conn)
                        tables_created.append("mcp_config")

                    if "rtsp_camera" not in existing_tables:
                        logger.info(
                            "RTSP camera table not found, creating...")
                        self._create_rtsp_camera_table(conn)
                        tables_created.append("rtsp_camera")

                    # If new tables were created, commit transaction
                    if tables_created:
                        conn.commit()
                        logger.info(
                            "Created missing tables: %s", tables_created)
                    else:
                        logger.info("All required tables already exist")

                    logger.info(
                        "Database loaded successfully: %s", self.db_path)
            else:
                logger.info(
                    "Database file does not exist, creating new database: %s",
                    self.db_path
                )
                # Create database connection and initialize table structure
                with self.get_connection() as conn:
                    self._create_tables(conn)
                    logger.info(
                        "Database initialized successfully: %s", self.db_path)

        except Exception as e:
            logger.error("Database initialization failed: %s", e)
            raise

    def _create_tables(self, conn: sqlite3.Connection) -> None:
        """Create database table structure"""
        self._create_kv_table(conn)
        self._create_trigger_rule_table(conn)
        self._create_trigger_rule_log_table(conn)
        self._create_model_vendor_table(conn)
        self._create_mcp_config_table(conn)
        self._create_chat_history_table(conn)
        self._create_rtsp_camera_table(conn)
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create index to improve query performance
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kv_key ON kv(key)")
        logger.info("KV table created successfully")

    def _create_trigger_rule_table(self, conn: sqlite3.Connection) -> None:
        """Create trigger rule table"""
        cursor = conn.cursor()

        # Create trigger rule table for storing trigger rules
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trigger_rule (
                id TEXT PRIMARY KEY,  -- Use UUID as primary key, no longer auto-increment
                name TEXT NOT NULL,
                enabled BOOLEAN DEFAULT 1,
                camera_dids TEXT NOT NULL,  -- JSON format storage for camera device ID list
                condition TEXT NOT NULL,    -- Trigger condition
                execute_info TEXT,          -- JSON format storage for ExecuteInfo object
                filter TEXT,                 -- JSON format storage for TriggerFilter object
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create indexes for trigger rule table to improve query performance
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_trigger_rule_name ON trigger_rule(name)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_trigger_rule_enabled ON trigger_rule(enabled)"
        )

        logger.info("Trigger rule table created successfully")

    def _create_model_vendor_table(self, conn: sqlite3.Connection) -> None:
        """Create model vendor table"""
        cursor = conn.cursor()

        # Create model vendor table for storing model access point information
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS model_vendor (
                id TEXT PRIMARY KEY,  -- Use UUID as primary key
                base_url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                model_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create indexes for model vendor table to improve query performance
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_vendor_model_name ON model_vendor(model_name)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_vendor_base_url ON model_vendor(base_url)"
        )
        logger.info("Model vendor table created successfully")

    def _create_mcp_config_table(self, conn: sqlite3.Connection) -> None:
        """Create MCP config table"""
        cursor = conn.cursor()

        # Create MCP config table for storing MCP service configuration information
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mcp_config (
                id TEXT PRIMARY KEY,  -- Use UUID as primary key
                access_type TEXT NOT NULL,  -- Access type: http_sse, streamable_http, stdio
                name TEXT NOT NULL,  -- Service name
                description TEXT DEFAULT '',  -- Service description
                provider TEXT DEFAULT '',  -- Provider
                provider_website TEXT DEFAULT '',  -- Provider website
                timeout INTEGER DEFAULT 60,  -- Timeout setting (seconds)
                enable BOOLEAN DEFAULT 1,  -- Enable status: 1=enabled, 0=disabled
                url TEXT,  -- Service URL (used by HTTP/SSE and Streamable HTTP)
                request_header_token TEXT,  -- Request header Token (used by HTTP/SSE and Streamable HTTP)
                command TEXT,  -- Command (used by Stdio)
                args TEXT,  -- Parameter list JSON format (used by Stdio)
                env_vars TEXT,  -- Environment variables JSON format (used by Stdio)
                working_directory TEXT,  -- Working directory (used by Stdio)
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create indexes for MCP config table to improve query performance
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_mcp_config_name ON mcp_config(name)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_mcp_config_access_type ON mcp_config(access_type)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_mcp_config_provider ON mcp_config(provider)"
        )
        logger.info("MCP config table created successfully")

    def _create_chat_history_table(self, conn: sqlite3.Connection) -> None:
        """Create chat history table"""
        cursor = conn.cursor()

        # Create chat history table for storing conversation history
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                session_id TEXT PRIMARY KEY,  -- Use UUID as primary key
                title TEXT NOT NULL,  -- Conversation title
                timestamp INTEGER NOT NULL,  -- Timestamp
                messages TEXT,  -- JSON format storage for message content
                session TEXT,  -- JSON format storage for session content
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create indexes for chat history table to improve query performance
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_history_title ON chat_history(title)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_history_created_at ON chat_history(created_at)"
        )
        logger.info("Chat history table created successfully")

    def _create_trigger_rule_log_table(self, conn: sqlite3.Connection) -> None:
        """Create trigger rule log table"""
        cursor = conn.cursor()

        # Create trigger rule log table for storing trigger rule execution logs
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trigger_rule_log (
                id TEXT PRIMARY KEY,  -- Use UUID as primary key
                timestamp INTEGER NOT NULL,  -- Trigger time (millisecond Unix timestamp)
                trigger_rule_id TEXT NOT NULL,  -- Trigger rule ID
                trigger_rule_name TEXT NOT NULL,  -- Trigger rule name
                trigger_rule_condition TEXT NOT NULL,  -- Trigger rule condition
                camera_condition_results TEXT NOT NULL,  -- JSON format storage for camera trigger condition result list
                execute_result TEXT,  -- JSON format storage for ExecuteResult object
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create indexes for trigger rule log table to improve query performance
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_trigger_rule_log_timestamp ON trigger_rule_log(timestamp)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_trigger_rule_log_rule_id ON trigger_rule_log(trigger_rule_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_trigger_rule_log_created_at ON trigger_rule_log(created_at)"
        )
        logger.info("Trigger rule log table created successfully")

    def _create_rtsp_camera_table(self, conn: sqlite3.Connection) -> None:
        """Create RTSP camera table - 创建RTSP摄像头表"""
        cursor = conn.cursor()

        # 创建RTSP摄像头表，用于存储用户手动添加的RTSP摄像头信息
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rtsp_camera (
                id TEXT PRIMARY KEY,  -- 使用UUID作为主键
                name TEXT NOT NULL,  -- 摄像头名称
                location TEXT DEFAULT '',  -- 摄像头位置
                rtsp_url_main TEXT NOT NULL,  -- 主码流RTSP地址
                rtsp_url_sub TEXT DEFAULT '',  -- 子码流RTSP地址（可选）
                enabled BOOLEAN DEFAULT 1,  -- 是否启用: 1=启用, 0=禁用
                online_main BOOLEAN DEFAULT 0,  -- 主码流在线状态
                online_sub BOOLEAN DEFAULT 0,  -- 子码流在线状态
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 为RTSP摄像头表创建索引以提高查询性能
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_rtsp_camera_name ON rtsp_camera(name)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_rtsp_camera_enabled ON rtsp_camera(enabled)"
        )
        logger.info("RTSP camera table created successfully")

    @contextmanager
    def get_connection(self):
        """Get database connection context manager"""
        conn = None
        try:
            conn = sqlite3.connect(str(self.db_path),
                                   timeout=self.timeout,
                                   check_same_thread=self.check_same_thread,
                                   isolation_level=self.isolation_level)
            # Set row factory to return dictionary format results
            conn.row_factory = sqlite3.Row
            # Enable foreign key constraints
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error("Database connection error: %s", e)
            raise
        finally:
            if conn:
                conn.close()

    def execute_query(self,
                      query: str,
                      params: Optional[Tuple] = None) -> List[Dict[str, Any]]:
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

    def execute_update(self,
                       query: str,
                       params: Optional[Tuple] = None) -> int:
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

    def execute_many(self, query: str, params_list: List[Tuple]) -> int:
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

    def get_database_info(self) -> Dict[str, Any]:
        """Get database information"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                # Get database size
                db_size = self.db_path.stat().st_size if self.db_path.exists(
                ) else 0

                # Get table information
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
                tables = [row[0] for row in cursor.fetchall()]

                # Get database version
                cursor.execute("PRAGMA user_version")
                version = cursor.fetchone()[0]

                return {
                    "path": str(self.db_path),
                    "size": db_size,
                    "tables": tables,
                    "version": version,
                    "exists": self.db_path.exists()
                }

        except Exception as e:  # pylint: disable=broad-exception-caught
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
