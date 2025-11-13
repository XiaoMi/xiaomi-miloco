# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
I18n.
"""
import asyncio
import logging
from pathlib import Path
from typing import Dict, Optional, Union, cast

import aiofiles
import yaml
from aiocache import Cache, cached, caches
from aiocache.base import BaseCache

from miot.const import SYSTEM_LANGUAGE_DEFAULT

_LOGGER = logging.getLogger(__name__)


class MIoTI18n:
    """MIoT Internationalization Translation.
    Translate by LLM, which does not guarantee the accuracy of the 
    translation. If there is a problem with the translation, please submit 
    the ISSUE feedback. After the review, we will modify it as soon as possible.
    """
    _main_loop: asyncio.AbstractEventLoop
    _lang: str

    def __init__(
        self, lang: Optional[str] = None, loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        self._main_loop = loop or asyncio.get_running_loop()
        self._lang = lang or SYSTEM_LANGUAGE_DEFAULT

    async def init_async(self) -> None:
        """Init."""
        _LOGGER.info("init i18n, %s", self._lang)

    async def update_lang_async(self, lang: str) -> None:
        """Update lang."""
        await cast(BaseCache, caches.get("default")).clear()
        self._lang = lang
        _LOGGER.debug("update lang: %s", lang)

    async def deinit_async(self) -> None:
        """Deinit."""
        await cast(BaseCache, caches.get("default")).clear()
        _LOGGER.info("deinit i18n")

    async def translate_async(
        self, domain: str, key: str, replace: Optional[Dict[str, str]] = None, default: Union[str, Dict, None] = None
    ) -> Union[str, Dict, None]:
        """Translate."""
        result = await self.__load_async(domain=domain)
        if not result:
            return default
        for item in key.split("."):
            if item not in result:
                return default
            result = result[item]
        if isinstance(result, str) and replace:
            for k, v in replace.items():
                result = result.replace("{{"+k+"}}", str(v))
        return result or default

    @cached(ttl=120, cache=Cache.MEMORY)
    async def __load_async(self, domain: str) -> Optional[Dict]:
        """Load."""
        file_path = Path(__file__).parent / "i18n" / self._lang / f"{domain}.yaml"
        if not file_path.exists():
            _LOGGER.warning("i18n file not exists, %s", file_path)
            return None
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            _LOGGER.info("load i18n file: %s", file_path)
            return yaml.safe_load(await f.read())
