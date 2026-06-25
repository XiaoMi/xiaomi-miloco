from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
BRIDGE = ROOT / "plugins" / "hermes" / "scripts" / "miloco-bridge.py"


def load_bridge():
    spec = importlib.util.spec_from_file_location("miloco_hermes_bridge", BRIDGE)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load bridge module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HermesBridgeTests(unittest.TestCase):
    def test_bridge_auth_uses_agent_bearer(self):
        bridge = load_bridge()
        token = bridge.resolve_bridge_auth_token(
            {
                "server": {"token": "backend-token"},
                "agent": {"auth_bearer": "agent-token"},
            }
        )
        self.assertEqual(token, "agent-token")

    def test_injector_writes_to_configured_incoming_dir(self):
        bridge = load_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            injector = bridge.MessageInjector(
                base_url="http://127.0.0.1:18789",
                incoming_dir=tmp,
                platform="weixin",
                user_id="u1",
            )
            msg_id = injector.inject("hello", "system")
            files = list(Path(tmp).glob("miloco-bridge-*.json"))
            self.assertEqual(len(files), 1)
            self.assertIn(msg_id, files[0].name)
            payload = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["message"], "hello")
            self.assertEqual(payload["metadata"]["system_prompt_inject"], "system")

    def test_trace_done_meta_uses_backend_camel_case_fields(self):
        bridge = load_bridge()
        store = bridge.TraceStore()
        store.start_turn("run-1", query="q", trace_id="trace-1")
        store.finish_turn("run-1", success=True, duration_ms=12.5)
        meta = store.pop_done("run-1")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["durationMs"], 12.5)
        self.assertEqual(meta["errorCount"], 0)
        self.assertNotIn("duration_ms", meta)


if __name__ == "__main__":
    unittest.main()
