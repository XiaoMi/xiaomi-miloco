"""install-hermes.sh helper: 写 config.json::server.python_bin"""
import json
import sys
from pathlib import Path

home, py_bin = sys.argv[1], sys.argv[2]
p = Path(home) / "config.json"
try:
    cfg = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    cfg = {}
cfg.setdefault("server", {})["python_bin"] = py_bin
p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
