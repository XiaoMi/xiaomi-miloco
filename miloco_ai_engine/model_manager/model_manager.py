"""Model manager module for managing AI models lifecycle and requests."""
from enum import Enum
from thespian.actors import ActorAddress
import asyncio
from typing import Dict, AsyncGenerator, List
from collections import deque
from miloco_ai_engine.config.config import MODELS_CONFIG
from miloco_ai_engine.model_manager.model_wrapper import ModelWrapper
from miloco_ai_engine.schema.actor_message import RequestMessage, actor_system, ModelAction, ResultMessage
from miloco_ai_engine.config.config_info import ModelConfig
from miloco_ai_engine.config.config_optimizer import adjust_config_by_memory
from miloco_ai_engine.schema.models_schema import ChatCompletionRequest, ChatCompletionResponse, ModelInfo, ModelDescription, VramUsage
from miloco_ai_engine.utils.cuda_info import estimate_vram_usage, get_cuda_memory_info
from miloco_ai_engine.middleware.exceptions import InvalidArgException, ModelSchedulerException, CoreNormalException, ModelManagerException
import time
import gc
import logging
import threading
logger = logging.getLogger(__name__)

class ModelLoadStrategy(Enum):
    """Model loading strategy"""
    RESIDENT = "resident"  # Resident in memory
    ON_DEMAND = "on_demand"  # Load on demand ##Not enabled##
    ONLY_ONE = "only_one"  # Only load the last used model

    @classmethod
    def _missing_(cls, value):
        logger.warning(
            "Invalid load strategy value: %s, using default resident strategy", value)
        return cls.RESIDENT


class ModelManager():
    """Model Manager"""

    MODEL_REQUER_TIMEOUT = ModelWrapper.MODEL_REQUER_TIMEOUT + 1

    def __init__(self):
        self.running = False

        self.model_loadable = threading.Event()  # Event to signal any model is loading
        self.model_loadable.set()

        self.models: Dict[str, ActorAddress] = {}
        self.model_configs: Dict[str, ModelConfig] = {}
        self.loaded_models: deque = deque()  # Loaded models queue (thread-safe)
        self._init_models()

    def _init_models(self):
        for model_name, config in MODELS_CONFIG.items():
            model_config = ModelConfig(model_name=model_name, **config)
            self.model_configs[model_name] = model_config
            self.models[model_name] = actor_system.createActor(
                lambda m_name=model_name, m_config=model_config: ModelWrapper(m_name, m_config))

    async def start(self):
        """
        Start model manager
        """
        self.running = True
        # Start cleanup task
        self.cleanup_task = asyncio.create_task(self._start_cleanup_task())
        # Preload models according to global strategy
        await self._preload_all_models()

        logger.info(
            "Model manager started, managing %d models", len(self.models))

    async def stop(self):
        """
        Stop model manager
        """
        self.running = False
        if self.cleanup_task:
            self.cleanup_task.cancel()

        models = list(self.loaded_models)
        for model_name in models:
            await self._unload_model(model_name)

        self.models.clear()
        self.loaded_models.clear()
        logger.info("Model manager stopped")

    async def auto_load_model(self, model_name: str):
        """
        Automatically load model according to global strategy
        """
        await self._load_model(model_name)

    async def auto_unload_model(self, model_name: str):
        """
        Automatically unload model according to global strategy
        """
        await self._unload_model(model_name)

    def model_list(self) -> List[str]:
        """
        Get model list
        """
        return list(self.models.keys())

    def model_info(self, model_name: str) -> ModelInfo:
        """
        Get model configuration
        """
        if model_name in self.models:
            return ModelInfo(id=model_name,
                             object="model",
                             created=int(time.time()),
                             owned_by="llama-mico")

    def model_desc(self, model_name: str) -> ModelDescription:
        """
        Get model description
        """
        if model_name in self.model_configs:
            model_config = self.model_configs[model_name]
            model_info = self.model_info(model_name)
            # adjust_config_by_memory(model_config) # Adjust config by free memory
            return ModelDescription(id=model_info.id,
                                    object=model_info.object,
                                    created=model_info.created,
                                    owned_by=model_info.owned_by,
                                    loaded=model_name in self.loaded_models,
                                    estimate_vram_usage=estimate_vram_usage(
                                        model_config.model_path,
                                        model_config.mmproj_path,
                                        model_config.total_context_num,
                                        model_config.chunk_size)
                                    )

    async def chat_completions(
            self, model_name: str,
            request: ChatCompletionRequest) -> ChatCompletionResponse:
        """
        Chat completion
        """
        if model_name not in self.loaded_models:
            raise ModelManagerException(f"Model not loaded: {model_name}")

        future: asyncio.Future = actor_system.ask(
            self.models[model_name],
            RequestMessage(action=ModelAction.CHAT, data=request))
        current_loop = asyncio.get_running_loop()
        future = self._mirror_future_to_loop(future, current_loop)

        try:
            # Different asyncio loop may cause issues with future
            result_message: ResultMessage = await asyncio.wait_for(future, timeout=self.MODEL_REQUER_TIMEOUT)
            if result_message.result:
                return result_message.data
            else:
                raise ModelSchedulerException(result_message.error)
        except asyncio.TimeoutError as exc:
            logger.error("Chat completion timeout(%fs)", self.MODEL_REQUER_TIMEOUT)
            raise ModelSchedulerException(f"Chat completion timeout({self.MODEL_REQUER_TIMEOUT}s)") from exc
        except Exception as e:
            logger.error("Chat completion failed: %s", e)
            raise CoreNormalException(f"Chat completion failed: {e}") from e

    async def chat_completions_stream(
        self, model_name: str, request: ChatCompletionRequest
    ) -> AsyncGenerator[ChatCompletionResponse, None]:
        """
        Stream chat completion
        """
        if model_name not in self.loaded_models:
            logger.error("Model not loaded: %s", model_name)
            raise ModelManagerException(f"Model not loaded: {model_name}")

        future: asyncio.Future = actor_system.ask(
            self.models[model_name],
            RequestMessage(action=ModelAction.STREAM_CHAT, data=request))
        current_loop = asyncio.get_running_loop()
        future = self._mirror_future_to_loop(future, current_loop)

        try:
            # Different asyncio loop may cause issues with future
            result_message: ResultMessage = await asyncio.wait_for(future, timeout=self.MODEL_REQUER_TIMEOUT)
            if result_message.result:
                return result_message.data
            else:
                raise ModelSchedulerException(result_message.error)
        except asyncio.TimeoutError as exc:
            logger.error("Stream chat completion timeout(%fs)", self.MODEL_REQUER_TIMEOUT)
            raise ModelSchedulerException(
                f"Stream chat completion timeout({self.MODEL_REQUER_TIMEOUT}s)") from exc
        except Exception as e:
            logger.error("Stream chat completion failed: %s", e)
            raise CoreNormalException(f"Stream chat completion failed: {e}") from e

    async def _preload_all_models(self):
        """
        Preload all models
        """
        pass

    def _mirror_future_to_loop(self, source_future: asyncio.Future,
                               target_loop: asyncio.AbstractEventLoop) -> asyncio.Future:
        """
        Ensure awaiting future belongs to the target event loop.
        """

        try:
            source_loop = source_future.get_loop()
        except RuntimeError:
            source_loop = None

        if source_loop is None or source_loop is target_loop:
            return source_future

        proxy_future: asyncio.Future = target_loop.create_future()

        def _transfer_result(done_future: asyncio.Future):
            if done_future.cancelled():
                target_loop.call_soon_threadsafe(proxy_future.cancel)
                return
            try:
                result = done_future.result()
            except Exception as exc:  # pylint: disable=broad-except
                target_loop.call_soon_threadsafe(
                    proxy_future.set_exception, exc)
            else:
                target_loop.call_soon_threadsafe(
                    proxy_future.set_result, result)

        source_loop.call_soon_threadsafe(
            source_future.add_done_callback, _transfer_result)

        return proxy_future

    async def _start_cleanup_task(self):
        """
        Start cleanup task
        """
        while self.running:
            try:
                await asyncio.sleep(60)
                # todo cleanup models

                # Optimize performance
                logger.info("Performing performance optimization...")
                for _, model_address in self.models.items():
                    actor_system.tell(
                        model_address,
                        RequestMessage(action=ModelAction.CLEANUP, data={}))

                # gc collection
                gc.collect()

                logger.info("Performance optimization completed")
            except asyncio.CancelledError:
                logger.info("Cleanup task cancelled")
                break
            except Exception as e:  # pylint: disable=broad-except
                logger.error("Cleanup task error: %s", e)

    async def _load_model(self, model_name: str):
        """
        Load model
        """
        if model_name in self.loaded_models:
            logger.info("Model %s already loaded", model_name)
            return
        if model_name not in self.models:
            logger.error("Model %s not configured", model_name)
            raise InvalidArgException(f"Model {model_name} not configured")

        logger.info("Loading model %s", model_name)
        self.model_loadable.wait()  # Wait for anther model to be loaded
        try:
            model_config = self.model_configs[model_name]
            # Adjust config by free memory
            adjust_config_by_memory(model_config)
            self.model_loadable.clear()
            result_message: ResultMessage = actor_system.ask(
                self.models[model_name],
                RequestMessage(action=ModelAction.LOAD, data={}),
                timeout=self.MODEL_REQUER_TIMEOUT)
        except Exception as e:
            logger.error("Load model %s error: %s", model_name, e)
            raise CoreNormalException(f"Load model {model_name} failed: {e}") from e
        finally:
            self.model_loadable.set()

        if result_message.result:
            self.loaded_models.append(model_name)
        else:
            raise ModelSchedulerException(result_message.error)

    async def _unload_model(self, model_name: str):
        """
        Unload model
        """
        if model_name not in self.loaded_models:
            logger.info("Model %s not loaded", model_name)
            return
        if model_name not in self.models:
            logger.error("Model %s not configured", model_name)
            raise InvalidArgException(f"Model {model_name} not configured")
        try:
            result_message: ResultMessage = actor_system.ask(
                    self.models[model_name],
                    RequestMessage(action=ModelAction.UNLOAD, data={}),
                    timeout=self.MODEL_REQUER_TIMEOUT)
        except Exception as e:
            logger.error("Unload model %s error: %s", model_name, e)
            raise CoreNormalException(f"Unload model {model_name} failed: {e}") from e

        if result_message.result:
            self.loaded_models.remove(model_name)
        else:
            raise ModelSchedulerException(result_message.error)

    def get_vram_usage(self) -> VramUsage:
        total, free, available = get_cuda_memory_info()
        if available:
            return VramUsage(total=total, free=free)
        return VramUsage(total=0, free=0)
