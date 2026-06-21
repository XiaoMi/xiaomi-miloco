import subprocess
import time

__all__ = [
    "get_catalog",
]

_cached = {"text": "", "generated_at": 0.0}
_REGEN_THROTTLE_S = 5.0


def _run_cli_catalog():
    try:
        result = subprocess.run(
            ["miloco-cli", "device", "catalog"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout or None


def get_catalog():
    now = time.time()
    if now - _cached["generated_at"] < _REGEN_THROTTLE_S:
        return _cached["text"]
    text = _run_cli_catalog()
    if text is None:
        return _cached["text"]
    _cached["text"] = text
    _cached["generated_at"] = now
    return text


def _reset_cache():
    _cached["text"] = ""
    _cached["generated_at"] = 0.0
