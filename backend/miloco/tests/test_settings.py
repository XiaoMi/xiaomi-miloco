# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""miloco.config.settings 单元测试。

- schema 自身合法性（jsonschema Draft 2020-12）
- Pydantic 模型与 settings.schema.json 字段对齐
- reset_settings() 后 env 覆盖生效
- 旧扁平 config.json 字段触发迁移异常
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from miloco.config import SETTINGS_SCHEMA, get_settings, reset_settings
from miloco.config.settings import MilocoSettings


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """每个用例独立 $MILOCO_HOME，避免读到用户真实 config.json。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    reset_settings()
    yield
    reset_settings()


def _load_schema() -> dict:
    return json.loads(SETTINGS_SCHEMA.read_text(encoding="utf-8"))


def test_schema_is_valid_draft_2020_12() -> None:
    schema = _load_schema()
    Draft202012Validator.check_schema(schema)


def _collect_schema_fields(schema: dict, prefix: str = "") -> dict[str, dict]:
    """把 schema.properties 展开成扁平的 {dotted.path: field_spec}。"""
    out: dict[str, dict] = {}
    for name, spec in schema.get("properties", {}).items():
        key = f"{prefix}{name}" if not prefix else f"{prefix}.{name}"
        out[key] = spec
        if spec.get("type") == "object" and "properties" in spec:
            out.update(_collect_schema_fields(spec, key))
    return out


def _collect_pydantic_fields(
    model_json_schema: dict, prefix: str = ""
) -> dict[str, dict]:
    """展开 Pydantic model_json_schema() 的 properties（处理 $ref/$defs）。"""
    defs = model_json_schema.get("$defs", {})

    def resolve(spec: dict) -> dict:
        if "$ref" in spec:
            name = spec["$ref"].rsplit("/", 1)[-1]
            return defs.get(name, {})
        return spec

    def walk(props: dict, prefix: str, out: dict[str, dict]) -> None:
        for name, spec in props.items():
            spec = resolve(spec)
            key = f"{prefix}{name}" if not prefix else f"{prefix}.{name}"
            out[key] = spec
            if spec.get("type") == "object" and "properties" in spec:
                walk(spec["properties"], key, out)

    out: dict[str, dict] = {}
    walk(model_json_schema.get("properties", {}), prefix, out)
    return out


def test_pydantic_matches_settings_schema() -> None:
    """schema.json 中声明的每个字段都必须在 MilocoSettings 中存在且类型/默认值一致。"""
    schema = _load_schema()
    schema_fields = _collect_schema_fields(schema)

    pydantic_schema = MilocoSettings.model_json_schema()
    pyd_fields = _collect_pydantic_fields(pydantic_schema)

    for path, spec in schema_fields.items():
        assert path in pyd_fields, (
            f"settings.schema.json 字段 {path} 未在 Pydantic 模型中出现"
        )
        pyd = pyd_fields[path]
        if "type" in spec and spec.get("type") != "object":
            assert pyd.get("type") == spec["type"], (
                f"{path} 类型不匹配：schema={spec['type']} vs pydantic={pyd.get('type')}"
            )
        if "default" in spec:
            assert pyd.get("default") == spec["default"], (
                f"{path} 默认值不匹配：schema={spec['default']!r} vs pydantic={pyd.get('default')!r}"
            )


def test_env_override_applies_after_reset(monkeypatch) -> None:
    s1 = get_settings()
    assert s1.server.url == "http://127.0.0.1:1810"

    monkeypatch.setenv("MILOCO_SERVER__URL", "http://example.com:9000")
    reset_settings()
    s2 = get_settings()
    assert s2.server.url == "http://example.com:9000"


def test_tier_u_dump_enable_default_false() -> None:
    """生产默认 perception.tier_u_dump_enable=false, 调试端点关闭。"""
    s = get_settings()
    assert s.perception.tier_u_dump_enable is False


def test_tier_u_dump_enable_env_override(monkeypatch) -> None:
    """支持环境变量 MILOCO_PERCEPTION__TIER_U_DUMP_ENABLE 切换。"""
    monkeypatch.setenv("MILOCO_PERCEPTION__TIER_U_DUMP_ENABLE", "true")
    reset_settings()
    assert get_settings().perception.tier_u_dump_enable is True


def test_notify_dedup_window_default() -> None:
    """通知去重窗口默认 60s。"""
    assert get_settings().notify.dedup_window_sec == 60.0


def test_notify_dedup_window_env_override(monkeypatch) -> None:
    """支持 env / config.json 覆盖去重窗口。"""
    monkeypatch.setenv("MILOCO_NOTIFY__DEDUP_WINDOW_SEC", "30")
    reset_settings()
    assert get_settings().notify.dedup_window_sec == 30.0


def test_notify_dedup_window_negative_loads_not_rejected(monkeypatch) -> None:
    """负值不被 pydantic 拒绝（避免误配一个字段崩整个 settings 加载）；
    <=0 交由 MessageDeduper 当作「关闭去重」，与 TS getNotifyDedupWindowMs 归零一致。"""
    monkeypatch.setenv("MILOCO_NOTIFY__DEDUP_WINDOW_SEC", "-5")
    reset_settings()
    assert get_settings().notify.dedup_window_sec == -5.0


def test_directory_paths_derive_from_miloco_home(tmp_path: Path) -> None:
    s = get_settings()
    assert s.directories.workspace_dir == tmp_path
    assert s.directories.image_dir == tmp_path / "images"
    assert s.directories.log_dir == tmp_path / "log"
    assert s.directories.miot_cache_dir == tmp_path / "miot_cache"


def test_model_defaults_align_with_schema() -> None:
    s = get_settings()
    assert s.model.omni.model == "xiaomi/mimo-v2.5"
    assert s.model.omni.base_url == "https://api.xiaomimimo.com/v1"
    assert s.model.omni.api_key == ""
    assert s.agent.webhook_url == "http://127.0.0.1:18789/miloco/webhook"
    assert s.agent.auth_bearer == ""
    assert s.server.python_bin == ""
    assert s.debug is False


# SSL 已废弃：backend 永远 HTTP，跨网加密走反代。原 ssl_enabled / ssl_certfile /
# ssl_keyfile computed_field 已删除，对应 5 个测试一并移除。tls_certfile / tls_keyfile
# 字段保留仅用于触发 utils/uvicorn.py 的 deprecation warning。


class TestServerUrlHostPortValidator:
    """server.url 与 server.host/port 一致性校验。"""

    def _make(self, **overrides) -> MilocoSettings:
        """构造 MilocoSettings 并触发 model_validator。"""
        base = {
            "server": {"url": "http://127.0.0.1:1810", "host": "127.0.0.1", "port": 1810},
        }
        for k, v in overrides.items():
            base["server"][k] = v
        return MilocoSettings(**base)

    def test_matching_config_no_warning(self, caplog):
        """默认配置完全一致，不触发 warning。"""
        import logging

        with caplog.at_level(logging.WARNING):
            self._make()
        assert not any("配置不一致" in r.message for r in caplog.records)

    def test_port_mismatch_warns(self, caplog):
        """url 端口与 server.port 不一致时触发 warning。"""
        import logging

        with caplog.at_level(logging.WARNING):
            self._make(port=1811)
        assert any("配置不一致" in r.message for r in caplog.records)

    def test_host_mismatch_warns(self, caplog):
        """url host 与 server.host 不一致时触发 warning。"""
        import logging

        with caplog.at_level(logging.WARNING):
            self._make(host="192.168.1.100")
        assert any("配置不一致" in r.message for r in caplog.records)

    def test_bind_all_only_checks_port(self, caplog):
        """host=0.0.0.0 时，url host 不同不告警，仅检查 port。"""
        import logging

        with caplog.at_level(logging.WARNING):
            self._make(host="0.0.0.0", url="http://192.168.1.50:1810")
        assert not any("配置不一致" in r.message for r in caplog.records)

    def test_bind_all_port_mismatch_warns(self, caplog):
        """host=0.0.0.0 但端口不一致时仍触发 warning。"""
        import logging

        with caplog.at_level(logging.WARNING):
            self._make(host="0.0.0.0", port=9999)
        assert any("配置不一致" in r.message for r in caplog.records)

    def test_localhost_normalized_no_warning(self, caplog):
        """localhost 与 127.0.0.1 视为等价，不触发 warning。"""
        import logging

        with caplog.at_level(logging.WARNING):
            self._make(url="http://localhost:1810", host="127.0.0.1")
        assert not any("配置不一致" in r.message for r in caplog.records)

    def test_default_port_http_no_warning(self, caplog):
        """http://host 不带端口时推断 80，与 port=80 匹配。"""
        import logging

        with caplog.at_level(logging.WARNING):
            self._make(url="http://127.0.0.1", port=80)
        assert not any("配置不一致" in r.message for r in caplog.records)
