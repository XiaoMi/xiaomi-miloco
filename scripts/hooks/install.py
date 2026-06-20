#!/usr/bin/env python3
"""Install git hooks for the Miloco repository.

Usage:
    python3 scripts/hooks/install.py

Installs:
    .git/hooks/pre-commit  →  syncs Hermes plugin skills before each commit
"""
import os
import shutil
import stat
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_SOURCE = Path(__file__).resolve().parent
GIT_HOOKS_DIR = REPO_ROOT / ".git" / "hooks"

HOOKS = ["pre-commit"]


def install() -> int:
    if not GIT_HOOKS_DIR.exists():
        print(f"error: git hooks dir not found at {GIT_HOOKS_DIR}", file=sys.stderr)
        return 1

    for hook_name in HOOKS:
        src = HOOKS_SOURCE / hook_name
        dst = GIT_HOOKS_DIR / hook_name

        if dst.exists():
            dst.unlink()

        shutil.copy2(src, dst)
        st = dst.stat()
        os.chmod(dst, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        print(f"installed {src.name} -> {dst}")

    print(f"\n{len(HOOKS)} hook(s) installed. Skills will auto-sync before each commit.")
    return 0


if __name__ == "__main__":
    sys.exit(install())
