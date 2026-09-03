# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Service manager module
"""

import asyncio
import logging
import uuid

from miloco.config import get_settings
from miloco.database.kv_repo import KVRepo, SystemConfigKeys
from miloco.database.person_repo import PersonRepo
from miloco.home_profile.service import HomeProfileService
from miloco.miot.client import MiotProxy
from miloco.miot.mips_listeners import PropTopUpListener
from miloco.miot.service import MiotService
from miloco.miot.state_align import align_iot_state, read_missing_props
from miloco.miot.state_push import IotPushWriter
from miloco.node_monitor import NodeKind, NodeName, get_monitor
from miloco.perception import init_perception_module
from miloco.perception.service import PerceptionService
from miloco.person.service import PersonService
from miloco.rule.service import RuleService, init_rule_service
from miloco.rule.terminate_evaluator import TerminateEvaluator
from miloco.state import StateStore
from miloco.task.service import TaskService

logger = logging.getLogger(__name__)

# 同一作用域代内对同一台设备最多补拉几次。补拉的判据是「容器里没有这条叶子」，而永久
# 不可读的属性永远不会进容器 —— 不限次的话，一台反复掉线的设备每次上线都会重新请求同一
# 批读不到的属性，挤米家限频。留几次是为了瞬时不可读还有机会
TOP_UP_MAX_ATTEMPTS = 3


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
            # 容器在这里建、在 initialize() 里 start：切换编排够得着它的时候
            # initialize() 不一定跑完，而没建起来的话编排会在清空那一步炸掉
            cls._instance._state_store = StateStore()
            cls._instance._iot_push_writer = None
            cls._instance._prop_top_up = None
            # did → 本代已补拉次数。切作用域时清空（对齐也是一代跑一次，口径一致）
            cls._instance._top_up_attempts = {}
        return cls._instance

    def __init__(self):
        pass

    def _wire_iot_push(self) -> None:
        """把两条推送接进状态容器。

        写入器挂在 Manager 上是因为它要同时够得着容器和 proxy，别处都够不着其中一个；
        订阅归 proxy，容器只是消费方之一。

        单独一个方法是为了可测：漏挂一条 listener 的后果是推送静默不进容器，而
        initialize() 整条在测试里跑不起来。
        """
        self._iot_push_writer = IotPushWriter(
            self._state_store,
            self._miot_proxy,
            scope_is_aligned=self.scope_is_aligned,
        )
        # 上线补拉是上下线扇出的第二个消费方，与写入器互不知情：写入器只管把标志写进
        # 容器，补拉只管把这台设备缺的属性补齐
        self._prop_top_up = PropTopUpListener(top_up=self._top_up_props)
        self._miot_proxy.add_device_state_listener(
            self._iot_push_writer.on_device_state
        )
        self._miot_proxy.add_device_props_listener(
            self._iot_push_writer.on_device_props
        )
        self._miot_proxy.add_device_state_listener(self._prop_top_up.on_event)

    async def _top_up_props(self, did: str) -> int:
        """设备转上线时补拉它在容器里缺的属性。返回写进去的条数。

        **正确性不在这里，在写入器。** 写入的两道闸（本代已对齐 / 还在当前家庭）由
        `IotPushWriter.write_pulled_props` 在写入的那一刻判，与推送共用一份 —— 补拉要打
        一趟云端，往返期间设备可能搬出当前家庭（同一代之内、代号不变），在这里判是判不住
        的。这个方法只做两件写入器管不了的事：

        * **额度**：判据「容器里没有这条叶子」是代理指标，永久不可读的属性永远不会进
          容器，不限次的话反复掉线的设备每次上线都重新请求同一批（见
          TOP_UP_MAX_ATTEMPTS）。只在真打了云端时才算一次 —— 空跑消耗额度会把「留几次
          给瞬时不可读」这个用意吃掉；
        * **在线检查**：离线设备云端给的是缓存里的最后一次上报、可能任意旧，写进去会把
          `last_reported` 刷成当下而响应不带时间戳，消费方看不出来 —— 对齐当初跳过离线
          设备就是这个理由。这一条写入器判不了（它不知道这批值是拉来的还是推来的）。

        「本代已对齐」和「还在当前家庭」那两道是**早退**，为的是省掉没必要的云端往返
        （尤其对齐窗口里：那时补进去的叶子会被对齐的整台替换直接删掉），不是闸 —— 真闸
        在写入器里，删掉这两道只会变慢，不会变错。**在线检查那道不能删**，它就是上面那条
        写入器管不了的事。
        """
        if not self.scope_is_aligned():
            logger.debug("top-up: 本代还没对齐完，跳过 did=%s", did)
            return 0
        device = (await self._miot_proxy.devices_in_current_home()).get(did)
        if device is None:
            logger.debug("top-up: 不在当前家庭，跳过 did=%s", did)
            return 0
        if not getattr(device, "online", True):
            logger.debug("top-up: 防抖窗口里又掉线了，跳过 did=%s", did)
            return 0
        used = self._top_up_attempts.get(did, 0)
        if used >= TOP_UP_MAX_ATTEMPTS:
            logger.debug("top-up: 本代额度已用完，跳过 did=%s", did)
            return 0
        scope_at_start = self._scope
        requested, values = await read_missing_props(
            self._state_store, self._miot_proxy, did
        )
        # 只给发起这次请求的那一代记账：往返期间切了作用域的话，计数表已经被
        # begin_scope_switch 清过，这时写回去等于给新一代预扣一次
        if requested and self._scope == scope_at_start:
            self._top_up_attempts[did] = used + 1
        if not values:
            return 0
        return await self._iot_push_writer.write_pulled_props(did, values)

    def deinit_iot_push(self) -> None:
        """取消补拉的待触发计时器。

        与 `MiotProxy.deinit` 里那四个 listener 同理：不取消的话，关闭期间计时器到点会
        对着一个已经拆掉一半的 proxy 打云端、往已经 stop 的容器里写。
        """
        if self._prop_top_up is not None:
            self._prop_top_up.deinit()

    def current_scope(self) -> int:
        return self._scope

    def begin_scope_switch(self) -> int:
        """代号 +1 并返回新值。唯一的递增入口。"""
        self._scope += 1
        self._top_up_attempts.clear()
        return self._scope

    def mark_scope_aligned(self, scope: int) -> None:
        """标记这一代已完成对齐。不是当前代就忽略 —— 那是迟到的旧对齐。"""
        if scope == self._scope:
            self._aligned_scope = scope

    def scope_is_aligned(self) -> bool:
        return self._aligned_scope == self._scope

    def start_state_alignment(self) -> asyncio.Task | None:
        """起一轮状态对齐，跑完把这一代标成已对齐。

        句柄留在 state_align_task 上：下一次作用域切换必须先取消它，否则它会把旧
        作用域的值写进刚清空重建的树。

        初始化还没跑完时只记一条日志、不起对齐 —— 这一代因此停在「未对齐」，依赖它的
        属性订阅门是关着的，正是安全的那一侧。
        """
        if not self._initialized:
            logger.warning("初始化还没跑完，这一代不做状态对齐")
            return None
        scope = self._scope
        proxy = self._miot_proxy

        async def run() -> None:
            if await align_iot_state(
                self._state_store,
                proxy,
                scope=scope,
                current_scope=self.current_scope,
            ):
                self.mark_scope_aligned(scope)

        self.state_align_task = asyncio.create_task(run())
        return self.state_align_task

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

        # 容器本身在 __new__ 里就建好了，这里只是接上 event loop 开始投递。必须排在
        # 所有写入方之前：没 start 的容器照样收值，但变更事件会被丢掉（只留一条告警和
        # dropped 计数），等接了订阅方就是每次启动静默漏掉第一批边沿。
        # 对齐由 start_state_alignment 起，关闭时 lifespan 取消，切换时编排取消
        self._state_store.start()

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

        self._wire_iot_push()

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
    def iot_push_writer(self) -> IotPushWriter | None:
        return self._iot_push_writer

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
                is_miot_ready=lambda: (
                    bool(kv.get(AuthConfigKeys.MIOT_TOKEN_INFO_KEY))
                    and bool(allowed_home_ids(kv))
                ),
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
