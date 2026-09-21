"""ort_utils 的 CoreML/OpenVINO cache 与 EP 选择逻辑单测。

覆盖:内容寻址 hash、总量兜底清理(超标全清 / 阈值内保留 / once-guard 幂等)、
person router 检测器单例在 settings reset 后失效,以及 OpenVINO EP 的平台
判定、逃生开关、provider 选择与回退。均为纯逻辑,不建真实 session(不依赖
CoreML/OpenVINO EP / 真实模型推理)。
"""

from __future__ import annotations

import pytest
from miloco.perception.inference import ort_utils


def test_hash_model_file_is_content_addressed(tmp_path):
    a = tmp_path / "a.onnx"
    a.write_bytes(b"model-A-bytes")
    b = tmp_path / "b.onnx"
    b.write_bytes(b"model-B-bytes")
    c = tmp_path / "c.onnx"
    c.write_bytes(b"model-A-bytes")  # 与 a 同内容、异名

    ha = ort_utils._hash_model_file(str(a))
    assert len(ha) == 16
    assert ha == ort_utils._hash_model_file(str(a))  # 同文件稳定
    assert ha == ort_utils._hash_model_file(str(c))  # 同内容 → 同 hash(与文件名无关)
    assert ha != ort_utils._hash_model_file(str(b))  # 不同内容 → 不同 hash


@pytest.fixture
def iso_home(tmp_path, monkeypatch):
    """把 MILOCO_HOME 指到临时目录,workspace_dir / models_dir 随之落在 tmp。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    from miloco.config import reset_settings

    reset_settings()
    (tmp_path / "models").mkdir(exist_ok=True)
    yield tmp_path
    reset_settings()


def _cache_root(monkeypatch, cache_dirname: str):
    """取当前 settings 下的 cache 根目录,并把进程级 once-guard 复位以便本次 sweep 生效。

    cache_dirname 区分 CoreML / OpenVINO 两条独立缓存,各自有独立 once-guard。
    """
    from miloco.config import get_settings

    # 复位对应 cache 的 once-guard:dict 形态,按 cache_dirname 隔离
    ort_utils._cache_swept.pop(cache_dirname, None)
    dirs = get_settings().directories
    return dirs.models_dir, dirs.workspace_dir / cache_dirname


def test_sweep_clears_oversized_then_idempotent(iso_home, monkeypatch):
    models_dir, root = _cache_root(monkeypatch, ort_utils._COREML_CACHE_DIRNAME)
    # 基准:models 下 1MB 假 onnx → 阈值 = 3MB
    (models_dir / "det.onnx").write_bytes(b"x" * 1_000_000)
    (root / "hashA").mkdir(parents=True)
    (root / "hashA" / "blob.bin").write_bytes(b"y" * 5_000_000)  # 5MB > 3MB

    ort_utils._sweep_cache_if_oversized_once(ort_utils._COREML_CACHE_DIRNAME)
    # 超标 → 整目录清空重建(root 仍在但为空)
    assert root.is_dir()
    assert not any(p.is_file() for p in root.rglob("*"))

    # once-guard 幂等:再造 oversize,第二次调用不再清
    (root / "hashB").mkdir(parents=True)
    (root / "hashB" / "blob.bin").write_bytes(b"z" * 5_000_000)
    ort_utils._sweep_cache_if_oversized_once(ort_utils._COREML_CACHE_DIRNAME)
    assert any(p.is_file() for p in root.rglob("*"))


def test_sweep_keeps_within_threshold(iso_home, monkeypatch):
    models_dir, root = _cache_root(monkeypatch, ort_utils._COREML_CACHE_DIRNAME)
    (models_dir / "det.onnx").write_bytes(b"x" * 5_000_000)  # 基准 5MB → 阈值 15MB
    (root / "hashA").mkdir(parents=True)
    (root / "hashA" / "blob.bin").write_bytes(b"y" * 6_000_000)  # 6MB < 15MB

    ort_utils._sweep_cache_if_oversized_once(ort_utils._COREML_CACHE_DIRNAME)
    assert (root / "hashA" / "blob.bin").exists()  # 未超标 → 保留复用


def test_sweep_skips_when_base_unknown(iso_home, monkeypatch):
    """models 无 onnx → 算不出基准 → 不敢清(避免误删),即便 cache 很大。"""
    _, root = _cache_root(monkeypatch, ort_utils._COREML_CACHE_DIRNAME)
    (root / "hashA").mkdir(parents=True)
    (root / "hashA" / "blob.bin").write_bytes(b"y" * 9_000_000)

    ort_utils._sweep_cache_if_oversized_once(ort_utils._COREML_CACHE_DIRNAME)
    assert (root / "hashA" / "blob.bin").exists()


def test_sweep_openvino_independent_of_coreml(iso_home, monkeypatch):
    """OpenVINO cache 与 CoreML cache 各自独立 once-guard,互不干扰。"""
    models_dir, ov_root = _cache_root(monkeypatch, ort_utils._OPENVINO_CACHE_DIRNAME)
    # 让两个 cache 都超标
    (models_dir / "det.onnx").write_bytes(b"x" * 1_000_000)
    (ov_root / "ovHash").mkdir(parents=True)
    (ov_root / "ovHash" / "blob.bin").write_bytes(b"y" * 5_000_000)

    # 只清 OpenVINO 这条:CoreML 的 once-guard 不受影响(反之亦然)
    ort_utils._sweep_cache_if_oversized_once(ort_utils._OPENVINO_CACHE_DIRNAME)
    assert ov_root.is_dir()
    assert not any(p.is_file() for p in ov_root.rglob("*"))


def test_reset_hook_invalidates_detector_singleton(iso_home, monkeypatch):
    import miloco.perception.engine.identity.tracker.detector as detector_mod
    import miloco.person.router as router
    from miloco.config import reset_settings

    # stub 掉真 Detector,避免建真实 CoreML session;每次返回不同对象便于判定
    monkeypatch.setattr(detector_mod, "Detector", lambda **kw: object())
    router._reset_detector_singleton()

    d1 = router._load_detector()
    d2 = router._load_detector()
    assert d1 is d2  # 单例:进程内复用同一实例

    reset_settings()  # 触发 register_reset_hook 注册的 cache_clear
    d3 = router._load_detector()
    assert d3 is not d1  # reset 后单例失效,重新构造


# =============================================================================
# Linux x86_64 / OpenVINO EP 适配测试
# =============================================================================


def test_openvino_disabled_by_env_var(monkeypatch):
    """MILOCO_DISABLE_OPENVINO=1 时 _openvino_disabled() 返回 True。"""
    # 先清宿主环境残留(开发者可能在 shell profile 里设了逃生开关),
    # 否则"默认不禁用"断言会因无关环境变量假红。
    monkeypatch.delenv("MILOCO_DISABLE_OPENVINO", raising=False)
    assert ort_utils._openvino_disabled() is False  # 默认不禁用
    monkeypatch.setenv("MILOCO_DISABLE_OPENVINO", "1")
    assert ort_utils._openvino_disabled() is True
    monkeypatch.setenv("MILOCO_DISABLE_OPENVINO", "true")
    assert ort_utils._openvino_disabled() is True
    monkeypatch.setenv("MILOCO_DISABLE_OPENVINO", "0")
    assert ort_utils._openvino_disabled() is False


@pytest.fixture
def reloadable_ort_utils(monkeypatch):
    """允许用例 patch platform + reload 重算模块级常量,退出时无条件复原。

    monkeypatch 的 teardown 只还原 platform.machine/system 本身,不会重新
    reload 模块;中间 assert 失败时 ort_utils._IS_X86_LINUX 会永久停在
    patch 后的值,污染后续用例。本 fixture 把 undo + reload 放进 teardown,
    保证失败路径也走到。
    """
    import importlib

    yield
    monkeypatch.undo()
    importlib.reload(ort_utils)


def test_x86_linux_detection(reloadable_ort_utils, monkeypatch):
    """Linux + x86_64 时 _IS_X86_LINUX 为 True;其它平台为 False。"""
    import importlib
    import platform as _plat

    monkeypatch.setattr(_plat, "system", lambda: "Linux")
    monkeypatch.setattr(_plat, "machine", lambda: "x86_64")
    importlib.reload(ort_utils)
    assert ort_utils._IS_X86_LINUX is True

    monkeypatch.setattr(_plat, "machine", lambda: "AMD64")
    importlib.reload(ort_utils)
    assert ort_utils._IS_X86_LINUX is True  # AMD64 也是 x86_64 别名

    monkeypatch.setattr(_plat, "system", lambda: "Linux")
    monkeypatch.setattr(_plat, "machine", lambda: "arm64")
    importlib.reload(ort_utils)
    assert ort_utils._IS_X86_LINUX is False  # ARM Linux 不算

    monkeypatch.setattr(_plat, "system", lambda: "Darwin")
    monkeypatch.setattr(_plat, "machine", lambda: "x86_64")
    importlib.reload(ort_utils)
    assert ort_utils._IS_X86_LINUX is False  # Intel Mac 不算(Linux-only)


def test_make_session_prefers_openvino_on_x86_linux(monkeypatch, tmp_path):
    """Linux x86_64 + OpenVINO EP 可用时,make_session 优先走 OpenVINO。"""
    monkeypatch.setattr(ort_utils, "_IS_X86_LINUX", True)
    monkeypatch.setattr(ort_utils, "_IS_APPLE_SILICON", False)
    monkeypatch.setattr(
        ort_utils.ort,
        "get_available_providers",
        lambda: ["CPUExecutionProvider", "OpenVINOExecutionProvider"],
    )
    monkeypatch.delenv("MILOCO_DISABLE_OPENVINO", raising=False)
    monkeypatch.setattr(
        ort_utils, "_openvino_cache_dir", lambda p: str(tmp_path / "ov_cache")
    )

    captured = {}

    def _fake_session(*args, **kwargs):
        captured["providers"] = kwargs.get(
            "providers", args[2] if len(args) > 2 else []
        )
        return object()

    monkeypatch.setattr(ort_utils.ort, "InferenceSession", _fake_session)

    model = tmp_path / "det.onnx"
    model.write_bytes(b"dummy")
    ort_utils.make_session(str(model))

    providers = captured["providers"]
    # OpenVINO 排第一、CPU 兜底排第二
    assert providers[0][0] == "OpenVINOExecutionProvider"
    assert providers[1] == "CPUExecutionProvider"
    ov_opts = providers[0][1]
    assert ov_opts["device_type"] == "CPU"  # 默认 use_gpu=False → CPU
    # 线程数对齐:load_config 里 INFERENCE_NUM_THREADS 应等于默认 4
    import json as _json

    assert _json.loads(ov_opts["load_config"]) == {
        "CPU": {"INFERENCE_NUM_THREADS": "4"}
    }
    # cache_dir 是优化项(准备失败时可能缺),只校验存在性、不校验具体路径
    assert "cache_dir" in ov_opts


def test_make_session_openvino_device_type_follows_use_gpu(monkeypatch, tmp_path):
    """use_gpu=True 且无 CUDA EP 时,OpenVINO 的 device_type 改 AUTO(让核显自吃)。"""
    monkeypatch.setattr(ort_utils, "_IS_X86_LINUX", True)
    monkeypatch.setattr(ort_utils, "_IS_APPLE_SILICON", False)
    monkeypatch.setattr(
        ort_utils.ort,
        "get_available_providers",
        lambda: ["CPUExecutionProvider", "OpenVINOExecutionProvider"],
    )
    monkeypatch.delenv("MILOCO_DISABLE_OPENVINO", raising=False)
    monkeypatch.setattr(
        ort_utils, "_openvino_cache_dir", lambda p: str(tmp_path / "ov_cache")
    )

    captured = {}

    def _fake_session(*args, **kwargs):
        captured["providers"] = kwargs.get(
            "providers", args[2] if len(args) > 2 else []
        )
        return object()

    monkeypatch.setattr(ort_utils.ort, "InferenceSession", _fake_session)

    model = tmp_path / "det.onnx"
    model.write_bytes(b"dummy")
    ort_utils.make_session(str(model), use_gpu=True)

    ov_opts = captured["providers"][0][1]
    assert ov_opts["device_type"] == "AUTO"  # use_gpu=True → AUTO


def test_make_session_falls_back_to_cpu_when_openvino_missing(monkeypatch, tmp_path):
    """Linux x86_64 但 OpenVINO EP 不可用时,回退纯 CPU EP。"""
    monkeypatch.setattr(ort_utils, "_IS_X86_LINUX", True)
    monkeypatch.setattr(ort_utils, "_IS_APPLE_SILICON", False)
    monkeypatch.setattr(
        ort_utils.ort,
        "get_available_providers",
        lambda: ["CPUExecutionProvider"],  # 无 OpenVINO
    )
    monkeypatch.delenv("MILOCO_DISABLE_OPENVINO", raising=False)

    captured = {}

    def _fake_session(*args, **kwargs):
        captured["providers"] = kwargs.get(
            "providers", args[2] if len(args) > 2 else []
        )
        return object()

    monkeypatch.setattr(ort_utils.ort, "InferenceSession", _fake_session)

    model = tmp_path / "det.onnx"
    model.write_bytes(b"dummy")
    ort_utils.make_session(str(model))

    assert captured["providers"] == ["CPUExecutionProvider"]


def test_make_session_falls_back_to_cpu_when_openvino_disabled(monkeypatch, tmp_path):
    """环境变量禁用 OpenVINO 时,即使 x86 Linux + OpenVINO 可用也走 CPU EP。"""
    monkeypatch.setattr(ort_utils, "_IS_X86_LINUX", True)
    monkeypatch.setattr(ort_utils, "_IS_APPLE_SILICON", False)
    monkeypatch.setattr(
        ort_utils.ort,
        "get_available_providers",
        lambda: ["CPUExecutionProvider", "OpenVINOExecutionProvider"],
    )
    monkeypatch.setenv("MILOCO_DISABLE_OPENVINO", "1")

    captured = {}

    def _fake_session(*args, **kwargs):
        captured["providers"] = kwargs.get(
            "providers", args[2] if len(args) > 2 else []
        )
        return object()

    monkeypatch.setattr(ort_utils.ort, "InferenceSession", _fake_session)

    model = tmp_path / "det.onnx"
    model.write_bytes(b"dummy")
    ort_utils.make_session(str(model))

    assert captured["providers"] == ["CPUExecutionProvider"]
