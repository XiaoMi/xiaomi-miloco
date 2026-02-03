# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""LLM proxy module for handling large language model related operations."""

import logging
from abc import ABC, abstractmethod
from typing import AsyncGenerator, Optional

from openai import AsyncOpenAI, AsyncStream
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam

logger = logging.getLogger(__name__)


class LLMProxy(ABC):
    """Abstract base class for large language model proxy."""

    def __init__(self,
                 model_name: str):
        logger.info("LLMProxy init model_name: %s", model_name)
        self.model_name = model_name


    @abstractmethod
    async def async_call_llm(self, messages: list[ChatCompletionMessageParam],
                           tools: Optional[list[ChatCompletionToolParam]] = None,
                           priority: Optional[int] = None) -> dict[str, any]:
        """Async call LLM (non-streaming)."""
        pass

    @abstractmethod
    async def async_call_llm_stream(self, messages: list[ChatCompletionMessageParam],
                                  tools: Optional[list[ChatCompletionToolParam]] = None,
                                  priority: Optional[int] = None) -> AsyncGenerator[dict[str, any], None]:
        """Async call LLM (streaming)."""
        pass

    async def _handle_async_stream_response(self, stream: AsyncStream) -> AsyncGenerator[dict, None]:
        """
        Handle async stream response
        
        Args:
            stream: OpenAI async stream response object
            
        Yields:
            Dictionary format for each stream response chunk
        """
        try:
            async for chunk in stream:
                yield {
                    "success": True,
                    "chunk": chunk,
                }

        except (ConnectionError, TimeoutError, ValueError) as e:
            logger.error("Error in async stream response: %s", str(e))
            yield {
                "success": False,
                "error": str(e)
            }

    async def _handle_async_stream_error(self, error_msg: str) -> AsyncGenerator[dict, None]:
        """
        Handle async stream response error
        
        Args:
            error_msg: Error message
            
        Yields:
            Error response dictionary
        """
        yield {
            "success": False,
            "error": error_msg,
        }


class OpenAIProxy(LLMProxy):
    """OpenAI compatible LLM proxy implementation."""
    def __init__(self, base_url: str,
                 api_key: str,
                 model_name: str):
        super().__init__(model_name)
        self.base_url = base_url
        self.api_key = api_key

        self.async_client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )
        logger.info("LLM Proxy initialized with base_url: %s", self.base_url)

    def __str__(self) -> str:
        """Return user-friendly string representation of the object."""
        api_key_display = f"{self.api_key[:4]}...{self.api_key[-4:]}" if len(self.api_key) > 8 else "***"
        return f"OpenAIProxy(model_name={self.model_name}, base_url={self.base_url}, api_key={api_key_display})"

    def __repr__(self) -> str:
        """Return developer-friendly string representation of the object."""
        return self.__str__()

    async def async_call_llm(self, messages: list[ChatCompletionMessageParam],
                           tools: Optional[list[ChatCompletionToolParam]] = None,
                           priority: Optional[int] = 0) -> dict[str, any]:
        """
        Call vision language model (async version, non-streaming)

        Args:
            messages: Message list
            tools: Tool list
            priority: Task priority (0-10), passed to AI engine for scheduling

        Returns:
            Raw OpenAI format model response
        """
        try:
            logger.debug(
                "Async calling model: %s, stream: False, messages: %s, tools: %s, priority: %s",
                self.model_name, messages, tools, priority
            )
            extra_body = {"priority": priority}
            completion = await self.async_client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                stream=False,
                tools=tools,
                temperature=0,
                extra_body=extra_body,
            )
            logger.info("Async model call completed successfully")
            return {
                "success": True,
                "response": completion,
                "content": completion.choices[0].message.content if completion.choices else ""
            }

        except (ConnectionError, TimeoutError, ValueError, RuntimeError) as e:
            logger.error("Error calling async model: %s", str(e))
            return {
                "success": False,
                "error": str(e),
            }

    async def async_call_llm_stream(self, messages: list[ChatCompletionMessageParam],
                                  tools: Optional[list[ChatCompletionToolParam]] = None,
                                  priority: Optional[int] = 0 ) -> AsyncGenerator[dict[str, any], None]:
        """
        Call vision language model (async version, streaming)

        Args:
            messages: Message list
            tools: Tool list
            priority: Task priority (0-100), passed to AI engine for scheduling

        Returns:
            Async streaming iterator for model response
        """
        try:
            logger.debug(
                "Async calling model: %s, stream: True, messages: %s, tools: %s, priority: %s",
                self.model_name, messages, tools, priority
            )
            extra_body = {"priority": priority}
            completion: AsyncStream = await self.async_client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                stream=True,
                tools=tools,
                temperature=0,
                extra_body=extra_body,
            )
            logger.info("Async model stream call completed successfully, completion: %s", completion)
            async for chunk in self._handle_async_stream_response(completion):
                yield chunk

        except (ConnectionError, TimeoutError, ValueError, RuntimeError) as e:
            logger.error("Error calling async model stream: %s", str(e))
            async for chunk in self._handle_async_stream_error(str(e)):
                yield chunk


async def get_models_from_openai_compatible_api(base_url: str, api_key: str) -> dict[str, any]:
    """
    Get model list from OpenAI compatible API
    
    Args:
        base_url: Base URL of API server
        api_key: API access key
        
    Returns:
        Dictionary format result containing model list
    """
    try:
        logger.info("Getting models from OpenAI compatible API: %s, api_key: %s", base_url, api_key)

        async_client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url
        )

        models = await async_client.models.list()

        logger.info("Successfully retrieved %s models", len(models.data))

        return {
            "success": True,
            "models": models.model_dump(),
            "count": len(models.data)
        }

    except (ConnectionError, TimeoutError, ValueError, RuntimeError) as e:
        logger.error("Error getting models from %s: %s", base_url, str(e))
        return {
            "success": False,
            "error": str(e),
            "models": None,
            "count": 0
        }


