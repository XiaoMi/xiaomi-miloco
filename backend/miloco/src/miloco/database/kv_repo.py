# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Key-value data access object
Handles CRUD operations for kv table, provides generic key-value storage functionality
"""

import logging
import sqlite3
from typing import Any

from miloco.database.connector import get_db_connector
from miloco.utils.time_utils import now_ms

logger = logging.getLogger(__name__)


class KVRepo:
    """Key-value data access object"""

    def __init__(self):
        self.db_connector = get_db_connector()
        self.cache = self.get_all_as_dict()
        logger.info("KVRepo init, keys: %s", list(self.cache.keys()))

    def set(self, key: str, value: str) -> bool:
        """
        Set configuration item (create if not exists, update if exists)

        Args:
            key: Configuration key
            value: Configuration value

        Returns:
            bool: True if operation successful, False otherwise
        """
        try:
            current_time = now_ms()
            sql = """
                INSERT INTO kv (key, value, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
            """
            params = (key, value, current_time, current_time)
            affected_rows = self.db_connector.execute_update(sql, params)
            if affected_rows > 0:
                self.cache[key] = value
                logger.info("KV set successfully: key=%s", key)
                return True
            else:
                logger.warning("Failed to set kv: key=%s", key)
                return False
        except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as e:
            logger.error("Error setting kv: key=%s, error=%s", key, e)
            return False

    def _get_by_key(self, key: str) -> dict[str, Any] | None:
        """
        Get configuration item by key

        Args:
            key: Configuration key

        Returns:
            Optional[Dict[str, Any]]: Configuration item info, None if not exists
        """
        try:
            sql = "SELECT * FROM kv WHERE key = ?"
            params = (key,)
            results = self.db_connector.execute_query(sql, params)
            if results:
                logger.debug("KV found: key=%s", key)
                return results[0]
            else:
                logger.debug("KV not found: key=%s", key)
                return None
        except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as e:
            logger.error("Error querying kv: key=%s, error=%s", key, e)
            return None

    def get(self, key: str, default_value: str | None = None) -> str | None:
        """
        Get configuration value by key

        Args:
            key: Configuration key
            default_value: Default value if configuration doesn't exist

        Returns:
            Optional[str]: Configuration value
        """
        if key in self.cache:
            return self.cache[key]
        kv = self._get_by_key(key)
        if kv:
            return kv.get("value")
        return default_value

    def get_all(self) -> dict[str, str]:
        """
        Get all configuration items

        Returns:
            Dict[str, str]: Dictionary with key as key, value as value
        """
        return self.cache

    def _get_all(self) -> list[dict[str, Any]]:
        """
        Get all configuration items

        Returns:
            List[Dict[str, Any]]: List of all configuration items
        """
        try:
            sql = "SELECT * FROM kv ORDER BY key"
            results = self.db_connector.execute_query(sql)
            logger.debug("Retrieved %d kv items", len(results))
            return results
        except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as e:
            logger.error("Error retrieving all kv: error=%s", e)
            return []

    def get_all_as_dict(self) -> dict[str, str]:
        """
        Get all configuration items and convert to key-value dictionary format

        Returns:
            Dict[str, str]: Dictionary with key as key, value as value
        """
        try:
            all_kvs = self._get_all()
            kv_dict = {}

            for kv in all_kvs:
                key = kv.get("key")
                value = kv.get("value")
                if key is not None and value is not None:
                    kv_dict[key] = value
            logger.info("Retrieved %d kv as dict", len(kv_dict))
            return kv_dict
        except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as e:
            logger.error("Error converting kv to dict: error=%s", e)
            return {}

    def delete(self, key: str) -> bool:
        """
        Delete configuration item

        Args:
            key: Configuration key

        Returns:
            bool: True if deletion successful, False otherwise
        """
        try:
            sql = "DELETE FROM kv WHERE key = ?"
            params = (key,)
            affected_rows = self.db_connector.execute_update(sql, params)

            if affected_rows > 0:
                self.cache.pop(key, None)
                logger.info("KV deleted successfully: key=%s", key)
                return True
            else:
                logger.debug("KV key not found, skip delete: key=%s", key)
                return False

        except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as e:
            logger.error("Error deleting kv: key=%s, error=%s", key, e)
            return False

    def exists(self, key: str) -> bool:
        """
        Check if configuration item exists

        Args:
            key: Configuration key

        Returns:
            bool: True if exists, False otherwise
        """
        try:
            if key in self.cache:
                return True
            sql = "SELECT COUNT(*) as count FROM kv WHERE key = ?"
            params = (key,)
            results = self.db_connector.execute_query(sql, params)

            if results and results[0]["count"] > 0:
                return True
            return False

        except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as e:
            logger.error("Error checking kv existence: key=%s, error=%s", key, e)
            return False


class AuthConfigKeys:
    MIOT_TOKEN_INFO_KEY = "MIOT_TOKEN_INFO_KEY"
    # 米家授权健康度（MiotAuthState 的 JSON）。与 MIOT_TOKEN_INFO_KEY 分开存:
    # 令牌本身还在、但云端已经拒绝续期,是两件事。落库而非只放内存,是为了让
    # 「授权已失效」这个判断在进程重启后立刻可用——否则重启到第一次刷新之间
    # (最长 5 分钟)界面会误报「一切正常」。
    MIOT_AUTH_STATE_KEY = "MIOT_AUTH_STATE_KEY"
    # 「正在刷新」的意向标记:发请求前写、拿到结果后清。
    # 刷新令牌是一次性的,请求一旦抵达云端,旧令牌无论我们有没有收到响应都已作废。
    # 若在这中间崩溃,新令牌从未到达本机、无从挽回。但重启后看到这个标记还在,
    # 只能断定「上一轮结果未知」——请求也可能压根没发出去,两种现场的痕迹一样。
    # 所以不预先判死,而是让下一次定时检查不等临期、立刻验一次,由验证结果定论。
    MIOT_REFRESH_INFLIGHT_KEY = "MIOT_REFRESH_INFLIGHT_KEY"


class SystemConfigKeys:
    DEVICE_UUID_KEY = "DEVICE_UUID_KEY"
    PERCEPTION_ENABLED_KEY = "PERCEPTION_ENABLED_KEY"  # 用户「感知开关」意图（缺省=开）


class DeviceInfoKeys:
    USER_INFO_KEY = "USER_INFO_KEY"


class ScopeConfigKeys:
    """miloco 接入范围限定（家庭启用集 / 摄像头停用集）。

    ``*_LIST_KEY`` 值统一为 JSON array 字符串（``"[]"`` / ``NULL`` 都表示空集）；
    ``CAMERA_PROMPT_MAP_KEY`` 是唯一例外——JSON object（did→prompt），非集合。
    """

    HOME_WHITE_LIST_KEY = "HOME_WHITE_LIST_KEY"       # 已启用的家庭 home_id 列表
    CAMERA_BLACK_LIST_KEY = "CAMERA_BLACK_LIST_KEY" # 已停用的摄像头 did 列表
    # 已**开启**「拾音」的摄像头 did 列表（allow-list，opt-in）；不在此集 = 拾音关闭
    # （**默认关闭**）。默认关是产品决策：现阶段远场拾音/转写质量不稳，默认开会带来
    # 误报等负体验，故改为用户按场景显式开启（详见前端开启时的知情提示）。
    # 与 CAMERA_BLACK_LIST_KEY 正交：相机可正常投喂**视频**感知，但只有在本集内的相机
    # 音频才会被处理（转写 / 语音派生 / 上云）；不在集内 = 引擎入口整批剥离音频。
    # KV 读取失败时按空集处理（fail-closed：宁可不处理，也不擅自开启未授权相机的音频）。
    CAMERA_VOICE_ALLOW_LIST_KEY = "CAMERA_VOICE_ALLOW_LIST_KEY"
    # 每摄像头「感知须知」自定义 prompt 映射（did→文本）。JSON object，缺省 = 无自定义。
    # 与上面几个集合类 key 结构不同（map 而非 list）：每台内容各异，需按 did 精确取值。
    # 该 prompt 作为**场景指导**注入 omni 的 **system prompt 尾部**（低频变动放尾部，前面
    # 共享前缀稳定、利于 prefix cache），video / audio 路由均注入；引擎每感知窗实时读取，
    # 改动下一窗即生效、不重启。用途：给模型补充该机位的环境描述 / 关注点 / 忽略项，
    # 消除固定误识（如门口机位把公共走廊电梯门误当自家入户门）。读取失败按「无自定义」处理。
    CAMERA_PROMPT_MAP_KEY = "CAMERA_PROMPT_MAP_KEY"
    # 已**关闭** Smart Crop（智能裁切增强）的摄像头 did 列表（deny-list，默认开启）；
    # 不在此集 = 该机位允许裁切。JSON array，空集 / NULL = 全部机位都开。
    # 键口径同 CAMERA_PROMPT_MAP_KEY：按**合成 did**（``did:ch{n}``，多通道相机逐路），
    # 因为"该不该裁"取决于该路镜头的视野（门口窄视野收益小、客厅广角收益大），逐路不同；
    # 与按整台走的 CAMERA_BLACK_LIST_KEY / CAMERA_VOICE_ALLOW_LIST_KEY 不同口径。
    # 与全局双闸（perception.engine.crop_enhance.enabled / user_enabled）**相与**：
    # 全局关则本集无意义，全局开时才逐机位看本集。引擎每感知窗实时读取，改动下一窗即生效、
    # 不重启（见 perception/engine/api.py 的 _crop_denied_dids）。
    # 读取失败按**空集**处理（fail-open = 继续裁）：本项只影响视频编码构图、不涉隐私，
    # 且"默认开启"的语义本就是"缺信息即为开"；反向 fail-closed 会让一次 KV 抖动静默关掉
    # 全家 Smart Crop，是更难发现的失效态。与 CAMERA_VOICE_ALLOW_LIST_KEY 的 fail-closed
    # 相反正是因为那项涉隐私。
    CAMERA_CROP_DENY_LIST_KEY = "CAMERA_CROP_DENY_LIST_KEY"

class OnboardingKeys:
    """主动 onboarding 邀请的一次性标记。

    值为发送成功时的 ISO 时间戳。终身一次：置位后不再自动清除，
    也没有重发定时器（用户之后随时可说「初始化家庭」手动发起）。
    """

    ONBOARDING_PROMPTED_KEY = "ONBOARDING_PROMPTED_KEY"  # 主动邀请已成功送达
