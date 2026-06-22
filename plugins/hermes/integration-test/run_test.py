#!/usr/bin/env python3
"""Integration test: verify Miloco Hermes plugin loads and bridge is reachable.

Test flow:
1. Check Hermes gateway is running
2. Check Miloco backend is running
3. Check plugin is loaded and enabled
4. Check bridge HTTP server is listening on :18789
5. Send a test webhook request to bridge (get_trace action)
6. Verify response format matches the fixed contract
7. Check hermes miloco CLI works
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request

HERMES_API = "http://localhost:8642"
BRIDGE_URL = "http://localhost:18789/miloco/webhook"

MAX_WAIT = 120
POLL_INTERVAL = 3


def wait_for(url: str, name: str, max_wait: int = MAX_WAIT) -> bool:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    print(f"[OK] {name} is up ({resp.status})")
                    return True
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)
    print(f"[FAIL] {name} not reachable after {max_wait}s")
    return False


def check_plugin_loaded() -> bool:
    result = subprocess.run(
        ["docker", "exec", "miloco-hermes-test", "hermes", "plugins", "list"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    print(f"[INFO] plugins list:\n{output}")
    if "miloco" in output and ("enabled" in output.lower() or "✓" in output):
        print("[OK] Miloco plugin is loaded and enabled")
        return True
    print("[FAIL] Miloco plugin not found or not enabled")
    return False


def check_bridge_health() -> bool:
    body = json.dumps({"action": "get_trace", "payload": {"runId": "nonexistent"}})
    result = subprocess.run(
        [
            "docker", "exec", "miloco-hermes-test",
            "curl", "-sf", "-X", "POST",
            "http://localhost:18789/miloco/webhook",
            "-H", "Content-Type: application/json",
            "-d", body,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        print(f"[FAIL] Bridge not reachable: {result.stderr}")
        return False
    try:
        data = json.loads(result.stdout)
        if data.get("code") == 0 and data.get("data", {}).get("status") == "unknown":
            print("[OK] Bridge responds with correct contract format")
            print(f"  Response: {json.dumps(data, ensure_ascii=False)}")
            return True
        print(f"[FAIL] Bridge response unexpected: {data}")
        return False
    except Exception as e:
        print(f"[FAIL] Bridge response parse error: {e}\n  Raw: {result.stdout}")
        return False


def check_backend_health() -> bool:
    result = subprocess.run(
        ["docker", "exec", "miloco-hermes-test", "curl", "-sf", "http://localhost:1810/health"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        print("[OK] Miloco backend is up")
        return True
    print(f"[FAIL] Miloco backend not reachable: {result.stderr}")
    return False


def check_hermes_health() -> bool:
    result = subprocess.run(
        ["docker", "exec", "miloco-hermes-test", "curl", "-sf", "http://localhost:8642/health"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        print("[OK] Hermes gateway is up")
        return True
    print(f"[FAIL] Hermes gateway not reachable: {result.stderr}")
    return False


def check_miloco_cli() -> bool:
    result = subprocess.run(
        ["docker", "exec", "miloco-hermes-test", "hermes", "miloco", "status"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    print(f"[INFO] hermes miloco status:\n{output}")
    if result.returncode == 0:
        print("[OK] hermes miloco status command works")
        return True
    print(f"[FAIL] hermes miloco status failed (rc={result.returncode})")
    return False


def main() -> int:
    print("=== Miloco Hermes Plugin Integration Test ===\n")

    checks = [
        ("Hermes gateway health", check_hermes_health),
        ("Miloco backend health", check_backend_health),
        ("Plugin loaded", check_plugin_loaded),
        ("Bridge contract", check_bridge_health),
        ("hermes miloco CLI", check_miloco_cli),
    ]

    results = []
    for name, check in checks:
        print(f"\n--- {name} ---")
        ok = check()
        results.append((name, ok))
        if not ok and name in ("Hermes gateway health", "Miloco backend health"):
            print("Critical service down, aborting remaining checks.")
            break

    print("\n=== Results ===")
    all_passed = True
    for name, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")
        if not ok:
            all_passed = False

    if all_passed:
        print("\nAll checks passed!")
        return 0
    print("\nSome checks failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
