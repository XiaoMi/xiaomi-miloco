# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
Common utilities.
"""

import asyncio
import hashlib
import importlib.metadata
import json
import random
import time
from asyncio import AbstractEventLoop
from collections import deque
from os import path
from typing import Dict, Optional
from urllib.parse import urlencode, urlparse

import yaml
from aiohttp import ClientSession, ClientTimeout

MIOT_ROOT_PATH: str = path.dirname(path.abspath(__file__))

#: 第三方服务的请求配额：host -> (窗口秒数, 窗口内最多请求数)。
#: 按 1 分钟一个桶滑动统计：窗口内允许把冷启动的数百个请求一次跑完（尖峰），
#: 但连续高频会在配额用完后被挡到最老的分钟桶过期。
_RATE_LIMIT_HOSTS: Dict[str, tuple[float, int]] = {
    "miot-spec.org": (600.0, 1200),
}
#: 配额桶的步长（秒）。窗口必须是步长的整数倍。
_RATE_WINDOW_STEP_SEC: float = 60.0
_rate_locks: Dict[str, asyncio.Lock] = {}
#: host -> deque[(分钟序号, 该分钟请求数)]，只保留窗口内的桶。
_rate_windows: Dict[str, deque[tuple[int, int]]] = {}


async def _acquire_rate_slot(url: str) -> None:
    """对受控 host 取一个请求配额；按分钟桶滑动，超出后等最老的桶过期。"""
    host = urlparse(url).hostname or ""
    cfg = _RATE_LIMIT_HOSTS.get(host)
    if not cfg:
        return
    window, limit = cfg
    step = _RATE_WINDOW_STEP_SEC
    slots = max(1, int(window // step))
    lock = _rate_locks.setdefault(host, asyncio.Lock())
    async with lock:
        buckets = _rate_windows.setdefault(host, deque())
        while True:
            now = time.monotonic()
            current = int(now // step)
            while buckets and buckets[0][0] <= current - slots:
                buckets.popleft()
            if sum(count for _, count in buckets) < limit:
                if buckets and buckets[-1][0] == current:
                    buckets[-1] = (current, buckets[-1][1] + 1)
                else:
                    buckets.append((current, 1))
                return
            # 等最老的桶滑出窗口（下一个分钟边界）
            wait = (buckets[0][0] + slots) * step - now + 0.01
            await asyncio.sleep(max(wait, step))


def gen_absolute_path(relative_path: str) -> str:
    """Generate an absolute path."""
    return path.join(MIOT_ROOT_PATH, relative_path)


def calc_group_id(uid: str, home_id: str) -> str:
    """Calculate the group ID based on a user ID and a home ID."""
    return hashlib.sha1(f"{uid}central_service{home_id}".encode("utf-8")).hexdigest()[
        :16
    ]


def load_json_file(json_file: str) -> Dict:
    """Load a JSON file."""
    with open(json_file, "r", encoding="utf-8") as f:
        return json.load(f)


def load_yaml_file(yaml_file: str) -> dict:
    """Load a YAML file."""
    with open(yaml_file, "r", encoding="utf-8") as f:
        return yaml.load(f, Loader=yaml.FullLoader)


def randomize_int(value: int, ratio: float) -> int:
    """Randomize an integer value."""
    return int(value * (1 - ratio + random.random() * 2 * ratio))


def randomize_float(value: float, ratio: float) -> float:
    """Randomize a float value."""
    return value * (1 - ratio + random.random() * 2 * ratio)


def get_pkg_version(package_name) -> Optional[str]:
    """Get the version of a package."""
    try:
        return importlib.metadata.version(package_name)
    except Exception:
        return None


async def http_get_async(
    url: str,
    params: Optional[Dict] = None,
    headers: Optional[Dict] = None,
    loop: Optional[AbstractEventLoop] = None,
) -> str:
    """Http get."""
    full_url = url
    if params:
        encoded_params = urlencode(params)
        full_url = f"{url}?{encoded_params}"

    async with ClientSession(loop=loop or asyncio.get_running_loop()) as session:
        async with session.get(
            url=full_url, headers=headers or {}, timeout=ClientTimeout(total=30)
        ) as response:
            if response.status != 200:
                raise ValueError(f"http get failed, {response.status}")
            return await response.text(encoding="utf-8")


async def http_get_json_async(
    url: str,
    params: Optional[Dict] = None,
    headers: Optional[Dict] = None,
    loop: Optional[AbstractEventLoop] = None,
) -> Dict:
    """Http get json."""
    full_url = url
    if params:
        encoded_params = urlencode(params)
        full_url = f"{url}?{encoded_params}"

    await _acquire_rate_slot(url)
    async with ClientSession(loop=loop) as session:
        async with session.get(
            url=full_url, headers=headers or {}, timeout=ClientTimeout(total=30)
        ) as response:
            if response.status != 200:
                raise ValueError(f"http get json failed, {response.status}")
            return await response.json(encoding="utf-8")


async def http_post_json_async(
    url: str,
    data: Dict,
    headers: Optional[Dict] = None,
    loop: Optional[AbstractEventLoop] = None,
) -> Dict:
    """Http post json."""
    await _acquire_rate_slot(url)
    async with ClientSession(loop=loop) as session:
        async with session.post(
            url=url,
            data=data,
            headers=headers or {},
            timeout=ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                raise ValueError(f"http post json failed, {response.status}")
            return await response.json(encoding="utf-8")
