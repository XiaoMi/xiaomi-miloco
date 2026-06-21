#!/usr/bin/env python3
"""Sync skills from plugins/skills/ to plugins/hermes/skills/.

This script is designed to run as a pre-commit hook. It ensures the
Hermes plugin directory always contains a current copy of the skills
so that `hermes plugins install` (which does git clone + shutil.move)
picks them up without a separate build step.

Usage:
    python3 plugins/hermes/scripts/sync_skills.py          # sync + stage
    python3 plugins/hermes/scripts/sync_skills.py --check  # check only (CI)

Exit codes:
    0 — skills in sync (or successfully synced)
    1 — skills out of sync (--check mode only)
    2 — source skills not found
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
SOURCE = REPO_ROOT / "plugins" / "skills"
TARGET = SCRIPT_DIR.parent / "skills"


def _count_skills(directory: Path) -> int:
    if not directory.exists():
        return 0
    return sum(
        1
        for child in directory.iterdir()
        if child.is_dir() and (child / "SKILL.md").exists()
    )


def _dirs_equal(src: Path, dst: Path) -> bool:
    if not src.exists() or not dst.exists():
        return False
    src_files = {p.relative_to(src) for p in src.rglob("*") if p.is_file()}
    dst_files = {p.relative_to(dst) for p in dst.rglob("*") if p.is_file()}
    if src_files != dst_files:
        return False
    for rel in src_files:
        if (src / rel).read_bytes() != (dst / rel).read_bytes():
            return False
    return True


def sync() -> int:
    if not SOURCE.exists():
        print(f"error: source skills not found at {SOURCE}", file=sys.stderr)
        return 2

    if _dirs_equal(SOURCE, TARGET):
        print(f"skills already in sync ({_count_skills(TARGET)} skills)")
        return 0

    shutil.rmtree(TARGET, ignore_errors=True)
    shutil.copytree(SOURCE, TARGET)

    count = _count_skills(TARGET)
    print(f"synced {count} skills: {SOURCE} -> {TARGET}")
    return 0


def check() -> int:
    if not SOURCE.exists():
        print(f"error: source skills not found at {SOURCE}", file=sys.stderr)
        return 2

    if not _dirs_equal(SOURCE, TARGET):
        print(
            "skills out of sync — run "
            "python3 plugins/hermes/scripts/sync_skills.py and re-commit",
            file=sys.stderr,
        )
        return 1

    print(f"skills in sync ({_count_skills(TARGET)} skills)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Miloco skills for Hermes plugin")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="check only, do not sync")
    mode.add_argument(
        "--stage",
        action="store_true",
        default=True,
        help="git add synced files (default)",
    )
    args = parser.parse_args()

    if args.check:
        return check()

    rc = sync()
    if rc == 0 and args.stage:
        import subprocess

        subprocess.run(
            ["git", "add", str(TARGET)],
            cwd=str(REPO_ROOT),
            check=False,
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
