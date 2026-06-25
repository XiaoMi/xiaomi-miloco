#!/usr/bin/env python3
"""
Miloco Device Catalog Generator

调用 miloco-cli device catalog 获取高频设备目录，供 agent prompt 注入使用。
失败时返回空字符串（agent 将走 device list fallback）。

用法:
    python3 miloco-catalog.py
"""

import json
import os
import subprocess
import sys
import time

MILOCO_HOME = os.environ.get("MILOCO_HOME", os.path.expanduser("~/.openclaw/miloco"))
CACHE_FILE = os.path.join(MILOCO_HOME, "catalog.cache.json")
CACHE_TTL = 5  # 秒


def run_cli_catalog(timeout: int = 10) -> str:
    """运行 miloco-cli device catalog 并返回输出"""
    try:
        result = subprocess.run(
            ["miloco-cli", "device", "catalog"],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "MILOCO_HOME": MILOCO_HOME}
        )
        if result.returncode != 0:
            print(f"miloco-cli exited {result.returncode}: {result.stderr[:200]}", file=sys.stderr)
            return ""
        return result.stdout.strip()
    except FileNotFoundError:
        print("miloco-cli not found", file=sys.stderr)
        return ""
    except subprocess.TimeoutExpired:
        print("miloco-cli timed out", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"miloco-cli error: {e}", file=sys.stderr)
        return ""


def get_catalog(use_cache: bool = True) -> str:
    """获取设备目录，优先用缓存"""
    if use_cache and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                cache = json.load(f)
            age = time.time() - cache.get("generated_at", 0)
            if age < CACHE_TTL:
                return cache.get("text", "")
        except Exception:
            pass

    text = run_cli_catalog()
    if text:
        # 缓存
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        cache = {"text": text, "generated_at": time.time()}
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, CACHE_FILE)
    else:
        # 返回旧缓存
        if use_cache and os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE) as f:
                    cache = json.load(f)
                return cache.get("text", "")
            except Exception:
                pass
    return text


if __name__ == "__main__":
    text = get_catalog()
    print(text)
