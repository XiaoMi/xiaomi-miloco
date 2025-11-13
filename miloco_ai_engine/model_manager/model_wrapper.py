# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Model wrapper module for managing model lifecycle and requests."""
from enum import Enum
import asyncio
import time
from thespian.actors import Actor, ActorAddress
from miloco_ai_engine.config.config_info import ModelConfig
from miloco_ai_engine.core_python.llama_mico import llama_mico
from typing import AsyncGenerator, Dict
from miloco_ai_engine.schema.models_schema import ChatCompletionRequest, ChatCompletionResponse, StreamErrorChunkMessage, StreamErrorChunk
from miloco_ai_engine.schema.actor_message import actor_system, ModelAction, ModelActorResponse, RequestMessage, ResultMessage, CallbackMessage
from miloco_ai_engine.task_scheduler.model_scheduler import TaskScheduler, TaskSchedulerAction
import uuid
import os
import logging
logger = logging.getLogger(__name__)


class ModelStatus(Enum):
    """Model loading status"""
    NOT_LOAD = "notload"  # Not loaded
    READY = "ready"  # Ready and idle
    RUNNING = "running"  # Running


class ModelWrapper(Actor):
    """Model instance"""

    MODEL_REQUER_TIMEOUT = 30.0

    def __init__(self, model_name: str, model_config: ModelConfig):
        super().__init__()
        self.model_name = model_name
        self.model_config = model_config
        self.handle = None  # Model handle
        self.task_scheduler = actor_system.createActor(lambda: TaskScheduler(
            self.model_name, self.model_config))  # Create task scheduler

        self.request_task: Dict[str, asyncio.Queue] = {}  # Request task queue
        # Request event loop (for cross-thread callbacks)
        self.request_loop: Dict[str, asyncio.AbstractEventLoop] = {}
        self.status = ModelStatus.NOT_LOAD
        self.last_used = 0
        self.use_count = 0

    # Directly interact with Actor, prefer using ask with return information
    def receiveMessage(self, msg: RequestMessage, sender: ActorAddress):
        logger.debug("model_wrapper ReceiveMessage:  %s", msg)

        if msg.action == ModelAction.LOAD:
            res = self._load()
            self.send(sender, res)  # Blocking load

        elif msg.action == ModelAction.UNLOAD:
            res = self._unload()
            self.send(sender, res)  # Blocking unload

        elif msg.action == ModelAction.CHAT:
            future = asyncio.Future()
            self.send(sender, future)
            asyncio.create_task(self._handle_chat(msg.data,
                                                  future))  # Non-blocking chat

        elif msg.action == ModelAction.STREAM_CHAT:
            future = asyncio.Future()
            self.send(sender, future)
            asyncio.create_task(self._handle_stream_chat(
                msg.data, future))  # Non-blocking stream_chat

        elif msg.action == ModelAction.GET_STATUS:
            self.send(sender, ResultMessage(True, "", {"status": self.status}))

        elif msg.action == ModelAction.CLEANUP:
            asyncio.create_task(self._cleanup())  # Non-blocking cleanup

        elif msg.action == ModelAction.CHAT_RESPONSE:
            self._handle_chat_response(msg.data)  # Write response data

        else:
            logger.error("Unknown model action: %s", msg.action)

    def _load(self) -> ResultMessage:
        """
        Load model
        """
        if self.status == ModelStatus.READY:
            logger.info("Model %s already ready", self.model_name)
            return ResultMessage(result=True, error="", data={})

        if not self._model_path_valid():
            return ResultMessage(result=False,
                                 error="Invalid model path, "
                                 "Please reset model config and Restart the ai_engine service",
                                 data={})

        try:
            config = self.model_config.to_dict()
            self.handle = llama_mico.init(config)
            if not self.handle:
                logger.error("LLaMA-MICO initialization %s failed", self.model_name)
                return ResultMessage(
                    result=False,
                    error=f"LLaMA-MICO initialization {self.model_name} failed",
                    data={})

            # Start task scheduler
            actor_system.tell(
                self.task_scheduler,
                RequestMessage(action=TaskSchedulerAction.START,
                               data=self.handle))

            self.status = ModelStatus.READY
            self.last_used = time.time()
            return ResultMessage(result=True, error="", data={})
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Model %s load failed: %s", self.model_name, e)
            return ResultMessage(result=False, error=str(e), data={})

    def _unload(self) -> ResultMessage:
        """
        Unload model
        """
        if self.status == ModelStatus.NOT_LOAD:
            return ResultMessage(result=True, error="", data={})

        if self.status == ModelStatus.RUNNING:
            logger.warning("Model %s is running, cannot unload",
                           self.model_name)
            return ResultMessage(result=False,
                                 error="Model is running, cannot unload",
                                 data={})

        if self.task_scheduler:
            actor_system.tell(self.task_scheduler,
                              RequestMessage(action=TaskSchedulerAction.STOP))

        if self.handle:
            llama_mico.cleanup(self.handle)
            self.handle = None

        self.status = ModelStatus.NOT_LOAD
        self.last_used = 0
        self.use_count = 0
        logger.info("Model %s unloaded", self.model_name)
        return ResultMessage(result=True, error="", data={})

    async def _handle_chat(self, data: ChatCompletionRequest,
                           future: asyncio.Future):
        """
        Non-streaming chat
        """
        if self.status == ModelStatus.NOT_LOAD:
            logger.error("Model %s not loaded", self.model_name)
            future.set_result(
                ResultMessage(result=False,
                              error=f"Model {self.model_name} not loaded",
                              data={}))
            return

        self.status = ModelStatus.RUNNING
        self.last_used = time.time()
        self.use_count += 1

        request_id = str(uuid.uuid4())
        data.max_tokens = self.model_config.context_per_seq

        self.request_task[request_id] = asyncio.Queue(maxsize=1)
        self.request_loop[request_id] = asyncio.get_running_loop()
        queue = self.request_task[request_id]

        actor_system.tell(
            self.task_scheduler,
            RequestMessage(action=TaskSchedulerAction.SUBMIT_TASK,
                           data=data,
                           call_back_message=CallbackMessage(
                               callback_actor=self.myAddress,
                               callback_action=ModelAction.CHAT_RESPONSE,
                               request_id=request_id)))
        try:
            response: ChatCompletionResponse = await asyncio.wait_for(queue.get(), timeout=self.MODEL_REQUER_TIMEOUT)
            future.set_result(ResultMessage(result=True, error="", data=response))
        except asyncio.TimeoutError:
            logger.error("Model execution timeout(%fs)",
                         self.MODEL_REQUER_TIMEOUT)
            future.set_result(
                ResultMessage(result=False,
                              error=f"Model execution timeout({self.MODEL_REQUER_TIMEOUT}s)",
                              data={}))
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Model execution error: %s", str(e))
            future.set_result(
                ResultMessage(result=False, error=str(e), data={}))
        finally:
            self.use_count -= 1
            if self.use_count <= 0:
                self.status = ModelStatus.READY
            self.request_task.pop(request_id, None)
            self.request_loop.pop(request_id, None)

    async def _handle_stream_chat(self, data: ChatCompletionRequest,
                                  future: asyncio.Future):
        """
        Streaming chat
        """
        if self.status == ModelStatus.NOT_LOAD:
            logger.error("Model %s not loaded", self.model_name)
            future.set_result(
                ResultMessage(result=False,
                              error=f"Model {self.model_name} not loaded",
                              data={}))
            return

        self.status = ModelStatus.RUNNING
        self.last_used = time.time()
        self.use_count += 1

        request_id = str(uuid.uuid4())
        data.max_tokens = self.model_config.context_per_seq

        self.request_task[request_id] = asyncio.Queue(maxsize=data.max_tokens)
        self.request_loop[request_id] = asyncio.get_running_loop()
        queue = self.request_task[request_id]

        actor_system.tell(
            self.task_scheduler,
            RequestMessage(action=TaskSchedulerAction.SUBMIT_TASK,
                           data=data,
                           call_back_message=CallbackMessage(
                               callback_actor=self.myAddress,
                               callback_action=ModelAction.CHAT_RESPONSE,
                               request_id=request_id)))

        # Convert queue to streaming response
        async def stream_response(
        ) -> AsyncGenerator[ChatCompletionResponse, None]:
            try:
                for _ in range(data.max_tokens):
                    response: ChatCompletionResponse = await asyncio.wait_for(
                        queue.get(), timeout=self.MODEL_REQUER_TIMEOUT)
                    yield response
                    if response.choices[0].finish_reason:
                        break
            except asyncio.TimeoutError as e:
                logger.error("Model %s stream chat failed: %s",
                             self.model_name, str(e))
                yield StreamErrorChunk(error=StreamErrorChunkMessage(
                    message=f"Model execution timeout({self.MODEL_REQUER_TIMEOUT}s)"))
            except Exception as e:  # pylint: disable=broad-except
                logger.error("Model %s stream chat failed: %s",
                             self.model_name, e)
                yield StreamErrorChunk(error=StreamErrorChunkMessage(
                    message=f"Model {self.model_name} stream chat failed: {e}")
                )
            finally:
                self.request_task.pop(request_id, None)
                self.request_loop.pop(request_id, None)

        try:
            agen_response = stream_response()
            future.set_result(
                ResultMessage(result=True, error="", data=agen_response))
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Model %s stream chat failed: %s", self.model_name, e)
            future.set_result(
                ResultMessage(result=False, error=str(e), data={}))
        finally:
            self.use_count -= 1
            if self.use_count <= 0:
                self.status = ModelStatus.READY
            agen_response.aclose()

    async def _cleanup(self) -> ResultMessage:
        # Cleanup model
        return ResultMessage(result=True, error="", data={})

    def _handle_chat_response(self, message: ModelActorResponse):
        """
        Handle chat response
        """
        queue = self.request_task.get(message.request_id, None)
        loop = self.request_loop.get(message.request_id, None)
        if queue and loop and loop.is_running():
            message.response.model = self.model_name
            try:
                loop.call_soon_threadsafe(queue.put_nowait, message.response)
            except Exception:  # pylint: disable=broad-except
                queue.put_nowait(message.response)
        elif queue and not loop:
            queue.put_nowait(message.response)

    def _model_path_valid(self) -> bool:
        """
        Check if model path is valid
        """
        if not self.model_config.model_path:
            logger.error("Model %s path not set", self.model_name)
            return False

        if not os.path.exists(self.model_config.model_path) or not os.path.isfile(self.model_config.model_path):
            logger.error("Model %s file not exists", self.model_config.model_path)
            return False
        if self.model_config.mmproj_path and not (
            os.path.exists(self.model_config.mmproj_path) and os.path.isfile(self.model_config.mmproj_path)):
            logger.error("Model %s mmproj file not exists", self.model_config.mmproj_path)
            return False

        return True
