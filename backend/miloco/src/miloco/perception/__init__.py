"""
Perception module — multimodal smart home perception engine.
"""

import asyncio
import logging

from miloco.database.perception_repo import PerceptionLogRepo
from miloco.perception.client import PerceptionEngineProxy
from miloco.perception.collect.camera_adapter import CameraDeviceAdapter
from miloco.perception.collect.collector import MultimodalCollector
from miloco.perception.processor import PipelineProcessor

logger = logging.getLogger(__name__)


async def init_perception_module(miot_proxy, kv_repo):
    """
    初始化感知模块所有组件
    :param miot_proxy: 外部传入的 miot 代理实例
    :param kv_repo: 持久化 KV 仓库，用于读取用户「感知开关」意图
    """
    from miloco.perception.runner import PerceptionRunner
    from miloco.perception.service import PerceptionService

    # 1. 初始化基础依赖实例
    perception_log_repo = PerceptionLogRepo()
    perception_engine_proxy = PerceptionEngineProxy()

    # 2. 创建窗口就绪事件（回调从流线程触发，需 threadsafe 调度到事件循环）
    loop = asyncio.get_running_loop()
    window_ready_event = asyncio.Event()

    # 3. 初始化相机适配器
    camera_adapter = CameraDeviceAdapter(
        miot_proxy,
        on_window_ready=lambda: loop.call_soon_threadsafe(window_ready_event.set),
    )

    # 4. 初始化多模态收集器
    multimodal_collector = MultimodalCollector([camera_adapter])

    # 5. 初始化管道处理器
    pipeline_processor = PipelineProcessor(
        collector=multimodal_collector,
        perception_engine_proxy=perception_engine_proxy,
        log_repo=perception_log_repo,
    )

    # 6. 初始化实时感知引擎
    perception_runner = PerceptionRunner(
        collector=multimodal_collector,
        pipeline=pipeline_processor,
        log_repo=perception_log_repo,
        window_ready_event=window_ready_event,
    )

    # 7. 初始化感知服务
    perception_service = PerceptionService(
        collector=multimodal_collector,
        pipeline=pipeline_processor,
        perception_runner=perception_runner,
        log_repo=perception_log_repo,
    )

    # 8. 启动引擎 —— 尊重用户「休息」意图：上次被手动暂停则不自动拉起，
    #    否则后台每次重启都会无视暂停、继续烧 token。
    from miloco.perception.engine_state import is_perception_enabled

    if is_perception_enabled(kv_repo):
        await perception_runner.start()
    else:
        logger.info("[perception] 上次被用户手动休息，跳过开机自动启动")

    return perception_service
