# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Service manager module
"""

import logging
import uuid

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
from miloco.state import StateStore
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
            # 作用域代号：切换账号或家庭时 +1。对齐、属性推送、删除调和写容器之前都
            # 比一次，比不上就整段放弃 —— 挡掉旧作用域迟到的写入。
            # 只有切换编排递增它，且那段在一把锁里跑，没有第二个写者。
            cls._instance._scope = 0
            # 已对齐到哪一代。存代号不存布尔：代号一推进旧标记自动失效，
            # 没有「谁负责重置」这个问题。初值取一个不可能等于真实代号的数
            cls._instance._aligned_scope = -1
            # 启动对齐的 task。挂在这里而不是 lifespan 的局部变量里，
            # 切换编排才够得着去取消上一轮
            cls._instance.state_align_task = None
        return cls._instance

    def __init__(self):
        pass

    def current_scope(self) -> int:
        return self._scope

    def begin_scope_switch(self) -> int:
        """代号 +1 并返回新值。唯一的递增入口。"""
        self._scope += 1
        return self._scope

    def mark_scope_aligned(self, scope: int) -> None:
        """标记这一代已完成对齐。不是当前代就忽略 —— 那是迟到的旧对齐。"""
        if scope == self._scope:
            self._aligned_scope = scope

    def scope_is_aligned(self) -> bool:
        return self._aligned_scope == self._scope

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

        # Initialize rule module
        async with mon.track_async(NodeName.RULE_SERVICE, "init"):
            self._rule_service = await init_rule_service(self._miot_proxy)

        async with mon.track_async(NodeName.TERMINATE_EVALUATOR, "init"):
            self._terminate_evaluator = TerminateEvaluator(self._rule_service)
            self._terminate_evaluator.start()

        # Initialize perception module
        async with mon.track_async(NodeName.PERCEPTION_SERVICE, "init"):
            self._perception_service = await init_perception_module(
                self._miot_proxy, self._kv_repo
            )

        self._task_service = TaskService(rule_service=self._rule_service)

        # 状态容器：这里只做启动对齐一次，还没有消费方订阅它。
        # 对齐 task 由 lifespan 建并负责取消 —— 它要打若干次云端请求，不能挡住启动，
        # 而关闭时必须有人取消它，否则容器停了它还在往里写。
        self._state_store = StateStore()
        self._state_store.start()

        self._initialized = True

    def init_device_uuid(self):
        """Initialize device UUID"""
        device_uuid = self._kv_repo.get(SystemConfigKeys.DEVICE_UUID_KEY)
        if not device_uuid:
            device_uuid = uuid.uuid4().hex
            self._kv_repo.set(SystemConfigKeys.DEVICE_UUID_KEY, device_uuid)
        self.device_uuid = device_uuid

    # Service access properties
    @property
    def state_store(self) -> StateStore:
        return self._state_store

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

    @property
    def onboarding_trigger(self):
        """onboarding 主动邀请触发器懒加载单例。

        依赖以可调用注入（同 DeviceWelcomeService 风格）：米家就绪 = 已授权
        （token 在 KV）且家庭启用集非空；成员 / 档案空判定分别走 person_service
        与 home_profile store（正式区）。
        """
        svc = getattr(self, "_onboarding_trigger", None)
        if svc is None:
            from miloco.database.kv_repo import AuthConfigKeys
            from miloco.home_profile import store as hp_store
            from miloco.home_profile.onboarding_trigger import OnboardingTriggerService
            from miloco.miot.filter import allowed_home_ids

            kv = self._kv_repo
            svc = OnboardingTriggerService(
                kv_repo=kv,
                is_miot_ready=lambda: bool(kv.get(AuthConfigKeys.MIOT_TOKEN_INFO_KEY))
                and bool(allowed_home_ids(kv)),
                has_persons=lambda: bool(self._person_service.list_persons()),
                has_profile_entries=lambda: bool(hp_store.load_profile().entries),
            )
            self._onboarding_trigger = svc
        return svc

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
