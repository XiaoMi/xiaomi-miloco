from miloco.observability import debug as debug_mod


def _reset(monkeypatch, tmp_path):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    debug_mod._reset_cache_for_tests()


def test_disabled_when_flag_missing(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    assert debug_mod.is_debug_enabled() is False


def test_enabled_when_flag_present(tmp_path, monkeypatch):
    (tmp_path / ".debug_observability").write_text("")
    _reset(monkeypatch, tmp_path)
    assert debug_mod.is_debug_enabled() is True


def test_file_flag_cached_after_first_call(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    assert debug_mod.is_debug_enabled() is False
    (tmp_path / ".debug_observability").write_text("")
    assert debug_mod.is_debug_enabled() is False  # 缓存生效


def test_runtime_override_true_wins_over_missing_file(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    debug_mod.set_runtime_override(True)
    assert debug_mod.is_debug_enabled() is True


def test_runtime_override_false_wins_over_present_file(tmp_path, monkeypatch):
    (tmp_path / ".debug_observability").write_text("")
    _reset(monkeypatch, tmp_path)
    debug_mod.set_runtime_override(False)
    assert debug_mod.is_debug_enabled() is False


def test_get_state_runtime_source(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    debug_mod.set_runtime_override(True)
    state = debug_mod.get_state()
    assert state["enabled"] is True
    assert state["source"] == "runtime"
    assert state["runtime_override"] is True
    assert state["file_flag_present"] is True  # on 顺带创建文件


def test_get_state_file_source(tmp_path, monkeypatch):
    (tmp_path / ".debug_observability").write_text("")
    _reset(monkeypatch, tmp_path)
    state = debug_mod.get_state()
    assert state["source"] == "file"
    assert state["file_flag_present"] is True
    assert state["runtime_override"] is None


def test_get_state_default_source(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    state = debug_mod.get_state()
    assert state["enabled"] is False
    assert state["source"] == "default"


def test_set_override_true_creates_file(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    assert not (tmp_path / ".debug_observability").exists()
    debug_mod.set_runtime_override(True)
    assert (tmp_path / ".debug_observability").exists()


def test_set_override_false_removes_file(tmp_path, monkeypatch):
    (tmp_path / ".debug_observability").write_text("")
    _reset(monkeypatch, tmp_path)
    debug_mod.set_runtime_override(False)
    assert not (tmp_path / ".debug_observability").exists()


def test_set_override_false_when_file_absent_is_noop(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    debug_mod.set_runtime_override(False)  # missing_ok,不抛
    assert not (tmp_path / ".debug_observability").exists()


def test_debug_on_survives_process_restart(tmp_path, monkeypatch):
    """on 后清掉 runtime override + cache(模拟重启),应仍 enabled。"""
    _reset(monkeypatch, tmp_path)
    debug_mod.set_runtime_override(True)
    debug_mod._reset_cache_for_tests()
    assert debug_mod.is_debug_enabled() is True


def test_debug_off_survives_process_restart(tmp_path, monkeypatch):
    """off 后清掉 runtime override + cache(模拟重启),应仍 disabled。"""
    (tmp_path / ".debug_observability").write_text("")
    _reset(monkeypatch, tmp_path)
    debug_mod.set_runtime_override(False)
    debug_mod._reset_cache_for_tests()
    assert debug_mod.is_debug_enabled() is False


