# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Exercise collection-time isolation in fresh pytest processes with hostile inputs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def isolated_checkout(tmp_path_factory):
    """Use real application code, but never write a .env into the developer checkout."""
    root = tmp_path_factory.mktemp("isolation-checkout")
    package = Path(__file__).resolve().parents[1]
    shutil.copytree(
        package / "src" / "miloco",
        root / "src" / "miloco",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    tests = root / "tests"
    tests.mkdir()
    shutil.copyfile(Path(__file__).with_name("conftest.py"), tests / "conftest.py")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / ".env").write_text(
        "MILOCO_SERVER__TOKEN=dotenv-test-token\n"
        f"MiLoCo_DIRECTORIES__STORAGE={root / 'dotenv-storage'}\n",
        encoding="utf-8",
    )
    (tests / "test_probe.py").write_text(
        textwrap.dedent("""\
        import os
        from pathlib import Path

        from fastapi.testclient import TestClient
        from miloco.config import get_settings, reset_settings
        from miloco.utils.paths import miloco_home

        # Read during collection, before fixtures, just like existing test modules.
        collected_settings = get_settings()
        isolated_home = miloco_home().resolve()
        from miloco import main

        def test_import_and_reset_keep_auth_and_storage_isolated():
            assert Path(main.__file__).resolve().is_relative_to(
                Path(__file__).resolve().parents[1] / "src"
            )
            assert collected_settings.server.token == ""
            assert collected_settings.directories.workspace_dir.resolve().is_relative_to(isolated_home)
            assert not (isolated_home / "config.json").exists()
            # Do not enter TestClient's context: real app/routes/auth, no live-device lifespan.
            client = TestClient(main.app)
            try:
                for _ in range(2):
                    assert client.get("/api/admin/features").status_code == 200
                    assert get_settings().server.token == ""
                    assert get_settings().directories.workspace_dir.resolve().is_relative_to(isolated_home)
                    reset_settings()
            finally:
                client.close()

        def test_explicit_overrides_still_work(tmp_path, monkeypatch):
            from miloco.config.settings import MilocoSettings
            try:
                with monkeypatch.context() as patch:
                    patch.setenv("MILOCO_HOME", str(tmp_path))
                    patch.setenv("miloco_server__token", "per-test-token")
                    reset_settings()
                    assert miloco_home() == tmp_path
                    assert get_settings().server.token == "per-test-token"
                    assert MilocoSettings(server={"token": "init-token"}).server.token == "init-token"
            finally:
                reset_settings()
            assert miloco_home().resolve() == isolated_home
            assert get_settings().server.token == ""

        def test_dotenv_can_be_enabled_locally(tmp_path, monkeypatch):
            from dotenv import load_dotenv
            dotenv_file = tmp_path / ".env"
            dotenv_file.write_text("MILOCO_SERVER__TOKEN=explicit-dotenv-token\\n", encoding="utf-8")
            try:
                with monkeypatch.context() as patch:
                    patch.delenv("PYTHON_DOTENV_DISABLED")
                    # Record the key so monkeypatch restores dotenv's mutation too.
                    patch.setenv("MILOCO_SERVER__TOKEN", "")
                    assert load_dotenv(dotenv_file, override=True)
                    reset_settings()
                    assert get_settings().server.token == "explicit-dotenv-token"
            finally:
                reset_settings()
            assert os.environ["PYTHON_DOTENV_DISABLED"] == "1"
            assert get_settings().server.token == ""
        """),
        encoding="utf-8",
    )
    return root


@pytest.mark.parametrize("prefix", ["MILOCO_", "miloco_", "MiLoCo_", None])
def test_collection_isolated_from_deployment_and_dotenv(
    isolated_checkout, tmp_path, prefix
):
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    config = json.dumps(
        {
            "server": {"token": "deployment-test-token"},
            "directories": {"storage": str(tmp_path / "deployment-storage")},
        }
    )
    config_file = deployment / "config.json"
    config_file.write_text(config, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MILOCO_")}
    env.update(
        MILOCO_HOME=str(deployment),
        PYTHONPATH=str(isolated_checkout / "src"),
        PYTHON_DOTENV_DISABLED="0",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTEST_ADDOPTS="",
    )
    if prefix is not None:
        env[prefix + "server__token"] = "inherited-test-token"
        env[prefix + "DIRECTORIES__STORAGE"] = str(tmp_path / "inherited-storage")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_probe.py", "-q"],
        cwd=isolated_checkout,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert config_file.read_text(encoding="utf-8") == config
    assert not (tmp_path / "deployment-storage").exists()
    assert not (tmp_path / "inherited-storage").exists()
    assert not (isolated_checkout / "dotenv-storage").exists()
