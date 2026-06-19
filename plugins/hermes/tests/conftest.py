import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_miloco_home(monkeypatch, tmp_path):
    home = tmp_path / "miloco"
    home.mkdir()
    monkeypatch.setenv("MILOCO_HOME", str(home))
    return home
