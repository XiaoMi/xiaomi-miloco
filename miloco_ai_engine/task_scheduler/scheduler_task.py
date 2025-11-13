# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Task module for task execution."""
from dataclasses import dataclass
from typing import Any, Dict
from thespian.actors import ActorAddress, Actor
from miloco_ai_engine.schema.models_schema import ChatCompletionRequest, ChatCompletionResponse, ChatMessage, FinishReason, ChatCompletionChoice
import time
from enum import Enum
from miloco_ai_engine.schema.actor_message import actor_system, RequestMessage, CallbackMessage, TaskAction, ModelActorResponse
from miloco_ai_engine.config.config import SERVER_CONCURRENCY
from miloco_ai_engine.core_python.llama_mico import llama_mico
from miloco_ai_engine.middleware.exceptions import CoreNormalException, InvalidArgException
import asyncio
from concurrent.futures import Future

import logging
logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    """Task status enum"""
    PENDING = "pending"
    RUNNING = "running"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskInfo:
    """Task information"""
    task_id: str
    table: str
    status: TaskStatus
    error: str
    retry_count: int
    created_at: float
    request: ChatCompletionRequest
    priority: int
    respone_message: CallbackMessage


class Task(Actor):
    """Task state holder"""

    def __init__(self, task_id: str, table: str, handle, task_scheduler: ActorAddress,
                 request: ChatCompletionRequest,
                 respone_message: CallbackMessage, priority: int):
        super().__init__()
        self.task_info = TaskInfo(task_id=task_id,
                                  table=table,
                                  status=TaskStatus.PENDING,
                                  error=None,
                                  retry_count=0,
                                  created_at=time.time(),
                                  request=request,
                                  respone_message=respone_message,
                                  priority=priority)
        self.queue_timeout = SERVER_CONCURRENCY["queue_wait_timeout"]
        self.handle = handle
        self.llama_mico = llama_mico
        self.task_scheduler = task_scheduler

    def receiveMessage(self, msg: RequestMessage, sender: ActorAddress):
        action = msg.action
        logger.debug("scheduler_task ReceiveMessage:  %s", action)

        if action == TaskAction.START:
            event_loop: asyncio.AbstractEventLoop = msg.data
            future: Future = asyncio.run_coroutine_threadsafe(self._handle_start_task(), event_loop)
            self.send(sender, future)

        elif action == TaskAction.CANCEL:
            self.task_info.status = TaskStatus.CANCELLED

        elif action == TaskAction.GET_INFO:
            self.send(sender, self.task_info)

    async def _handle_start_task(self) -> bool:
        """
        Handle task start
        """
        logger.debug(
            "Task %s starting execution %s", self.task_info.table, self.task_info.task_id)
        if self.task_info.status != TaskStatus.PENDING:
            logger.error(
                "Task %s status error %s", self.task_info.table, self.task_info.status)
            return False

        # Retry not supported yet

        stream = self.task_info.request.stream
        wait_timeout = self.queue_timeout
        # Task wait timeout
        now = time.time()
        if now - self.task_info.created_at > wait_timeout:
            self.task_info.status = TaskStatus.CANCELLED
            self.task_info.error = f"Task {self.task_info.table} wait timeout"
            logger.error("%s", self.task_info.error)
            self._call_model_wrapper(
                self._generate_chat_fail_response(self.task_info.error))
            return False

        # Task start
        self.task_info.status = TaskStatus.RUNNING
        try:
            if not self.handle:
                raise InvalidArgException(
                    "Missing handle parameter in request")
            if stream:
                self._handle_chat_completion_stream()
            else:
                self._handle_chat_completion()
        except Exception as ex:  # pylint: disable=broad-except
            self.task_info.status = TaskStatus.FAILED
            self.task_info.error = str(ex)
            self._call_model_wrapper(self._generate_chat_fail_response(str(ex)))
            logger.error(
                "Task %s execution failed %s", self.task_info.table, self.task_info.error)
            return False

        self.task_info.status = TaskStatus.COMPLETED
        return True

    def _handle_chat_completion(self):
        """
        Non-streaming chat response
        """
        request = self._generate_chat_completion_request()
        response = self.llama_mico.chat_completion(self.handle, **request)
        if not response:
            raise CoreNormalException("Model empty response")
        self._call_model_wrapper(response)

    def _handle_chat_completion_stream(self):
        """
        Streaming chat response
        """
        request = self._generate_chat_completion_request()
        response = self.llama_mico.chat_completion(self.handle, **request)
        if not response:
            raise CoreNormalException("Model empty response")
        for chunk in response:
            self._call_model_wrapper(chunk)

    def _generate_chat_completion_request(self) -> Dict[str, Any]:
        """
        Generate chat request in dict format, filter parameters
        """
        res = {}
        res["priority"] = self.task_info.priority

        if self.task_info.request.stream:
            res["stream"] = self.task_info.request.stream
        else:
            res["stream"] = False

        if self.task_info.request.messages:
            res["messages"] = [
                msg.model_dump() for msg in self.task_info.request.messages
            ]
        else:
            res["messages"] = []

        if self.task_info.request.tools:
            res["tools"] = [
                tool.model_dump() for tool in self.task_info.request.tools
            ]
        else:
            res["tools"] = []

        if self.task_info.request.temperature:
            res["temperature"] = self.task_info.request.temperature
        else:
            res["temperature"] = -1.0

        return res

    def _generate_chat_fail_response(self,
                                     error: str) -> ChatCompletionResponse:
        """
        Chat failure response
        """
        object_type = "chat.completion.chunk" if self.task_info.request.stream else "chat.completion"
        return ChatCompletionResponse(object=object_type,
                                      created=int(time.time()),
                                      model=self.task_info.request.model,
                                      choices=[
                                          ChatCompletionChoice(
                                              index=0,
                                              message=ChatMessage(
                                                  role="assistant",
                                                  content=f"error: {error}"),
                                              delta=None,
                                              finish_reason=FinishReason.STOP)
                                      ])

    def _call_model_wrapper(self, response: ChatCompletionResponse):
        """
        Call model actor
        """
        callback_message = self.task_info.respone_message
        actor_system.tell(
            callback_message.callback_actor,
            RequestMessage(action=callback_message.callback_action,
                           data=ModelActorResponse(
                               request_id=callback_message.request_id,
                               response=response)))
