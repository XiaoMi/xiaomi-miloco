# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Service manager module
"""

import logging
import uuid
from typing import Callable, Optional


from miloco_server.dao.chat_history_dao import ChatHistoryDAO
from miloco_server.dao.trigger_rule_log_dao import TriggerRuleLogDAO
from miloco_server.schema.auth_schema import UserLanguage
from miloco_server.utils.local_models import ModelPurpose
from miloco_server.utils.default_action import DefaultPresetActionManager
from miloco_server.mcp.tool_executor import ToolExecutor
from miloco_server.utils.cleaner import Cleaner
from miloco_server.dao.kv_dao import KVDao, SystemConfigKeys
from miloco_server.dao.trigger_dao import TriggerRuleDAO
from miloco_server.dao.third_party_model_dao import ThirdPartyModelDAO
from miloco_server.dao.mcp_config_dao import MCPConfigDAO
from miloco_server.proxy.llm_proxy import LLMProxy
from miloco_server.proxy.miot_proxy import MiotProxy
from miloco_server.proxy.ha_proxy import HAProxy
from miloco_server.service.trigger_rule_runner import TriggerRuleRunner
from miloco_server.mcp.mcp_client_manager import MCPClientManager
from miloco_server.service.auth_service import AuthService
from miloco_server.service.miot_service import MiotService
from miloco_server.service.ha_service import HaService
from miloco_server.service.trigger_rule_service import TriggerRuleService
from miloco_server.service.model_service import ModelService
from miloco_server.service.mcp_service import McpService
from miloco_server.service.chat_history_service import ChatHistoryService
from miloco_server.config.normal_config import MIOT_CONFIG, RTSP_CAMERA_CONFIG
from miloco_server.utils.chat_companion import ChatCompanion

logger = logging.getLogger(__name__)


class Manager:
    """
    Service manager singleton class - simplified version
    Only responsible for service initialization and providing access interfaces, no business logic
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Manager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        pass

    async def initialize(self, callback: Optional[Callable[[], None]] = None):
        """
        Initialize all services
        """
        if getattr(self, "_initialized", False):
            logger.debug("Manager already initialized, skipping duplicate initialization")
            return

        logger.info("Manager initialization started")

        self._initialized = True

        # Initialize DAO layer
        self._kv_dao = KVDao()
        self._trigger_rule_dao = TriggerRuleDAO()
        self._third_party_model_dao = ThirdPartyModelDAO()
        self._mcp_config_dao = MCPConfigDAO()
        self._chat_history_dao = ChatHistoryDAO()
        self._trigger_rule_log_dao = TriggerRuleLogDAO()
        self._cleaner = Cleaner(self._chat_history_dao, self._trigger_rule_log_dao)
        self._chat_companion = ChatCompanion(self._chat_history_dao)

        # Initialize device UUID
        self.init_device_uuid()

        # Initialize proxy layer
        self._miot_proxy = await MiotProxy.create_miot_proxy(
            uuid=self.device_uuid,
            redirect_uri="https://mico.api.mijia.tech/login_redirect",
            kv_dao=self._kv_dao,
            cloud_server=MIOT_CONFIG["cloud_server"],
            rtsp_cameras=RTSP_CAMERA_CONFIG)

        self._ha_proxy = HAProxy(kv_dao=self._kv_dao)

        # LLM proxy initialization moved to ModelService.__init__ for automatic execution

        # Initialize MCP client manager
        self._mcp_client_manager = await MCPClientManager.create(self._mcp_config_dao, self._miot_proxy, self._ha_proxy)

        # Initialize tool executor
        self._tool_executor = ToolExecutor(self._mcp_client_manager)

        # Initialize default preset action manager
        self._default_preset_action_manager = DefaultPresetActionManager(self._tool_executor)

        # Initialize trigger
        self._trigger_rule_runner = TriggerRuleRunner(
            trigger_rules=self._trigger_rule_dao.get_all(enabled_only=False),
            miot_proxy=self._miot_proxy,
            get_llm_proxy_by_purpose=self.get_llm_proxy_by_purpose,
            get_language=self.get_language,
            tool_executor=self._tool_executor,
            trigger_rule_log_dao=self._trigger_rule_log_dao,
        )

        # Initialize all services
        self._auth_service = AuthService(self._kv_dao)
        self._miot_service = MiotService(
            self._miot_proxy, self._mcp_client_manager, self._default_preset_action_manager)
        self._ha_service = HaService(self._ha_proxy, self._mcp_client_manager, self._default_preset_action_manager)
        self._model_service = ModelService(self._kv_dao, self._third_party_model_dao)
        self._mcp_service = McpService(self._mcp_config_dao, self._mcp_client_manager)
        self._chat_service = ChatHistoryService(self._chat_history_dao, self._chat_companion)
        self._trigger_rule_service = TriggerRuleService(
            self._trigger_rule_dao,
            self._trigger_rule_log_dao,
            self._trigger_rule_runner,
            self._miot_proxy,
            self._mcp_client_manager
        )

        self._trigger_rule_runner.start_periodic_task()

        if callback:
            callback()
        logger.info("Manager initialization completed")

    def init_device_uuid(self):
        """Initialize device UUID"""
        device_uuid = self._kv_dao.get(SystemConfigKeys.DEVICE_UUID_KEY)
        if not device_uuid:
            device_uuid = uuid.uuid4().hex
            self._kv_dao.set(SystemConfigKeys.DEVICE_UUID_KEY, device_uuid)
        self.device_uuid = device_uuid

    # Service access properties
    @property
    def auth_service(self) -> AuthService:
        return self._auth_service

    @property
    def miot_service(self) -> MiotService:
        return self._miot_service

    @property
    def ha_service(self) -> HaService:
        return self._ha_service

    @property
    def trigger_rule_service(self) -> TriggerRuleService:
        return self._trigger_rule_service

    @property
    def model_service(self) -> ModelService:
        return self._model_service

    @property
    def mcp_service(self) -> McpService:
        return self._mcp_service

    @property
    def chat_service(self) -> ChatHistoryService:
        return self._chat_service

    @property
    def chat_companion(self) -> ChatCompanion:
        return self._chat_companion

    # Tool and proxy access properties
    @property
    def tool_executor(self) -> ToolExecutor:
        return self._tool_executor

    @property
    def default_preset_action_manager(self) -> DefaultPresetActionManager:
        return self._default_preset_action_manager

    def get_llm_proxy_by_purpose(self, purpose: ModelPurpose) -> LLMProxy:
        llm_proxy_by_purpose = self._model_service.get_llm_proxy()
        if purpose not in llm_proxy_by_purpose:
            logger.warning("LLM proxy not set in purpose: %s", purpose)
            return None
        return llm_proxy_by_purpose[purpose]

    def get_language(self) -> UserLanguage:
        return self._auth_service.get_user_language().language

    # DAO layer access properties
    @property
    def kv_dao(self) -> KVDao:
        return self._kv_dao

    @property
    def trigger_rule_dao(self) -> TriggerRuleDAO:
        return self._trigger_rule_dao

    @property
    def third_party_model_dao(self) -> ThirdPartyModelDAO:
        return self._third_party_model_dao

    @property
    def mcp_config_dao(self) -> MCPConfigDAO:
        return self._mcp_config_dao

    @property
    def chat_history_dao(self) -> ChatHistoryDAO:
        return self._chat_history_dao

    @property
    def trigger_rule_log_dao(self) -> TriggerRuleLogDAO:
        return self._trigger_rule_log_dao

    @property
    def cleaner(self) -> Cleaner:
        return self._cleaner

    # Proxy layer access properties
    @property
    def miot_proxy(self) -> MiotProxy:
        return self._miot_proxy

    @property
    def ha_proxy(self) -> HAProxy:
        return self._ha_proxy

# Global singleton instance
manager_instance = None


def get_manager():
    """Get Manager singleton instance"""
    global manager_instance
    if manager_instance is None:
        manager_instance = Manager()
    return manager_instance
