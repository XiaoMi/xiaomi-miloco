"""sync-skills.py — 同步 miloco-* skill 到 Hermes 兼容目录。

把 `plugins/skills/miloco-*`（16 个目录）复制到 `plugins/hermes/skills/`，
对每个 SKILL.md 做最小适配：

1. 删除 YAML frontmatter 里 `metadata.openclaw` 整个子键（Hermes skill
   frontmatter 无此字段；见 hermes-agent/website/docs/developer-guide/
   creating-skills.md）。保留 name / description / metadata(author/version/date)
   等 agentskills.io 标准字段。
2. 特别修改 `miloco-terminate-task/SKILL.md`：正文里
   「OpenClaw cron tool `action=remove`」改成
   「Hermes cronjob tool `action=remove`」（Hermes 的 cron 工具叫 cronjob）。
3. 给 frontmatter `date:` 值强制加引号 —— 未加引号的日期会被 YAML 解析成
   ``datetime.date``，导致 Hermes skill_view 的 json.dumps 失败（cron 触发时
   加载 skill 报 `Object of type date is not JSON serializable`）。
4. 其余正文原样保留 —— 这些 skill 全通过 `miloco-cli` 调后端 HTTP API，
   与 agent 平台无关。

幂等：每次运行先清空目标 `plugins/hermes/skills/` 下所有 miloco-* 目录
再重新生成，源目录 `plugins/skills/` 只读不动。

用法：
    python plugins/hermes/scripts/sync-skills.py
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

# 源 / 目标根目录（脚本位于 plugins/hermes/scripts/，故上溯两级取 plugins/）。
SCRIPT_DIR = Path(__file__).resolve().parent
PLUGINS_DIR = SCRIPT_DIR.parents[1]
SRC_ROOT = PLUGINS_DIR / "skills"
DST_ROOT = PLUGINS_DIR / "hermes" / "skills"

SKILL_GLOB = "miloco-*"

# YAML frontmatter 边界：文件首行 `---`，匹配到下一个独占一行的 `---`。
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)



def quote_date_field(frontmatter_text: str) -> str:
    """给 frontmatter 里未加引号的 `date:` 值加上双引号。

    Hermes 的 skill_view 会把 skill 元数据 json.dumps 返回；YAML 把未加引号的
    `date: 2026-06-16` 解析成 ``datetime.date`` 对象，导致
    ``Object of type date is not JSON serializable``，cron 触发时加载 skill 失败
    （见 hermes-agent/cron/scheduler.py 调 skill_view）。强制加引号让 date 恒为字符串。

    已加引号的（``date: "2026-06-14"``）不匹配，原样保留。
    """
    return re.sub(
        r"(^(\s*)date:\s*)(\d{4}-\d{2}-\d{2})\s*$",
        r'\1"\3"',
        frontmatter_text,
        flags=re.MULTILINE,
    )


def patch_terminate_task_body(body: str) -> str:
    """miloco-terminate-task 专用：把正文里
    「OpenClaw cron tool `action=remove`」改成
    「Hermes cronjob tool `action=remove`」。

    仅替换这一处明确措辞（任务规范指定），其余 `OpenClaw cron` 引用
    （如 cron `action=remove` 不带 tool 字样的）原样保留。
    """
    return body.replace(
        "OpenClaw cron tool `action=remove`",
        "Hermes cronjob tool `action=remove`",
    )


def adapt_skill_md(content: str, skill_name: str) -> str:
    """对单个 SKILL.md 内容做 Hermes 适配，返回新内容。"""
    m = FRONTMATTER_RE.match(content)
    if not m:
        # 无 frontmatter —— 理论不会发生，但容错：原样返回（仅 terminate-task
        # 改正文）。
        body = content
        if skill_name == "miloco-terminate-task":
            body = patch_terminate_task_body(body)
        return body

    fm_text = m.group(1)
    new_fm = quote_date_field(fm_text)
    body = content[m.end():]
    if skill_name == "miloco-terminate-task":
        body = patch_terminate_task_body(body)
    return f"---\n{new_fm}\n---\n{body}"


def sync_one(src_dir: Path, dst_root: Path) -> Path:
    """复制单个 skill 目录到 dst_root/<name>/，适配 SKILL.md。"""
    name = src_dir.name
    dst_dir = dst_root / name

    # 覆盖式复制：先删目标目录（若存在），再整树拷贝。
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    shutil.copytree(src_dir, dst_dir)

    skill_md = dst_dir / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"skill 缺 SKILL.md: {skill_md}")
    original = skill_md.read_text(encoding="utf-8")
    adapted = adapt_skill_md(original, name)
    skill_md.write_text(adapted, encoding="utf-8")
    return dst_dir


def main() -> int:
    if not SRC_ROOT.is_dir():
        sys.stderr.write(f"源目录不存在: {SRC_ROOT}\n")
        return 1

    src_dirs = sorted(p for p in SRC_ROOT.iterdir() if p.is_dir() and p.name.startswith(SKILL_GLOB.replace("*", "")))
    if not src_dirs:
        sys.stderr.write(f"未在 {SRC_ROOT} 找到 {SKILL_GLOB} 目录\n")
        return 1

    # 幂等：清空目标根下所有 miloco-* 目录再重生成。
    DST_ROOT.mkdir(parents=True, exist_ok=True)
    for old in DST_ROOT.iterdir():
        if old.is_dir() and old.name.startswith("miloco-"):
            shutil.rmtree(old)

    generated: list[str] = []
    for src_dir in src_dirs:
        dst_dir = sync_one(src_dir, DST_ROOT)
        generated.append(dst_dir.name)

    print(f"同步完成：{len(generated)} 个 skill -> {DST_ROOT}")
    for name in generated:
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
