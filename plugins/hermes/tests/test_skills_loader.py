import logging
from pathlib import Path

from hermes import skills_loader


def test_skills_source_dir_exists():
    src = skills_loader._skills_source_dir()
    assert src.exists()
    assert src.is_dir()


def test_skills_source_dir_has_at_least_15_skills():
    src = skills_loader._skills_source_dir()
    skill_dirs = [
        child
        for child in src.iterdir()
        if child.is_dir() and (child / "SKILL.md").exists()
    ]
    assert len(skill_dirs) >= 15


def test_register_skills_registers_each_skill_dir():
    class FakeCtx:
        def __init__(self):
            self.registered = []

        def register_skill(self, name, path):
            self.registered.append((name, path))

    ctx = FakeCtx()
    skills_loader.register_skills(ctx)
    assert len(ctx.registered) >= 15
    names = [name for name, _ in ctx.registered]
    assert "miloco-perception-digest" in names
    for _name, path in ctx.registered:
        assert Path(path).exists()


def test_register_skills_warns_when_source_missing(monkeypatch, tmp_path, caplog):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(skills_loader, "_skills_source_dir", lambda: missing)

    class FakeCtx:
        def register_skill(self, name, path):
            raise AssertionError("should not register when source missing")

    with caplog.at_level(logging.WARNING):
        result = skills_loader.register_skills(FakeCtx())
    assert result == 0
    assert any("not found" in rec.message for rec in caplog.records)
