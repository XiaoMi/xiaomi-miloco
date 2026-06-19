import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).parent


def _skills_source_dir():
    return _PLUGIN_DIR.parent / "skills"


def register_skills(ctx):
    src = _skills_source_dir()
    if not src.exists():
        logger.warning("skills source not found: %s", src)
        return
    count = 0
    for child in sorted(src.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)
            count += 1
    logger.info("registered %d skills", count)
    return count
