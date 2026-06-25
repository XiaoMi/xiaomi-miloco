#!/usr/bin/env python3
"""
Miloco Backend Service Manager for Hermes

管理 miloco-backend 的启停和健康检查。

用法:
    python3 miloco-service.py {start|stop|restart|status}
"""

import json
import os
import subprocess
import sys
import time

MILOCO_HOME = os.environ.get("MILOCO_HOME", os.path.expanduser("~/.openclaw/miloco"))
CONFIG_FILE = os.path.join(MILOCO_HOME, "config.json")
DEFAULT_BACKEND_URL = "http://127.0.0.1:1810"


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def get_backend_url() -> str:
    cfg = load_config()
    return cfg.get("server", {}).get("url", DEFAULT_BACKEND_URL)


def health_check() -> bool:
    """检查 backend 是否响应"""
    import urllib.request
    url = f"{get_backend_url()}/health"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_backend():
    """启动 miloco backend"""
    if health_check():
        print("miloco-backend already healthy")
        return

    os.makedirs(MILOCO_HOME, exist_ok=True)
    
    env = os.environ.copy()
    env["MILOCO_HOME"] = MILOCO_HOME
    
    try:
        subprocess.run(
            ["miloco-cli", "service", "start"],
            env=env,
            check=False,
        )
        time.sleep(3)
        
        # 验证启动
        for _ in range(10):
            if health_check():
                print("miloco-backend started successfully")
                return
            time.sleep(1)
        
        print("miloco-backend started but health check failed", file=sys.stderr)
    except FileNotFoundError:
        print("miloco-cli not found. Please install miloco first.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Failed to start miloco-backend: {e}", file=sys.stderr)
        sys.exit(1)


def stop_backend():
    """停止 miloco backend"""
    env = os.environ.copy()
    env["MILOCO_HOME"] = MILOCO_HOME
    try:
        subprocess.run(["miloco-cli", "service", "stop"], env=env, check=False)
    except FileNotFoundError:
        print("miloco-cli not found. Please stop backend manually.", file=sys.stderr)
        sys.exit(1)
    print("miloco-backend stopped")


def show_status():
    healthy = health_check()
    
    status = {
        "running": healthy,
        "healthy": healthy,
        "url": get_backend_url(),
        "home": MILOCO_HOME,
    }
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: miloco-service.py {start|stop|restart|status}")
        sys.exit(1)
    
    action = sys.argv[1]
    if action == "start":
        start_backend()
    elif action == "stop":
        stop_backend()
    elif action == "restart":
        stop_backend()
        time.sleep(1)
        start_backend()
    elif action == "status":
        show_status()
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)
