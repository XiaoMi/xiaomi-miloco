# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Service manager module
"""

import logging
import uuid

from miloco.automation.service import AutomationService
from miloco.config import get_settings
from miloco.database.kv_repo import KVRepo, SystemConfigKeys
from miloco.database.person_repo import PersonRepo
from miloco.home_profile.service import HomeProfileService
from miloco.miot.client import MiotProxy
from miloco.miot.service import MiotService
from miloco.node_monitor import NodeKind, NodeName, get_monitor
from miloco.perception import init_perception_module
from miloco.perception.service import PerceptionService
from miloco.person.service import PersonService
from miloco.rule.service import RuleService, init_rule_service
from miloco.rule.terminate_evaluator import TerminateEvaluator
from miloco.task.service import TaskService

logger = logging.getLogger(__name__)


class Manager:
    """
    Service manager singleton class - simplified version
    Only responsible for service initialization and providing access interfaces, no business logic
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        pass

    async def initialize(self):
        """
        Initialize all services
        """
        if getattr(self, "_initialized", False):
            logger.debug(
                "Manager already initialized, skipping duplicate initialization"
            )
            return

        logger.info("Manager initialization started")

        mon = get_monitor()
        mon.register(NodeName.CAMERA, NodeKind.SOURCE, watchdog_s=60)
        mon.register(NodeName.COLLECTOR, NodeKind.WINDOW, watchdog_s=60)
        mon.register(NodeName.PROCESSOR, NodeKind.WINDOW, watchdog_s=60)
        mon.register(NodeName.ENGINE, NodeKind.WINDOW, watchdog_s=60)
        mon.register(NodeName.RULE, NodeKind.EVENT, watchdog_s=60)
        mon.register(NodeName.MIOT_PROXY, NodeKind.SERVICE)
        mon.register(NodeName.RULE_SERVICE, NodeKind.SERVICE)
        mon.register(NodeName.PERCEPTION_SERVICE, NodeKind.SERVICE)
        mon.register(NodeName.TERMINATE_EVALUATOR, NodeKind.SERVICE)

        # Initialize repo layer
        self._kv_repo = KVRepo()
        self._person_repo = PersonRepo()

        # Initialize device UUID
        self.init_device_uuid()

        # Initialize proxy layer
        async with mon.track_async(NodeName.MIOT_PROXY, "init"):
            self._miot_proxy = await MiotProxy.create_miot_proxy(
                uuid=self.device_uuid,
                redirect_uri="https://mico.api.mijia.tech/login_redirect",
                kv_repo=self._kv_repo,
                cloud_server=get_settings().miot.cloud_server,
            )

        # Initialize all services
        self._miot_service = MiotService(
            self._miot_proxy,
            self._person_repo,
        )
        self._person_service = PersonService(self._person_repo)
        self._home_profile_service = HomeProfileService(self._person_service)
        self._automation_service = AutomationService(self._kv_repo)

        # Initialize rule module
        async with mon.track_async(NodeName.RULE_SERVICE, "init"):
            self._rule_service = await init_rule_service(self._miot_proxy)

        async with mon.track_async(NodeName.TERMINATE_EVALUATOR, "init"):
            self._terminate_evaluator = TerminateEvaluator(self._rule_service)
            self._terminate_evaluator.start()

        # Initialize perception module
        async with mon.track_async(NodeName.PERCEPTION_SERVICE, "init"):
            self._perception_service = await init_perception_module(self._miot_proxy)

        self._task_service = TaskService()

        self._initialized = True
        self._miot_proxy.register_automation_trigger_handler(
            self._handle_miot_automation_trigger
        )
        try:
            await self._miot_service.sync_automation_property_subscriptions(
                self._automation_service.list_mappings()
            )
        except Exception as e:
            logger.warning("Failed to sync automation MiOT subscriptions: %s", e)

    def init_device_uuid(self):
        """Initialize device UUID"""
        device_uuid = self._kv_repo.get(SystemConfigKeys.DEVICE_UUID_KEY)
        if not device_uuid:
            device_uuid = uuid.uuid4().hex
            self._kv_repo.set(SystemConfigKeys.DEVICE_UUID_KEY, device_uuid)
        self.device_uuid = device_uuid

    async def _handle_miot_automation_trigger(self, trigger) -> None:
        await self._automation_service.handle_trigger(
            trigger=trigger,
            perception_service=self._perception_service,
            rule_service=self._rule_service,
            miot_service=self._miot_service,
            meaningful_events_dao=self.meaningful_events_dao,
        )

    # Service access properties
    @property
    def miot_service(self) -> MiotService:
        return self._miot_service

    @property
    def person_service(self) -> PersonService:
        return self._person_service

    @property
    def home_profile_service(self) -> HomeProfileService:
        return self._home_profile_service

    @property
    def rule_service(self) -> RuleService:
        return self._rule_service

    @property
    def perception_service(self) -> PerceptionService:
        return self._perception_service

    @property
    def task_service(self) -> TaskService:
        return self._task_service

    @property
    def automation_service(self) -> AutomationService:
        return self._automation_service

    # Repo layer access properties
    @property
    def kv_repo(self) -> KVRepo:
        return self._kv_repo

    @property
    def meaningful_events_dao(self):
        """meaningful_events DAO 懒加载单例.

        放在 Manager 上让 _persist_meaningful_event / events_service / cleanup loop
        共用同一实例.SQLiteConnector 单例,DAO 仅持引用,初始化零成本.
        """
        dao = getattr(self, "_meaningful_events_dao", None)
        if dao is None:
            from miloco.database.meaningful_events_dao import MeaningfulEventDao

            dao = MeaningfulEventDao()
            self._meaningful_events_dao = dao
        return dao

    @property
    def events_service(self):
        """events_service 懒加载单例;复用 self.meaningful_events_dao."""
        svc = getattr(self, "_events_service", None)
        if svc is None:
            from miloco.perception.events_service import EventsService

            svc = EventsService(self.meaningful_events_dao)
            self._events_service = svc
        return svc

    # Proxy layer access properties
    @property
    def miot_proxy(self) -> MiotProxy:
        return self._miot_proxy

    # 主动注册:registration session manager lazy 单例
    # 进程内单一实例,管理 pending dict + commit / sessions / rollback。
    @property
    def register_session_manager(self):
        rsm = getattr(self, "_register_session_manager", None)
        if rsm is None:
            from miloco.perception.engine.identity.config_loader import (
                resolve_library_root,
            )
            from miloco.perception.engine.identity.library import IdentityLibrary
            from miloco.perception.engine.identity.registration_session import (
                RegistrationSessionManager,
            )
            lib = IdentityLibrary(resolve_library_root())
            rsm = RegistrationSessionManager(lib)
            self._register_session_manager = rsm
        return rsm


# Global singleton instance
manager_instance: Manager | None = None


def get_manager():
    """Get Manager singleton instance"""
    global manager_instance
    if manager_instance is None:
        manager_instance = Manager()
    return manager_instance
