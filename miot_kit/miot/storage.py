# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
MIoT storage and certificate management.
"""
import os
import asyncio
import json
import shutil
import traceback
import hashlib
from enum import Enum, auto
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
import logging

# pylint: disable=relative-beyond-top-level

_LOGGER = logging.getLogger(__name__)


class MIoTStorageType(Enum):
    LOAD = auto()
    LOAD_FILE = auto()
    SAVE = auto()
    SAVE_FILE = auto()
    DEL = auto()
    DEL_FILE = auto()
    CLEAR = auto()


class MIoTStorage:
    """File management.
    """
    _main_loop: asyncio.AbstractEventLoop
    _file_future: Dict[str, Tuple[MIoTStorageType, asyncio.Future]]

    _root_path: str

    def __init__(
        self, root_path: str, loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        """Initialize with a root path."""
        self._main_loop = loop or asyncio.get_running_loop()
        self._file_future = {}

        self._root_path = os.path.abspath(root_path)
        os.makedirs(self._root_path, exist_ok=True)

        _LOGGER.debug("root path, %s", self._root_path)

    def __get_full_path(self, domain: str, name: str, suffix: str) -> str:
        return os.path.join(self._root_path, domain, f"{name}.{suffix}")

    def __add_file_future(
        self, key: str, op_type: MIoTStorageType, fut: asyncio.Future
    ) -> None:
        def fut_done_callback(fut: asyncio.Future):
            del fut
            self._file_future.pop(key, None)

        fut.add_done_callback(fut_done_callback)
        self._file_future[key] = op_type, fut

    def __load(
        self, full_path: str, type_: type = bytes, with_hash_check: bool = True
    ) -> Union[bytes, str, Dict, List, None]:
        if not os.path.exists(full_path):
            _LOGGER.debug("load error, file does not exist, %s", full_path)
            return None
        if not os.access(full_path, os.R_OK):
            _LOGGER.error("load error, file not readable, %s", full_path)
            return None
        try:
            with open(full_path, "rb") as r_file:
                r_data: bytes = r_file.read()
                if r_data is None:
                    _LOGGER.error("load error, empty file, %s", full_path)
                    return None
                data_bytes: bytes
                # Hash check
                if with_hash_check:
                    if len(r_data) <= 32:
                        return None
                    data_bytes = r_data[:-32]
                    hash_value = r_data[-32:]
                    if hashlib.sha256(data_bytes).digest() != hash_value:
                        _LOGGER.error("load error, hash check failed, %s", full_path)
                        return None
                else:
                    data_bytes = r_data
                if type_ == bytes:
                    return data_bytes
                if type_ == str:
                    return str(data_bytes, "utf-8")
                if type_ in [Dict, List, dict, list]:
                    return json.loads(data_bytes)
                _LOGGER.error("load error, unsupported data type, %s", type_.__name__)
                return None
        except (OSError, TypeError) as e:
            _LOGGER.error("load error, %s, %s", e, traceback.format_exc())
            return None

    def load(self, domain: str, name: str, type_: type = bytes) -> Union[bytes, str, Dict, List, None]:
        """Load data from file."""
        full_path = self.__get_full_path(domain=domain, name=name, suffix=type_.__name__)
        return self.__load(full_path=full_path, type_=type_)

    async def load_async(self, domain: str, name: str, type_: type = bytes) -> Union[bytes, str, Dict, List, None]:
        """Async load data from file."""
        full_path = self.__get_full_path(domain=domain, name=name, suffix=type_.__name__)
        if full_path in self._file_future:
            # Waiting for the last task to be completed
            op_type, fut = self._file_future[full_path]
            if op_type == MIoTStorageType.LOAD:
                if not fut.done():
                    return await fut
            else:
                await fut
        fut = self._main_loop.run_in_executor(None, self.__load, full_path, type_)
        if not fut.done():
            self.__add_file_future(full_path, MIoTStorageType.LOAD, fut)
        return await fut

    def __save(
        self, full_path: str, data: Union[bytes, str, Dict, List, None], cover: bool = True, with_hash: bool = True
    ) -> bool:
        if data is None:
            _LOGGER.error("save error, save data is None")
            return False
        if os.path.exists(full_path):
            if not cover:
                _LOGGER.error("save error, file exists, cover is False")
                return False
            if not os.access(full_path, os.W_OK):
                _LOGGER.error("save error, file not writeable, %s", full_path)
                return False
        else:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
        try:
            w_bytes: bytes
            if isinstance(data, bytes):
                w_bytes = data
            elif isinstance(data, str):
                w_bytes = data.encode("utf-8")
            elif isinstance(data, (Dict, List, dict, list)):
                w_bytes = json.dumps(data).encode("utf-8")
            else:
                _LOGGER.error("save error, unsupported data type, %s", type(data).__name__)
                return False
            with open(full_path, "wb") as w_file:
                w_file.write(w_bytes)
                if with_hash:
                    w_file.write(hashlib.sha256(w_bytes).digest())
            return True
        except (OSError, TypeError) as e:
            _LOGGER.error("save error, %s, %s", e, traceback.format_exc())
            return False

    def save(self, domain: str, name: str, data: Union[bytes, str, Dict, List, None]) -> bool:
        """Save data to file."""
        full_path = self.__get_full_path(domain=domain, name=name, suffix=type(data).__name__)
        return self.__save(full_path=full_path, data=data)

    async def save_async(self, domain: str, name: str, data: Union[bytes, str, Dict, List, None]) -> bool:
        """Async save data to file."""
        full_path = self.__get_full_path(domain=domain, name=name, suffix=type(data).__name__)
        if full_path in self._file_future:
            # Waiting for the last task to be completed
            fut = self._file_future[full_path][1]
            await fut
        fut = self._main_loop.run_in_executor(None, self.__save, full_path, data)
        if not fut.done():
            self.__add_file_future(full_path, MIoTStorageType.SAVE, fut)
        return await fut

    def __remove(self, full_path: str) -> bool:
        item = Path(full_path)
        if item.is_file() or item.is_symlink():
            item.unlink()
        return True

    def remove(self, domain: str, name: str, type_: type) -> bool:
        """Remove file."""
        full_path = self.__get_full_path(domain=domain, name=name, suffix=type_.__name__)
        return self.__remove(full_path=full_path)

    async def remove_async(self, domain: str, name: str, type_: type) -> bool:
        """Async remove file."""
        full_path = self.__get_full_path(domain=domain, name=name, suffix=type_.__name__)
        if full_path in self._file_future:
            # Waiting for the last task to be completed
            op_type, fut = self._file_future[full_path]
            if op_type == MIoTStorageType.DEL:
                if not fut.done():
                    return await fut
            else:
                await fut
        fut = self._main_loop.run_in_executor(None, self.__remove, full_path)
        if not fut.done():
            self.__add_file_future(full_path, MIoTStorageType.DEL, fut)
        return await fut

    def __remove_domain(self, full_path: str) -> bool:
        path_obj = Path(full_path)
        if path_obj.exists():
            # Recursive deletion
            shutil.rmtree(path_obj)
        return True

    def remove_domain(self, domain: str) -> bool:
        """Remove domain."""
        full_path = os.path.join(self._root_path, domain)
        return self.__remove_domain(full_path=full_path)

    async def remove_domain_async(self, domain: str) -> bool:
        """Async remove domain."""
        full_path = os.path.join(self._root_path, domain)
        if full_path in self._file_future:
            # Waiting for the last task to be completed
            op_type, fut = self._file_future[full_path]
            if op_type == MIoTStorageType.DEL:
                if not fut.done():
                    return await fut
            else:
                await fut
        # Waiting domain tasks finish
        for path, value in self._file_future.items():
            if path.startswith(full_path):
                await value[1]
        fut = self._main_loop.run_in_executor(None, self.__remove_domain, full_path)
        if not fut.done():
            self.__add_file_future(full_path, MIoTStorageType.DEL, fut)
        return await fut

    def get_names(self, domain: str, type_: type) -> List[str]:
        """Get names of files in domain."""
        path: str = os.path.join(self._root_path, domain)
        type_str = f".{type_.__name__}"
        names: List[str] = []
        for item in Path(path).glob(f"*{type_str}"):
            if not item.is_file() and not item.is_symlink():
                continue
            names.append(item.name.replace(type_str, ""))
        return names

    def file_exists(self, domain: str, name_with_suffix: str) -> bool:
        """Check if file exists."""
        return os.path.exists(os.path.join(self._root_path, domain, name_with_suffix))

    def save_file(self, domain: str, name_with_suffix: str, data: bytes) -> bool:
        """Save file."""
        if not isinstance(data, bytes):
            _LOGGER.error("save file error, file must be bytes")
            return False
        full_path = os.path.join(self._root_path, domain, name_with_suffix)
        return self.__save(full_path=full_path, data=data,  with_hash=False)

    async def save_file_async(self, domain: str, name_with_suffix: str, data: bytes) -> bool:
        """Async save file."""
        if not isinstance(data, bytes):
            _LOGGER.error("save file error, file must be bytes")
            return False
        full_path = os.path.join(self._root_path, domain, name_with_suffix)
        if full_path in self._file_future:
            # Waiting for the last task to be completed
            fut = self._file_future[full_path][1]
            await fut
        fut = self._main_loop.run_in_executor(None, self.__save, full_path, data, True, False)
        if not fut.done():
            self.__add_file_future(full_path, MIoTStorageType.SAVE_FILE, fut)
        return await fut

    def load_file(self, domain: str, name_with_suffix: str) -> Optional[bytes]:
        """Load file."""
        full_path = os.path.join(self._root_path, domain, name_with_suffix)
        return self.__load(full_path=full_path, type_=bytes, with_hash_check=False)  # type: ignore

    async def load_file_async(self, domain: str, name_with_suffix: str) -> Optional[bytes]:
        """Async load file."""
        full_path = os.path.join(self._root_path, domain, name_with_suffix)
        if full_path in self._file_future:
            # Waiting for the last task to be completed
            op_type, fut = self._file_future[full_path]
            if op_type == MIoTStorageType.LOAD_FILE:
                if not fut.done():
                    return await fut
            else:
                await fut
        fut = self._main_loop.run_in_executor(None, self.__load, full_path, bytes, False)
        if not fut.done():
            self.__add_file_future(full_path, MIoTStorageType.LOAD_FILE, fut)
        return await fut  # type: ignore

    def remove_file(self, domain: str, name_with_suffix: str) -> bool:
        """Remove file."""
        full_path = os.path.join(self._root_path, domain, name_with_suffix)
        return self.__remove(full_path=full_path)

    async def remove_file_async(self, domain: str, name_with_suffix: str) -> bool:
        """Async remove file."""
        full_path = os.path.join(self._root_path, domain, name_with_suffix)
        if full_path in self._file_future:
            # Waiting for the last task to be completed
            op_type, fut = self._file_future[full_path]
            if op_type == MIoTStorageType.DEL_FILE:
                if not fut.done():
                    return await fut
            else:
                await fut
        fut = self._main_loop.run_in_executor(None, self.__remove, full_path)
        if not fut.done():
            self.__add_file_future(full_path, MIoTStorageType.DEL_FILE, fut)
        return await fut

    def clear(self) -> bool:
        """Clear all data."""
        root_path = Path(self._root_path)
        for item in root_path.iterdir():
            if item.is_file() or item.is_symlink():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item)
        return True

    async def clear_async(self) -> bool:
        """Async clear all data."""
        if self._root_path in self._file_future:
            op_type, fut = self._file_future[self._root_path]
            if op_type == MIoTStorageType.CLEAR and not fut.done():
                return await fut
        # Waiting all future resolve
        for value in self._file_future.values():
            await value[1]

        fut = self._main_loop.run_in_executor(None, self.clear)
        if not fut.done():
            self.__add_file_future(self._root_path, MIoTStorageType.CLEAR, fut)
        return await fut

    def gen_storage_path(self, domain: Optional[str] = None, name_with_suffix: Optional[str] = None) -> str:
        """Generate file path."""
        result = self._root_path
        if domain:
            result = os.path.join(result, domain)
            if name_with_suffix:
                result = os.path.join(result, name_with_suffix)
        return result
