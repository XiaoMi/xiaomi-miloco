# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
Common utilities.
"""
from asyncio import AbstractEventLoop
import asyncio
import importlib.metadata
import json
from os import path
import random
from typing import Dict, Optional
import hashlib
from urllib.parse import urlencode
from aiohttp import ClientSession, ClientTimeout
import yaml

MIOT_ROOT_PATH: str = path.dirname(path.abspath(__file__))


def gen_absolute_path(relative_path: str) -> str:
    """Generate an absolute path."""
    return path.join(MIOT_ROOT_PATH, relative_path)


def calc_group_id(uid: str, home_id: str) -> str:
    """Calculate the group ID based on a user ID and a home ID."""
    return hashlib.sha1(
        f"{uid}central_service{home_id}".encode("utf-8")).hexdigest()[:16]


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
    return int(value * (1 - ratio + random.random()*2*ratio))


def randomize_float(value: float, ratio: float) -> float:
    """Randomize a float value."""
    return value * (1 - ratio + random.random()*2*ratio)


def get_pkg_version(package_name) -> Optional[str]:
    """Get the version of a package."""
    try:
        return importlib.metadata.version(package_name)
    except Exception:  # pylint: disable=broad-exception-caught
        return None


async def http_get_async(
    url: str, params: Optional[Dict] = None, headers: Optional[Dict] = None,
    loop: Optional[AbstractEventLoop] = None
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

    async with ClientSession(loop=loop) as session:
        async with session.get(
            url=full_url, headers=headers or {}, timeout=ClientTimeout(total=30)
        ) as response:
            if response.status != 200:
                raise ValueError(f"http get json failed, {response.status}")
            return await response.json(encoding="utf-8")


async def http_post_json_async(
    url: str, data: Dict, headers: Optional[Dict] = None,
    loop: Optional[AbstractEventLoop] = None
) -> Dict:
    """Http post json."""
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
