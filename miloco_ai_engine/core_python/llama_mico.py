# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
LLaMA-MICO Core Interface
Directly calls LLaMA-MICO C API using ctypes
"""

import ctypes
import json
from typing import List, Optional, Dict, Any, Union, Iterator
from miloco_ai_engine.schema.models_schema import ChatCompletionResponse, ChatCompletionChoice, ChatMessage, Role, FinishReason
import threading
import time
import contextlib
from miloco_ai_engine.utils.mico_content_util import MicoContentUtil
from miloco_ai_engine.utils.image_process import ImageProcess
from miloco_ai_engine.middleware.exceptions import CoreNormalException, InvalidArgException
from miloco_ai_engine.core_python.lib_manager import get_library
from miloco_ai_engine.config import config as c
import numpy as np

import logging
logger = logging.getLogger(__name__)

class LlamaMico:
    """LLaMA-MICO core interface class - Adapts llama-mico.h interface"""

    _HIGH_PROCESS_IMAGE_SIZE = (448, 448)
    _LOW_PROCESS_IMAGE_SIZE = (224, 224)
    _VIDEO_CONTINUOUS_FRAMES_NUM = 6
    _MAX_BYTE_BUFFER_SIZE = 4
    _DEFAULT_DECODED_TEXT = "@"

    def __init__(self):
        self.request_id_counter = 0
        self._counter_lock = threading.Lock()
        self.mico_content_util = MicoContentUtil()
        self._active_modal_buffers = {}  # Keep image buffers alive
        self._byte_buffers = {}  # Cache incomplete bytes, key is request_id

    def init(self, config: Dict[str, Any]) -> Optional[ctypes.c_void_p]:
        """
        Initialize LLaMA-MICO context
        """
        llama_mico_lib = get_library()

        config["log_file"] = str(c.LOG_FILE_NAME)
        config["log_level"] = c.LOGGING_CONFIG["log_level"].lower()

        config_json = json.dumps(config, ensure_ascii=False)
        handle_ptr = ctypes.c_void_p()
        # logger.info("config_json: %s", config_json)
        config_json_bytes = config_json.encode("utf-8")
        ret = llama_mico_lib.llama_mico_init(config_json_bytes, ctypes.byref(handle_ptr))

        if ret != 0:
            err = f"Initialization failed: {ret}"
            logger.error(err)
            raise CoreNormalException(err)

        handle = handle_ptr.value
        logger.info("LLaMA-MICO context initialized successfully, handle: %s", handle)
        return handle

    def cleanup(self, handle: ctypes.c_void_p):
        """
        Clean up resources
        """
        if not handle:
            logger.warning("Handle is None, skipping cleanup")
            return

        llama_mico_lib = get_library()
        ret = llama_mico_lib.llama_mico_free(handle)
        if ret != 0:
            err = f"Failed to free context: {ret}"
            logger.warning(err)
            raise CoreNormalException(err)
        logger.info("LLaMA-MICO context freed, handle: %d", handle)

    def _parse_content(
            self,
            content_ptr: ctypes.c_char_p,
            current_id: int) -> Union[str, List[Dict[str, Any]]]:
        """
        Parse LLaMA-MICO response content
        """
        res = ""
        if not content_ptr or not content_ptr.value:
            return res
        if current_id not in self._byte_buffers:
            self._byte_buffers[current_id] = b""

        self._byte_buffers[current_id] += content_ptr.value
        decoded_text = ""

        try:
            decoded_text = self._byte_buffers[current_id].decode("utf-8")
            self._byte_buffers[current_id] = b""
            return decoded_text
        except UnicodeDecodeError:
            pass

        # Try reverse splitting and decode character by character
        for i in range(len(self._byte_buffers[current_id]), 0, -1):
            try:
                decoded_text = self._byte_buffers[current_id][:i].decode("utf-8")
                self._byte_buffers[current_id] = self._byte_buffers[current_id][i:]
                return decoded_text
            except UnicodeDecodeError:
                continue

        # If remaining bytes still exceed UTF-8 decode threshold, discard directly
        if len(self._byte_buffers[current_id]) > self._MAX_BYTE_BUFFER_SIZE:
            logger.warning("Byte buffer exceeds 4 bytes and still cannot be UTF-8 decoded: %s", 
                           self._byte_buffers[current_id])
            try:
                decoded_text = self._byte_buffers[current_id].decode("utf-8", errors="replace")
            except UnicodeDecodeError:
                decoded_text = self._DEFAULT_DECODED_TEXT
            self._byte_buffers[current_id] = b""

        return decoded_text

    def _request_prompt(
            self, handle: ctypes.c_void_p,
            request_data: Dict[str, Any]) -> ChatCompletionResponse:
        """
        Process prompt request
        """
        if not handle:
            raise InvalidArgException("handle cannot be empty")

        current_id = int(request_data["id"].split("-")[-1])
        request_json = json.dumps(request_data, ensure_ascii=False)
        request_json_bytes = request_json.encode("utf-8")
        # Allocate output parameter pointers
        is_finished_ptr = ctypes.c_int32()
        content_ptr = ctypes.c_char_p()

        llama_mico_lib = get_library()
        ret = llama_mico_lib.llama_mico_request_prompt(
            handle, request_json_bytes, ctypes.byref(is_finished_ptr),
            ctypes.byref(content_ptr))

        content = self._parse_content(content_ptr, current_id)
        # todo: Process the ret code uniformly
        if ret == -1:
            err = f"Prompt request failed: {content}"
            logger.error(err)
            with self._counter_lock:
                self._active_modal_buffers.pop(current_id, None)
            raise CoreNormalException(err)

        is_finished = is_finished_ptr.value
        finish_reason = FinishReason.STOP if is_finished else None
        finish_reason = FinishReason.LENGTH if (is_finished and ret == -2) else finish_reason
        if finish_reason == FinishReason.LENGTH:
            logger.error("Generate token too long")

        response = ChatCompletionResponse(
            id=request_data.get("id", "local-chatcmpl-0"),
            created=int(time.time()),
            choices=[
                ChatCompletionChoice(index=0,
                                     delta=ChatMessage(role=Role.ASSISTANT,
                                                       content=content),
                                     finish_reason=finish_reason)
            ])

        # logger.debug(
        #     f"Prompt request processed successfully, is_finished: {is_finished}, content: {content}")
        with self._counter_lock:
            self._active_modal_buffers.pop(current_id, None)
        return response

    def _request_generate(
            self, handle: ctypes.c_void_p,
            request_data: Dict[str, Any]) -> ChatCompletionResponse:
        """
        Process generate request
        """
        if not handle:
            raise InvalidArgException("handle cannot be empty")

        request_json = json.dumps(request_data, ensure_ascii=False)
        request_json_bytes = request_json.encode("utf-8")

        # Allocate output parameter pointers
        is_finished_ptr = ctypes.c_int32()
        content_ptr = ctypes.c_char_p()

        llama_mico_lib = get_library()
        ret = llama_mico_lib.llama_mico_request_generate(
            handle, request_json_bytes, ctypes.byref(is_finished_ptr),
            ctypes.byref(content_ptr))

        current_id = int(request_data["id"].split("-")[-1])
        content = self._parse_content(content_ptr, current_id)
        # todo: Process the ret code uniformly
        if ret == -1:
            err = f"Generate request failed: {content}"
            logger.error(err)
            raise CoreNormalException(err)

        is_finished = is_finished_ptr.value

        # Construct response
        finish_reason = FinishReason.STOP if is_finished else None
        finish_reason = FinishReason.LENGTH if is_finished and ret == -2 else finish_reason
        if finish_reason == FinishReason.LENGTH:
            logger.error("Generate tokens too long")

        response = ChatCompletionResponse(
            id=request_data.get("id", "local-chatcmpl-0"),
            created=int(time.time()),
            choices=[
                ChatCompletionChoice(index=0,
                                     delta=ChatMessage(content=content),
                                     finish_reason=finish_reason)
            ])

        # Return response data
        # logger.debug(
        #     f"Generate request processed successfully, is_finished: {is_finished}, content: {content}")
        return response

    def chat_completion(
        self,
        handle: ctypes.c_void_p,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        priority: int = 0,
        temperature: float = -1.0,
        stream: bool = False
    ) -> Iterator[ChatCompletionResponse] | ChatCompletionResponse:
        """
        Chat completion interface - Simplified usage
        """
        if not handle:
            raise InvalidArgException("handle cannot be empty")

        if not messages:
            raise InvalidArgException("Message list cannot be empty")

        # Handle None values in message list
        for msg in messages:
            keys_to_remove = []
            for key, value in msg.items():
                if value is None:
                    keys_to_remove.append(key)
            for key in keys_to_remove:
                msg.pop(key, None)

        modal_bytes = []
        for msg in messages:
            content = msg.get("content", None)
            if content:
                msg["content"] = self.mico_content_util.process_multimodal_message(
                    content)
                # Convert base64 encoding to file address
                msg["content"], bytes_list = self.mico_content_util.mutilmodal_message_to_bytes(
                    msg["content"])
                for ide, bytes_item in enumerate(bytes_list):
                    # Default to JPEG for now
                    bytes_item = ImageProcess.center_crop_to_size(
                        bytes_item, self._HIGH_PROCESS_IMAGE_SIZE)  # Crop to high precision size
                    # Process frames that are not at the start or end of video segments
                    if (ide % self._VIDEO_CONTINUOUS_FRAMES_NUM != 0 and
                            ide % self._VIDEO_CONTINUOUS_FRAMES_NUM !=
                            self._VIDEO_CONTINUOUS_FRAMES_NUM - 1):
                        bytes_item = ImageProcess.resize_low_precision(
                            bytes_item,
                            self._LOW_PROCESS_IMAGE_SIZE)  # Compress to low precision size

                    modal_bytes.append(bytes_item)

        # Convert modal_bytes to C language memory address list char*
        address_list = []
        buffers = []
        for single_bytes in modal_bytes:
            arr = np.frombuffer(single_bytes, dtype=np.uint8).copy()
            address_list.append({str(int(arr.ctypes.data)): arr.nbytes})
            buffers.append(arr)

        with self._counter_lock:
            current_id = self.request_id_counter
            self.request_id_counter += 1
            self._active_modal_buffers[current_id] = buffers  # Keep memory alive during C++ runtime

        # ======================= request_data ======================= #
        request_data = {
            "id": f"local-chatcmpl-{current_id}",
            "messages": messages,
            "tools": tools,
            "stop": False,
            "modal_prts": address_list,
            "priority": priority,
            "temperature": temperature
        }
        # ======================= request_data ======================= #

        try:
            if stream:
                # Streaming output mode
                return self._stream_chat_completion(handle, request_data)
            else:
                # Non-streaming output mode
                return self._non_stream_chat_completion(handle, request_data)
        except Exception as e:
            with contextlib.suppress(Exception):
                self._request_generate(handle, {
                    "id": request_data["id"],
                    "stop": True
                })
            raise e
        finally:
            self._byte_buffers.pop(current_id, None)

    def _stream_chat_completion(
            self, handle: ctypes.c_void_p,
            request_data: Dict[str, Any]) -> Iterator[ChatCompletionResponse]:
        """
        Streaming chat completion
        """
        request_generate_data = {
            "id": request_data["id"],
            "stop": request_data["stop"]
        }
        # Accumulate content to detect tool calls
        accumulated_content = ""
        tool_use_detected = False
        tool_wait = False
        first = True

        while True:
            if first:
                response = self._request_prompt(handle, request_data)
                first = False
            else:
                response = self._request_generate(handle,
                                                  request_generate_data)

            response.object = "chat.completion.chunk"
            current_token = response.choices[0].delta.content
            accumulated_content += current_token

            tool_wait, tool_use_detected, accumulated_content, res = self.mico_content_util.process_tool_calls(
                tool_wait, tool_use_detected, accumulated_content)

            if isinstance(res, ChatCompletionResponse):
                response.choices[0].delta = res.choices[0].message
                response.choices[0].finish_reason = res.choices[
                    0].finish_reason
                yield response
            elif isinstance(res, str):
                response.choices[0].delta.content = res
                yield response

            if response.choices[0].finish_reason is not None:
                break
            # Add small delay to avoid too frequent requests
            time.sleep(0.001)

        # Exceeded generation length
        if response.choices[0].finish_reason is None or response.choices[0].finish_reason is FinishReason.LENGTH:
            if tool_use_detected:
                logger.warning("Tool call incomplete, request too long, returning empty response")

            yield response

        # Add stop signal for non-stop endings
        if response.choices[0].finish_reason is FinishReason.TOOL_CALL:
            logger.debug("Actively stopping seq %s", request_data["id"])
            with contextlib.suppress(Exception):
                self._request_generate(handle, {
                    "id": request_data["id"],
                    "stop": True
                })

    def _non_stream_chat_completion(
            self, handle: ctypes.c_void_p,
            request_data: Dict[str, Any]) -> ChatCompletionResponse:
        """
        Non-streaming chat completion
        """
        response = self._request_prompt(handle, request_data)
        response.object = "chat.completion"
        response.choices[0].message = response.choices[0].delta
        response.choices[0].delta = None

        request_generate_data = {
            "id": request_data["id"],
            "stop": request_data["stop"]
        }
        accumulated_content = ""
        tool_use_detected = False
        tool_wait = False

        while True:
            if response.choices[0].finish_reason is not None:
                break
            generate_response = self._request_generate(handle,
                                                       request_generate_data)
            response.choices[0].finish_reason = generate_response.choices[
                0].finish_reason
            current_token = generate_response.choices[0].delta.content
            accumulated_content += current_token

            tool_wait, tool_use_detected, accumulated_content, res = self.mico_content_util.process_tool_calls(
                tool_wait, tool_use_detected, accumulated_content)

            if isinstance(res, ChatCompletionResponse):
                response.choices[0].message.content += res.choices[
                    0].message.content
                response.choices[0].message.tool_calls = res.choices[
                    0].message.tool_calls
                response.choices[0].finish_reason = res.choices[
                    0].finish_reason
            elif isinstance(res, str):
                response.choices[0].message.content += res

            # Add small delay to avoid too frequent requests
            time.sleep(0.001)

        response.created = int(time.time())

        # Exceeded generation length
        if response.choices[0].finish_reason is None or response.choices[0].finish_reason is FinishReason.LENGTH:
            if tool_use_detected:
                logger.warning("Tool call incomplete, request too long, returning empty response")

        # Add stop signal for non-stop endings
        if response.choices[0].finish_reason is FinishReason.TOOL_CALL:
            logger.debug("Actively stopping seq %s", request_data["id"])
            with contextlib.suppress(Exception):
                self._request_generate(handle, {
                    "id": request_data["id"],
                    "stop": True
                })

        return response


# Global instance
llama_mico = LlamaMico()
