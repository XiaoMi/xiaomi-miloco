# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Admin controller
System status check interface
"""

import asyncio
import json
import logging
import re
import shlex
import subprocess
import time
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StrictBool
from sse_starlette.sse import EventSourceResponse

from miloco.admin import log_pack as _log_pack_mod
from miloco.config import get_settings
from miloco.database.token_usage_repo import get_token_usage_repo
from miloco.manager import get_manager
from miloco.middleware import verify_token, verify_token_query_fallback
from miloco.observability import debug as debug_mod
from miloco.perception.engine.omni import probe as _probe
from miloco.schema.common_schema import NormalResponse
from miloco.utils.agent_config import update_shared_config
from miloco.utils.paths import miloco_home

logger = logging.getLogger(name=__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])

manager = get_manager()


@router.get("/status", summary="System Status", response_model=NormalResponse)
async def get_system_status(current_user: str = Depends(verify_token)):
    """
    Check system component status:
    - MiOT: whether logged in with valid token
    - SQLite: whether database is accessible
    - Perception model: whether a vision_understanding model is activated
    - Rule engine: whether running and how many rules are loaded
    """
    logger.info("Get system status API called - User: %s", current_user)

    # MiOT login status
    try:
        miot_ok = await manager.miot_proxy.check_token_valid()
    except Exception:
        miot_ok = False

    # SQLite status
    try:
        rule_service = manager.rule_service
        total_rules = rule_service._repo.count_all()
        enabled_rules = rule_service._repo.count_enabled()
        sqlite_ok = True
    except Exception:
        total_rules = 0
        enabled_rules = 0
        sqlite_ok = False

    # Perception status
    try:
        perception_status = manager.perception_service.engine_status()
        perception_ok = perception_status.running
    except Exception:
        perception_ok = False

    data = {
        "miot": {"ok": miot_ok},
        "sqlite": {"ok": sqlite_ok},
        "perception": {"ok": perception_ok},
        "rule_engine": {
            "total_rules": total_rules,
            "enabled_rules": enabled_rules,
        },
    }

    logger.info("System status retrieved: %s", data)
    return NormalResponse(
        code=0, message="System status retrieved successfully", data=data
    )


def _run_git(args: list[str]) -> str | None:
    """在 backend 源码目录跑 git, 失败/超时/无 git 都返回 None (让 git 自动向上找 .git)。"""
    try:
        r = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=2,
            cwd=Path(__file__).resolve().parent,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


_HATCH_VCS_LOCAL_RE = re.compile(r"\+g([0-9a-f]{7,40})(?:\.d(\d{8}))?")


def _parse_version_git(v: str) -> dict | None:
    """从 hatch-vcs local version 段提取 commit_short + dirty。

    Wheel 部署无 .git 时的 fallback: pyproject 里 hatch-vcs 会把版本号写成
    ``0.1.0.dev5+g4a2b3c1.d20260701``, 其中 ``g<sha>`` 是构建时 commit,
    ``.d<YYYYMMDD>`` 存在表示构建时 tree 有未提交改动。
    """
    m = _HATCH_VCS_LOCAL_RE.search(v)
    if m is None:
        return None
    sha = m.group(1)
    return {
        "commit": sha if len(sha) == 40 else None,
        "commit_short": sha[:7],
        "branch": None,
        "dirty": m.group(2) is not None,
        "commit_time": None,
    }


def _git_info(version: str | None = None) -> dict | None:
    """优先跑 git 命令 (source checkout); 失败时从 pkg version 里解析 (wheel 部署)。"""
    commit = _run_git(["rev-parse", "HEAD"])
    if commit:
        branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        status = _run_git(["status", "--porcelain"])
        commit_time = _run_git(["log", "-1", "--format=%cI", "HEAD"])
        return {
            "commit": commit,
            "commit_short": commit[:7],
            "branch": branch if branch and branch != "HEAD" else None,
            "dirty": bool(status) if status is not None else None,
            "commit_time": commit_time or None,
        }
    return _parse_version_git(version) if version else None


def _pkg_version() -> str:
    try:
        return pkg_version("miloco")
    except PackageNotFoundError:
        return "unknown"


@router.get(
    "/version",
    summary="Backend version (package version + git info if available)",
    response_model=NormalResponse,
)
async def get_version(current_user: str = Depends(verify_token)):
    """Return backend package version and, if deployed from a git checkout,
    the current commit / branch / dirty flag / commit time.

    ``data.git`` is null when backend runs from a wheel / docker image without
    the .git directory (i.e. not a git checkout).
    """
    version = _pkg_version()
    return NormalResponse(
        code=0,
        message="Version info",
        data={
            "version": version,
            "git": _git_info(version),
        },
    )


# ── 升级检测 / 一键升级 ────────────────────────────────────────────────
# 发布只走 GitHub Release（CalVer tag）。检测 = 查 releases/latest（最新正式版）比对
# 当前版本；一键升级 = detached 起官方 install.sh 的**非交互 agent 模式**——install.py
# 默认 run() 在无 tty 时会 fail("non_interactive") 中止（main:1643-1645），故必须走
# --agent-prepare/--agent-finish（它在 tty 检查前 return，全程无交互、保留现有 config）。
# 仅 release/wheel 部署可一键升级；dev(git) 只提示 git pull。roll-forward，不回滚。
_GH_REPO = "XiaoMi/xiaomi-miloco"
_GH_LATEST_API = f"https://api.github.com/repos/{_GH_REPO}/releases/latest"
_INSTALL_SH_URL = f"https://github.com/{_GH_REPO}/releases/latest/download/install.sh"
_UPGRADE_CHECK_TTL_S = 6 * 3600
# 失败（GitHub 不可达/限流）时的短退避 TTL：负缓存，避免故障期间每次开页都重打接口
# （正是缓存要防的）。成功缓存 6h，失败仅缓存 10min 后重试。
_UPGRADE_CHECK_NEG_TTL_S = 10 * 60

# 服务端共享缓存（进程内），避免多用户每次打开页面都打 GitHub / 触发限流。
_upgrade_check_cache: dict = {"ts": 0.0, "data": None}
# 一键升级单飞标志（进程内；升级成功会重启 backend，重启后自然复位）。用时间戳而非
# 布尔：正常路径靠重启复位，但若 detached 脚本在重启服务前就失败（如 curl 下载失败、
# set -e 提前中止，service 从未停/起），当前 backend 会一直存活且标志永远为 True，导致
# 后续一键升级永久 409。故加 TTL 自愈——超过 TTL 视为上次尝试已失败，允许重新触发。
# **TTL 必须 ≥ 前端轮询超时（POLL_TIMEOUT_MS=20min）**：否则一次合法的慢升级（冷下载
# 68MB 可达十几分钟）跑到 15min 后，第二个标签页仍看到缓存 has_update 触发 /run 就能通过
# 单飞、起第二个 install.sh，与第一个抢共享下载缓存 + 抢 upgrade.log 截断 + 抢 service restart。
_UPGRADE_SINGLEFLIGHT_TTL_S = 25 * 60
# 单飞状态放进可变 dict 而非模块级标量：subscript 赋值是"就地修改"、非全局名重绑定，
# 从而避开 CodeQL py/unused-global-variable 的跨调用误报（标量 global 在函数内写后当轮不再读，
# 静态分析看不到"下次请求才读"而误判为写死）。语义不变：进程级、跨请求持久的单飞时间戳。
_upgrade_state: dict[str, float] = {"started_at": 0.0}


# 「已确认（dismiss）到的版本」：用户关闭升级 banner 即记录于此，之后该版本 banner 永久不再
# 出现、直到出现更新的版本。**存后端**（MILOCO_HOME 下文件），不放浏览器——随服务器状态走：
# 彻底卸载/重装即清零、跨浏览器一致、可测、不被浏览器缓存干扰。红点不看它（有更新就显）。
def _dismiss_file() -> Path:
    return miloco_home() / "upgrade_dismissed"


def _read_dismissed() -> str | None:
    try:
        v = _dismiss_file().read_text(encoding="utf-8").strip()
        return v or None
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _write_dismissed(version: str) -> None:
    # 尽力持久化：写盘失败（磁盘满/权限）不该让"关 banner"这个纯 UI 动作 500。失败只记
    # 日志、静默降级——banner 本次仍会隐藏（前端已就地更新），仅重载后可能再现。
    try:
        _dismiss_file().write_text(version.strip(), encoding="utf-8")
    except Exception as e:
        logger.warning("failed to persist dismissed version %s: %s", _scrub_log(version), e)


def _scrub_log(value: object) -> str:
    """中和插入日志的外部值里的 CR/LF，防日志伪造/注入（CodeQL py/log-injection）。"""
    return str(value).replace("\r", " ").replace("\n", " ")


def _deploy_kind() -> str:
    """release = 正式发布版（干净 CalVer tag）；dev = git checkout / 未打 tag 的构建。

    用**包版本串**判定而非 ``git rev-parse``：后者会沿目录向上误命中 $HOME / venv 所在的
    无关 .git 仓库（如 dotfiles 仓），把正式 wheel 部署误判成 dev、白白禁用一键升级。
    hatch-vcs 对非 tag 构建会写 ``.dev<N>`` / ``+g<sha>`` 本地段；干净 tag 版则没有。"""
    v = _pkg_version()
    if v == "unknown":
        return "release"
    return "dev" if (".dev" in v or _HATCH_VCS_LOCAL_RE.search(v)) else "release"


def _norm_ver(v: str) -> str:
    return v[1:] if v.startswith("v") else v


def _latest_is_newer(current: str, latest: str) -> bool:
    """latest 是否严格新于 current（按 PEP440/CalVer 解析，非字符串比较）。

    packaging 是 miloco 的传递依赖（非直接声明）。若环境里缺失（ImportError）、
    或版本串非法（InvalidVersion）、或任何解析异常，都**保守视为"无更新"**并静默降级——
    绝不让 ``/upgrade/check`` 因版本比较抛错而 500（宁可不提示，也不误报/不崩）。
    """
    try:
        from packaging.version import Version

        return Version(_norm_ver(latest)) > Version(_norm_ver(current))
    except Exception as e:
        # latest 源自 GitHub tag_name（外部可控），current 源自本地包版本——统一经
        # _scrub_log 中和 CR/LF，与 upgrade_run/_write_dismissed 一致地闭合日志注入这一类。
        # 异常 e 也必须 _scrub_log：packaging 的 InvalidVersion 会把原始（未净化的）版本串
        # 原样嵌进消息（"Invalid version: '<latest>'"），直接打 e 等于把 latest 的 CR/LF
        # 从旁路重新带回日志、抵消上面对 latest 的净化——故一并中和。
        logger.info(
            "upgrade check: version compare failed (%s vs %s): %s",
            _scrub_log(current),
            _scrub_log(latest),
            _scrub_log(e),
        )
        return False


async def _fetch_latest_release() -> dict | None:
    """查 GitHub releases/latest（最新正式版）。不可达 / 失败返回 None，不抛。"""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                _GH_LATEST_API, headers={"Accept": "application/vnd.github+json"}
            )
        if r.status_code != 200:
            return None
        j = r.json()
        tag = j.get("tag_name")
        if not tag:
            return None
        return {"tag": tag, "html_url": j.get("html_url")}
    except Exception as e:  # 网络不可达 / 超时 / 解析失败 → 静默降级，不打扰用户
        # e 亦经 _scrub_log：JSONDecodeError 等可能把响应体片段（外部数据）嵌进消息，
        # 与 _latest_is_newer 一致地闭合日志注入旁路。
        logger.info("upgrade check: fetch latest release failed: %s", _scrub_log(e))
        return None


@router.get(
    "/upgrade/check",
    summary="Check whether a newer official release is available",
    response_model=NormalResponse,
)
async def upgrade_check(
    force: bool = Query(
        False, description="跳过服务端缓存强制现查一次（供用户手动「检查更新」）"
    ),
    current_user: str = Depends(verify_token),
):
    """检测是否有新版本。前端打开页面时调一次（走服务端缓存、无轮询）；用户手动「检查更新」
    时带 ``force=true`` 跳过缓存现查一次。GitHub 不可达时 ``reachable=false`` 且
    ``has_update=false``——不报错、不打扰。dev(git) 部署不给一键升级，仅提示。"""
    current = _pkg_version()
    kind = _deploy_kind()
    now = time.time()

    # 负缓存：失败(rel=None)也缓存，但用更短 TTL 退避——ts==0 表示从未查过。
    # force：用户手动检查，跳过缓存强制现查（结果仍写回缓存）。
    ts = _upgrade_check_cache["ts"]
    cached = _upgrade_check_cache["data"]
    ttl = _UPGRADE_CHECK_TTL_S if cached is not None else _UPGRADE_CHECK_NEG_TTL_S
    if force or ts == 0.0 or (now - ts) > ttl:
        rel = await _fetch_latest_release()
        if rel is not None:
            _upgrade_check_cache["data"] = rel
            _upgrade_check_cache["ts"] = now
        elif cached is not None:
            # 一次抖动（GitHub 限流/超时）不清空上次好结果——否则 banner 会因短暂不可达消失。
            # 把 ts 回拨成"再过 NEG_TTL 就重试"，保留旧好值继续展示。
            _upgrade_check_cache["ts"] = now - (
                _UPGRADE_CHECK_TTL_S - _UPGRADE_CHECK_NEG_TTL_S
            )
            rel = cached
        else:
            # 从未成功过：记不可达，NEG_TTL 短退避后再试。
            _upgrade_check_cache["data"] = None
            _upgrade_check_cache["ts"] = now
    else:
        rel = cached

    reachable = rel is not None
    latest = _norm_ver(rel["tag"]) if reachable else None
    has_update = bool(
        reachable and kind == "release" and _latest_is_newer(current, rel["tag"])
    )
    return NormalResponse(
        code=0,
        message="upgrade check",
        data={
            "current": current,
            "latest": latest,
            "has_update": has_update,
            "deploy_kind": kind,
            "release_url": rel["html_url"] if reachable else None,
            "reachable": reachable,
            "checked_at": int(_upgrade_check_cache["ts"]),
            # 已确认版本（后端持久化）。前端据此决定 banner 显隐：latest === dismissed 则不显。
            "dismissed": _read_dismissed(),
        },
    )


@router.post(
    "/upgrade/dismiss",
    summary="Dismiss the upgrade banner for a version (won't reappear until a newer release)",
    response_model=NormalResponse,
)
async def upgrade_dismiss(
    version: str = Query(..., description="要标记为已确认的版本号（一般传当前 latest）"),
    current_user: str = Depends(verify_token),
):
    """用户关闭升级 banner 时调用：把该版本记为「已确认」并持久化到后端（MILOCO_HOME）。
    之后 banner 对该版本永久不再出现，直到出现更新的版本。语义与存储位置解释见 `_read_dismissed`。"""
    v = _norm_ver(version)
    _write_dismissed(v)
    return NormalResponse(code=0, message="dismissed", data={"dismissed": v})


@router.post(
    "/upgrade/run",
    summary="Trigger a one-click upgrade to the latest official release",
    response_model=NormalResponse,
)
async def upgrade_run(current_user: str = Depends(verify_token)):
    """一键升级：detached 起官方 install.sh（非交互 agent 模式）覆盖安装并重启服务。
    仅 release 部署可用；dev(git) 拒绝。升级进程脱离 backend 进程组，backend 被安装器
    重启也不影响它；日志落 ``<log_dir>/upgrade.log``。roll-forward、不回滚——失败由
    前端引导用户重跑 install.sh / 查日志。"""
    if _deploy_kind() != "release":
        raise HTTPException(
            status_code=400,
            detail="git checkout deployment: use `git pull`; one-click upgrade is disabled",
        )
    # 前置校验：确实有更新才升（防御纵深——前端已 gate 按钮，但直接 POST 也不该
    # 触发"重装同版本"式的无谓重启）。latest 取自 /check 的服务端缓存；不可达/未查过
    # 时保守拒绝，让前端先跑 check。
    current = _pkg_version()
    latest_rel = _upgrade_check_cache["data"]
    target = _norm_ver(latest_rel["tag"]) if latest_rel else None
    if not latest_rel or not _latest_is_newer(current, latest_rel["tag"]):
        # 400（非 409）：与"已在升级中"区分开——前端据状态码判定，409=接管进度、400=失败提示。
        raise HTTPException(
            status_code=400,
            detail="already on the latest release (or latest unknown); nothing to upgrade",
        )
    since_last = time.time() - _upgrade_state["started_at"]
    if _upgrade_state["started_at"] and since_last < _UPGRADE_SINGLEFLIGHT_TTL_S:
        # 单飞 TTL 内——但若上次尝试已在日志写下终态标记（done/failed），证明它已彻底
        # 结束、无进程在跑（终态是脚本 exit 前最后一步 echo），提前解除单飞允许重试，
        # 避免快失败（如 curl 连不上 GitHub/限流，install 从未跑、原 backend 从未重启）
        # 后被硬锁满 TTL。这**不违反单飞不变量**（TTL 常量与「TTL ≥ 前端轮询超时」不等式
        # 均不变，仅在有正向"已结束"证据时短路）：仍在跑的慢升级日志无终态标记 → 照旧 409，
        # 不会起第二个 install.sh 与其抢下载缓存/日志/重启。此判定与 /run 其余部分同样无
        # await，事件循环内原子执行；放行后 open("w") 会截断残留日志、重开一份干净的。
        if _read_upgrade_phase() not in _UPGRADE_TERMINAL_PHASES:
            raise HTTPException(
                status_code=409, detail="an upgrade is already in progress"
            )

    launched = False
    try:
        settings = get_settings()
        log_dir = Path(settings.directories.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        upgrade_log = log_dir / "upgrade.log"
        home = miloco_home()
        tmp_sh = home / ".upgrade-install.sh"

        # 关键点：
        #  - `export MILOCO_LANG=zh`：把安装器日志语言**钉死为中文**，使 /upgrade/status 的
        #    阶段解析与服务器 LANG 无关（否则英文 locale 服务器上日志是英文、中文标记全不匹配、
        #    进度失灵）。日志是内部产物，用户看到的步骤文字走前端 i18n、跟随网页语言，与此无关。
        #  - 路径经 shlex.quote 防注入（MILOCO_HOME 来自环境变量，双引号内仍会展开 $()/反引号）。
        #  - **不能用 `set -e`**：curl 下载失败（连不上 GitHub / 限流——恰是最常见的失败）会让
        #    `set -e` 在写终态标记前就中止整脚本，日志无 AGENT_UPGRADE_FAILED，前端只能空等
        #    20min 轮询超时才知失败。故改为把 curl 用 `|| rc=$?` 收进 rc、下载失败即跳过安装、
        #    仍走到末尾 echo AGENT_UPGRADE_FAILED，保证任何失败都有终态标记（快失败）。
        #  - 末尾无条件 `service start` 兜底 install.py 的 atexit 停服务（curl 失败时服务从未被
        #    停、start 幂等无害）；prepare/finish 用 `&&`+`|| rc=$?`，失败也走到 service start
        #    （roll-forward、不回滚），最后以安装码收尾。
        #  - **终态标记**：install.py 在 prepare/finish 里会多次重启到新版本（新版本会"提前"
        #    可达），单看版本变更会误判完成。故脚本在**全部跑完 + 最后一次 service start 之后**
        #    才 echo AGENT_UPGRADE_DONE/FAILED 到日志——这是整个升级唯一可靠的终态信号，
        #    /upgrade/status 据此报 done/failed，前端只认这个标记才判完成，不看中途版本变更。
        q_url = shlex.quote(_INSTALL_SH_URL)
        q_sh = shlex.quote(str(tmp_sh))
        script = (
            "export MILOCO_LANG=zh; rc=0; "
            f"curl -fsSL {q_url} -o {q_sh} || rc=$?; "
            'if [ "$rc" = "0" ]; then '
            f"bash {q_sh} --agent-prepare && bash {q_sh} --agent-finish || rc=$?; "
            "fi; "
            "miloco-cli service start || true; "
            'if [ "$rc" = "0" ]; then echo "AGENT_UPGRADE_DONE"; '
            'else echo "AGENT_UPGRADE_FAILED rc=$rc"; fi; '
            # 清理下载的临时安装脚本：放在终态标记 echo 之后，不影响进度/终态解析；
            # 无论 done/failed 都删，避免 MILOCO_HOME 长期残留 .upgrade-install.sh。
            f"rm -f {q_sh}; "
            "exit $rc"
        )
        # 截断（"w"）：每次升级独立日志，避免上轮旧内容让 /upgrade/status 误报阶段。
        logf = open(upgrade_log, "w")
        try:
            subprocess.Popen(
                ["bash", "-lc", script],
                cwd=str(home),
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # 脱离 backend 进程组/会话，重启 backend 不波及它
            )
            # 起进程成功即占单飞（在 finally 关 fd 之前置位）：即便随后 logf.close() 抛错走到
            # except，也不会漏计已启动的进程、避免下一次 /run 与其并起第二个 install.sh。
            launched = True
            _upgrade_state["started_at"] = time.time()
        finally:
            logf.close()  # 子进程已 dup fd，父进程可关
    except HTTPException:
        raise
    except Exception as e:
        # 启动失败（未起任何进程）：显式释放单飞。否则若本次是「快失败后重试」（上一轮已写终态
        # 标记、_upgrade_state["started_at"] 仍在 TTL 内），上面的 open("w") 已把日志截断、终态标记丢失
        # （phase 退回 starting=非终态），而 _upgrade_state["started_at"] 未被本次刷新 → TTL 内后续 /run
        # 恒 409 锁死用户，却根本没有进程在跑。未起进程即释放，允许立即重试（fail-open 且安全：
        # 无进程 → 不会 racing）。
        if not launched:
            _upgrade_state["started_at"] = 0.0
        logger.error("failed to launch upgrade process: %s", _scrub_log(e))
        raise HTTPException(
            status_code=500, detail="failed to launch upgrade process"
        ) from e

    # 审计日志刻意**不插入** target / current_user：current_user 恒为 None（verify_token
    # 返回 None、仅做鉴权），target 源自 GitHub tag（外部可控）——两者都是 CodeQL 日志注入
    # 的污点源。目标版本已随响应体 data.target 返回给调用方，无需再进日志。静态文案即可、
    # 零外部值进日志，彻底闭合 py/log-injection（不靠脱敏兜）。
    logger.info("one-click upgrade triggered")
    return NormalResponse(
        code=0,
        message="upgrade started",
        data={"started": True, "target": target},
    )


# upgrade.log 阶段锚点。取在日志中**出现位置最靠后**（rfind）的标记，而非列表顺序——
# 因为安装器会先打印「安装核心组件」标题、再打印「正在下载」，按列表顺序会把整段下载误判成安装。
# 升级子进程强制 MILOCO_LANG=zh（见 /upgrade/run），中文标记确定可匹配；归档名 `.tar.gz`
# 与语言无关，作下载兜底锚点。done/failed 由 /upgrade/run 脚本在全部跑完后 echo，是整个升级
# 唯一可靠的终态标记（位置必在最后）——前端只认它判完成，不看中途"提前"的版本变更。
# 前端把 downloading/installing 映射到「下载」「安装」步，「重启」由前端凭后端连不上判定。
_UPGRADE_STAGES = [
    (".tar.gz", "downloading"),
    ("正在下载", "downloading"),
    ("安装核心组件", "installing"),
    ("安装 miloco", "installing"),
    ("初始化服务", "installing"),
    ("引擎预热", "installing"),
    ("准备感知模型", "installing"),
    ("解压感知模型", "installing"),
    ("AGENT_JSON", "installing"),
    ("AGENT_UPGRADE_FAILED", "failed"),
    ("AGENT_UPGRADE_DONE", "done"),
]

# 终态标记：脚本 exit 前的最后一步 echo，出现即证明升级子进程已彻底结束（无进程在跑）。
_UPGRADE_TERMINAL_PHASES = ("done", "failed")


def _read_upgrade_phase() -> str:
    """从 upgrade.log 解析当前升级阶段（单一事实源，/upgrade/status 与单飞自愈共用）。

    无日志 → idle；有日志但无任何锚点 → starting；否则取**出现位置最靠后**（rfind）
    的阶段。强制 utf-8 解码：日志已被 MILOCO_LANG=zh 钉成中文，若按 locale 默认
    （C/POSIX）解码中文标记会成 U+FFFD 全不匹配、进度失灵；errors="replace" 只兜底
    非法字节，不改匹配。
    """
    settings = get_settings()
    log = Path(settings.directories.log_dir) / "upgrade.log"
    if not log.exists():
        return "idle"
    try:
        text = log.read_text(encoding="utf-8", errors="replace")[-8000:]
    except Exception:
        text = ""
    phase, best_pos = "starting", -1
    for marker, ph in _UPGRADE_STAGES:
        pos = text.rfind(marker)
        if pos > best_pos:
            best_pos, phase = pos, ph  # 取日志中出现位置最靠后的阶段
    return phase


@router.get(
    "/upgrade/status",
    summary="Progress of an in-flight one-click upgrade (parsed from upgrade.log)",
    response_model=NormalResponse,
)
async def upgrade_status(current_user: str = Depends(verify_token)):
    """升级阶段：从 upgrade.log 解析当前处于哪一阶段，供前端点亮对应步骤 / 判完成。
    「重启」阶段本端点随 backend 一起消失（前端凭连不上判定）。
    返回 phase ∈ {idle, starting, downloading, installing, done, failed}。"""
    return NormalResponse(
        code=0, message="upgrade status", data={"phase": _read_upgrade_phase()}
    )


@router.get(
    "/token-usage",
    summary="Token Usage (raw events in [since, until])",
    response_model=NormalResponse,
)
async def get_token_usage(
    since: int | None = None,
    until: int | None = None,
    limit: int = 10000,
    current_user: str = Depends(verify_token),
):
    """Raw token-usage events in [since, until] (ms epoch). Defaults to today.

    ``limit`` caps the response size; ``truncated=true`` in the payload tells
    the client to narrow the window if the cap is hit. Up to ~3 days of data
    is queryable (older events have been rolled up to /token-usage/daily).
    """
    events, truncated = get_token_usage_repo().list_events(since, until, limit)
    return NormalResponse(
        code=0,
        message="ok",
        data={"events": events, "total": len(events), "truncated": truncated},
    )


@router.get(
    "/token-usage/daily",
    summary="Token Usage (daily rollup by date / model / type)",
    response_model=NormalResponse,
)
async def get_token_usage_daily(
    since: str | None = None,
    until: str | None = None,
    current_user: str = Depends(verify_token),
):
    """Daily rollup rows (date / model / type) combining historical + today's live."""
    rows = get_token_usage_repo().aggregate_daily(since, until)
    return NormalResponse(code=0, message="ok", data={"rows": rows, "total": len(rows)})


@router.get(
    "/token-usage/buckets",
    summary="Token Usage (today, server-side bucketed by time / model / type)",
    response_model=NormalResponse,
)
async def get_token_usage_buckets(
    since: int | None = None,
    until: int | None = None,
    bin_minutes: int = Query(60, alias="bin", ge=1),
    current_user: str = Depends(verify_token),
):
    """Server-side bucketed aggregation for the "today" view (ms epoch window).

    ``bin`` is the bucket width in minutes. Response size is bounded by bucket
    count, so it never hits the raw-event cap regardless of activity — preferred
    over /token-usage for the today timeline.
    """
    rows = get_token_usage_repo().aggregate_buckets(since, until, bin_minutes)
    return NormalResponse(code=0, message="ok", data={"rows": rows, "total": len(rows)})


@router.post(
    "/token-usage/clear",
    summary="清空全部 Token 用量(实时表 + 日聚合，不可恢复)",
    response_model=NormalResponse,
)
def clear_token_usage(current_user: str = Depends(verify_token)):
    """删除 token_usage + token_usage_daily 全部行，返回各表删除条数。供重置统计用。"""
    deleted = get_token_usage_repo().clear_all()
    return NormalResponse(code=0, message="ok", data={"deleted": deleted})


# ─── debug 开关(同步 runtime override + .debug_observability 文件 flag) ────────


class DebugOverrideBody(BaseModel):
    enabled: StrictBool


@router.get("/debug", summary="Debug 开关状态", response_model=NormalResponse)
def get_debug_state(current_user: str = Depends(verify_token)):
    """返回 observability debug 开关的当前状态。

    解析顺序: runtime override > 文件 flag > 默认 False。
    """
    return NormalResponse(code=0, message="ok", data=debug_mod.get_state())


@router.post(
    "/debug",
    summary="设置 Debug 开关(同步 runtime override + 文件 flag)",
    response_model=NormalResponse,
)
def set_debug_override(
    body: DebugOverrideBody, current_user: str = Depends(verify_token)
):
    """``enabled=true`` 开启并创建 .debug_observability;
    ``enabled=false`` 关闭并删除文件。重启后从文件 flag 恢复状态。

    本 flag 目前不挂任何已有行为,保留供后续 debug 选项接入。
    """
    debug_mod.set_runtime_override(body.enabled)
    return NormalResponse(code=0, message="ok", data=debug_mod.get_state())


class SchedulerConfigBody(BaseModel):
    enabled: StrictBool


@router.get(
    "/scheduler-config",
    summary="内置定时任务自动管理开关状态",
    response_model=NormalResponse,
)
def get_scheduler_config(current_user: str = Depends(verify_token)):
    """返回 ``scheduler.enabled``：是否由 miloco 自动管理内置定时任务。"""
    return NormalResponse(
        code=0,
        message="ok",
        data={"enabled": get_settings().scheduler.enabled},
    )


@router.put(
    "/scheduler-config",
    summary="设置内置定时任务自动管理开关(写盘 config.json)",
    response_model=NormalResponse,
)
def put_scheduler_config(
    body: SchedulerConfigBody, current_user: str = Depends(verify_token)
):
    """写入 ``scheduler.enabled`` 到 ``config.json``。

    实际生效方是 openclaw 插件——它在网关下次启动时读取此开关；
    ``enabled=false`` 时清除并停止重建内置定时任务。backend 不控制 openclaw
    网关生命周期，故此改动在网关下次重启后生效。
    """
    update_shared_config(scheduler={"enabled": body.enabled})
    return NormalResponse(
        code=0,
        message="ok",
        data={"enabled": get_settings().scheduler.enabled},
    )


@router.post(
    "/debug/log-pack",
    summary="打包 trace db / jsonl / log 到 $MILOCO_HOME/packs/",
    response_model=NormalResponse,
)
def post_log_pack(current_user: str = Depends(verify_token)):
    """LRU 保留最新 2 个;预扫描超 500MB 返 422 + 各组件 size 明细。"""
    try:
        result = _log_pack_mod.build_log_pack()
    except _log_pack_mod.LogPackSizeExceeded as e:
        raise HTTPException(status_code=422, detail=e.info)
    return NormalResponse(code=0, message="ok", data=result)


# ─── 事件反馈(打包 omni 复现数据) ─────────────────────────────────────────


class EventFeedbackBody(BaseModel):
    event_id: str
    error_types: list[str] = []
    feedback_text: str = ""
    include_gallery: bool = False


@router.post(
    "/events/feedback",
    summary="提交感知事件反馈(打包 omni 复现数据)",
    response_model=NormalResponse,
)
async def submit_event_feedback(
    body: EventFeedbackBody,
    current_user: str = Depends(verify_token),
):
    """打包单事件的 omni_trace + clips + metadata 到本地 tar.gz.

    后续上传服务就绪后,打包完成会自动上传.当前仅本地存储.
    """
    from miloco.admin import feedback_pack as _fb_mod

    uid = ""
    try:
        miot_proxy = get_manager().miot_proxy
        user_info = await miot_proxy.get_user_info()
        if user_info:
            uid = user_info.uid
    except Exception:
        logger.exception(
            "Failed to resolve miot uid for feedback pack; falling back to anonymous"
        )

    try:
        result = await asyncio.to_thread(
            _fb_mod.build_feedback_pack,
            event_id=body.event_id,
            error_types=body.error_types,
            feedback_text=body.feedback_text,
            include_gallery=body.include_gallery,
            uid=uid,
        )
    except _fb_mod.EventNotFoundError:
        raise HTTPException(status_code=404, detail="event not found")
    except _fb_mod.FeedbackPackError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return NormalResponse(
        code=0,
        message="ok",
        data={
            "event_id": body.event_id,
            "pack_path": result["path"],
            "pack_size_bytes": result["size_bytes"],
            "uploaded": False,
            "upload_key": None,
            "components": result["components"],
        },
    )


class RevealDirBody(BaseModel):
    path: str


@router.post(
    "/reveal-dir",
    summary="在系统文件管理器中打开指定目录",
    response_model=NormalResponse,
)
async def reveal_dir(
    body: RevealDirBody,
    current_user: str = Depends(verify_token),
):
    """macOS: open <dir>, Linux: xdg-open <dir>."""
    import platform
    from pathlib import Path

    from miloco.utils.paths import miloco_home

    dir_path = Path(body.path).resolve()
    allowed_root = (miloco_home() / "packs").resolve()
    if not dir_path.is_relative_to(allowed_root):
        raise HTTPException(status_code=403, detail="path outside allowed directory")
    if not dir_path.is_dir():
        raise HTTPException(status_code=404, detail="directory not found")

    system = platform.system()
    try:
        if system == "Darwin":
            cmd = ["open", str(dir_path)]
        else:
            cmd = ["xdg-open", str(dir_path)]
        await asyncio.to_thread(subprocess.run, cmd, timeout=5)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return NormalResponse(code=0, message="ok", data=None)


# ─── omni 模型配置(在「模型」页内读/写) ─────────────────────────────────────


def _mask_api_key(key: str) -> str:
    """打码 api_key:只回前 3 + … + 后 4 位,既能确认"配了哪把 key"又不泄漏全文。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "…" + key[-2:]
    return f"{key[:3]}…{key[-4:]}"


def _key_by_label(label: str, provided: str | None, *, base_url: str | None = None) -> str:
    """provided 非空用它;否则取该 label 档案(或当前生效配置)已存的 key。

    base_url 非 None 时,还要求档案里存的 base_url 与传入一致,否则不沿用 key
    (返 "" 让调用方走 no_key 分支)。这是防"跨 URL 复用凭证"—— 攻击者拿到
    admin token 后可以:
      - upsert:改档案 base_url 但 api_key 留空 → 沿用旧 key(可能是云 provider 真 key)
      - test / list_models:传新 base_url + 已存档案 label → 后端拿档案里的 key 送到新 URL
    这两条路径覆盖内网 SSRF (base_url 改成 127.0.0.1 / 169.254.169.254 / 内网 IP)
    和公网钓鱼 (base_url 改成攻击者控制的 endpoint) 两个场景;共同点是 key 跟着
    base_url 走。此参数强制"key 只能配合它当初被存进来的那个 URL 用"。

    自建 LLM 场景不受影响 —— 自建 provider 通常无鉴权,用户本来就传空 key,
    走的是"无 key"分支而非"沿用"分支。
    """
    if provided and provided.strip():
        return provided.strip()
    if not label:
        return ""
    m = get_settings().model

    def _url_matches(stored: str) -> bool:
        if base_url is None:
            return True  # caller 没提供 base_url = 无 URL 上下文,按老语义沿用
        return stored.rstrip("/") == base_url.rstrip("/")

    # 命中当前生效配置(含 label 为空、按展示 label 合成的「当前生效行」)→ 回退其 key。
    if m.omni.api_key and label in (m.omni.label, _active_display_label()):
        if _url_matches(m.omni.base_url):
            return m.omni.api_key
        return ""
    for p in m.omni_profiles:
        if p.label == label and p.api_key:
            if _url_matches(p.base_url):
                return p.api_key
            return ""
    return ""


def _active_display_label() -> str:
    """当前生效配置用于「列表展示 / 编辑 / 删除」的稳定 label。

    omni.label 可能为空(env 或手改 config.json 直填 key、未走 web 档案流程的态),此时
    回退为 ``model @ base_url`` —— 与前端档案命名一致,保证合成的「当前生效行」label 非空,
    可被编辑 / 测试 / 删除按 label 正确定位(否则空 label 会使 upsert 报 400、删除 was_active
    误判为 False 而静默无效)。仅在有 key 时有展示意义。
    """
    m = get_settings().model.omni
    return m.label or f"{m.model} @ {m.base_url}"


def _full_omni_payload() -> dict:
    """{active, profiles}：均 api_key 打码;profiles 标记哪套 active(按档案名 label 匹配)。

    当前生效配置(active)并不一定已存档进 omni_profiles —— 默认状态(omni_profiles 为空、
    omni 是默认 MiMo)或历史遗留场景下,active 不在档案列表里。此时若直接返回 profiles,
    前端列表就看不到「当前生效模型」(只有折叠态标题栏读 active 能看到),造成「配没配好」
    的困惑。故在 active 未出现在档案列表时,把它作为一条合成档案补到列表头部(标 active)。

    active 字段附带 health 子对象(见 spec §6.1),来自 omni 熔断器 snapshot;前端顶部横条
    与「模型」页 active 行的连接状态列均读此字段。
    """
    from dataclasses import asdict

    from miloco.perception.engine.omni.circuit_breaker import get_omni_circuit_breaker

    m = get_settings().model
    active = m.omni
    profiles = [
        {
            "label": p.label,
            "model": p.model,
            "base_url": p.base_url,
            "api_key_masked": _mask_api_key(p.api_key),
            "has_key": bool(p.api_key),
            "active": p.label == active.label,
        }
        for p in m.omni_profiles
    ]
    if active.api_key and not any(p["active"] for p in profiles):
        profiles.insert(
            0,
            {
                "label": _active_display_label(),
                "model": active.model,
                "base_url": active.base_url,
                "api_key_masked": _mask_api_key(active.api_key),
                "has_key": True,
                "active": True,
            },
        )
    health = asdict(get_omni_circuit_breaker().snapshot())
    return {
        "active": {
            "label": active.label,
            "model": active.model,
            "base_url": active.base_url,
            "api_key_masked": _mask_api_key(active.api_key),
            "has_key": bool(active.api_key),
            "health": health,
        },
        "profiles": profiles,
    }


def _profiles_as_dicts() -> list[dict]:
    return [
        {
            "label": p.label,
            "model": p.model,
            "base_url": p.base_url,
            "api_key": p.api_key,
        }
        for p in get_settings().model.omni_profiles
    ]


class OmniConfigBody(BaseModel):
    label: str  # 档案名 = 唯一 id(非空);base_url/api_key/model 都是它的可改属性
    base_url: str
    model: str
    api_key: str | None = None  # 留空 = 沿用该档案原 key(不被打码值覆盖)
    original_label: str | None = None  # 正在编辑的档案原名(支持改名/定位);None=新增
    activate: bool = True  # True=同时设为当前生效;False=只入列表(激活由 /activate 负责)


class OmniSelectBody(BaseModel):
    """按档案名(label)定位一套档案。"""

    label: str


@router.get(
    "/omni-config",
    summary="读取 omni 配置(当前生效 active + 已存档案 profiles，api_key 打码)",
    response_model=NormalResponse,
)
def get_omni_config(current_user: str = Depends(verify_token)):
    return NormalResponse(code=0, message="ok", data=_full_omni_payload())


@router.put(
    "/omni-config",
    summary="保存一套 omni 配置(upsert 档案;activate=true 时设为当前，默认 true)",
    response_model=NormalResponse,
)
async def put_omni_config(
    body: OmniConfigBody, current_user: str = Depends(verify_token)
):
    """保存(新增/更新)一套档案到列表。

    - 档案名(label)= 唯一 id,非空;base_url / api_key / model 均为该档案可改属性。
    - ``original_label`` 标识正在编辑的档案(支持改名);为空表示新增。
    - ``api_key`` 留空 = 沿用该档案原 key(不被打码值覆盖)。
    - 重名(label 与"别的"档案相同)→ 409。
    - ``activate``=true(默认)同时设为当前生效;false 只入列表、不切换当前(激活走
      ``/activate``,即列表的「启用」)。但**正在编辑的就是当前生效那套时**,无论 activate
      与否都同步刷新 ``model.omni``,使改 key/model 即时对运行中的感知生效。
    - 若本次会写 ``model.omni``(激活或编辑当前生效那套),落盘前先跑 preflight
      (``_probe.probe_omni``),失败返 400——避免任何绕过 web「测试连接」的调用方(CLI/curl)
      把未校验配置写进运行时。
    - 写 config.json,感知下个推理周期热生效。env ``MILOCO_MODEL__OMNI__*`` 优先级更高会盖过。
    """
    label = body.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="档案名不能为空")
    base_url = body.base_url.strip()
    model = body.model.strip()
    orig = (body.original_label or "").strip()
    profiles = _profiles_as_dicts()
    target = next((p for p in profiles if p["label"] == orig), None) if orig else None
    clash = next((p for p in profiles if p["label"] == label and p is not target), None)
    if clash:
        raise HTTPException(status_code=409, detail=f"档案名「{label}」已存在")
    # 传 base_url 让 _key_by_label 校验"URL 未变才沿用旧 key",防跨 URL 复用凭证。
    key = _key_by_label(orig or label, body.api_key, base_url=base_url)
    entry = {"label": label, "base_url": base_url, "model": model, "api_key": key}
    tgt = orig or label
    will_activate = body.activate or _label_is_active(tgt)
    if will_activate:
        if not key:
            raise HTTPException(
                status_code=400, detail={"code": "no_key", "message": "未配置 API Key"}
            )
        result = await _probe.probe_omni(model, base_url, key)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result)
    if target:
        profiles[profiles.index(target)] = entry
    else:
        profiles.append(entry)
    update: dict = {"omni_profiles": profiles}
    if will_activate:
        update["omni"] = entry
    update_shared_config(model=update)
    if will_activate:
        # preflight 通过 = 新配置已验可用,主动把熔断状态清掉。之前 OPEN_CONFIG (bad_key
        # 之类) 时 before_call 短路一切,omni_client 里的 _maybe_reset_breaker_on_config_change
        # 只在真正调 omni 时才触发,永远等不到,用户改完 key 仍要手动点 retry 才恢复。
        from miloco.perception.engine.omni.circuit_breaker import (
            get_omni_circuit_breaker,
        )

        await get_omni_circuit_breaker().reset_on_config_change()
    return NormalResponse(code=0, message="ok", data=_full_omni_payload())


@router.post(
    "/omni-config/activate",
    summary="切换当前生效配置为某套已存档案(激活前跑 preflight)",
    response_model=NormalResponse,
)
async def activate_omni_config(
    body: OmniSelectBody, current_user: str = Depends(verify_token)
):
    """激活前跑 preflight;失败返 400 + 错误码。"""
    label = body.label.strip()
    for p in get_settings().model.omni_profiles:
        if p.label == label:
            if not p.api_key:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "no_key", "message": "未配置 API Key"},
                )
            result = await _probe.probe_omni(p.model, p.base_url, p.api_key)
            if not result.get("ok"):
                raise HTTPException(status_code=400, detail=result)
            update_shared_config(
                model={
                    "omni": {
                        "label": p.label,
                        "model": p.model,
                        "base_url": p.base_url,
                        "api_key": p.api_key,
                    }
                }
            )
            # 同 upsert 路径:preflight 通过后主动清熔断状态,避免 OPEN_CONFIG 卡死。
            from miloco.perception.engine.omni.circuit_breaker import (
                get_omni_circuit_breaker,
            )

            await get_omni_circuit_breaker().reset_on_config_change()
            return NormalResponse(code=0, message="ok", data=_full_omni_payload())
    raise HTTPException(status_code=404, detail="档案不存在")


def _label_is_active(label: str) -> bool:
    """label 是否指向当前生效配置(含空 label 当前生效的合成展示 label)。

    刻意返回 bool:PUT/DELETE/DEACTIVATE 三处调用只需「是不是当前生效」,不区分命中的是真
    label 还是合成展示 label;暂不为该区分(如审计)引入更复杂的身份判定,避免过早抽象。
    """
    omni = get_settings().model.omni
    return bool(label) and (
        label == omni.label or (bool(omni.api_key) and label == _active_display_label())
    )


async def _soft_stop_best_effort(action: str) -> None:
    """重置当前生效配置后软停感知:关引擎 + 降回 no_omni_api_key,保留 tick 自愈循环。
    best-effort —— 配置落盘是主操作,软停失败仅告警(下次后端重启生效),不阻断整体。"""
    try:
        await manager.perception_service.stop_to_unconfigured()
    except Exception as e:  # noqa: BLE001
        logger.warning("%s当前生效模型后软停感知失败(将于重启后生效): %s", action, e)


@router.post(
    "/omni-config/delete",
    summary="删除一套已存档案;删的是当前生效那套时,回到「未配模型」态并软停感知",
    response_model=NormalResponse,
)
async def delete_omni_config(
    body: OmniSelectBody, current_user: str = Depends(verify_token)
):
    """删除一套档案。删的若是当前生效模型,则把当前生效配置重置为「未配」(清空 key)并软停
    感知 —— 等价于回到初始未配模型态:感知停下,等重新配置并启用模型后由 tick 自愈自动拉起。
    """
    from miloco.config.settings import OmniModelSettings

    label = body.label.strip()
    was_active = _label_is_active(label)
    profiles = [p for p in _profiles_as_dicts() if p["label"] != label]
    update: dict = {"omni_profiles": profiles}
    if was_active:
        # 删当前生效模型 → 当前生效配置重置为出厂未配态(MiMo 默认 + 空 key)。
        update["omni"] = OmniModelSettings().model_dump()
    update_shared_config(model=update)
    if was_active:
        await _soft_stop_best_effort("删除")
    return NormalResponse(code=0, message="ok", data=_full_omni_payload())


@router.post(
    "/omni-config/deactivate",
    summary="停用当前生效模型:回到「未配模型」态并软停感知,但保留所有档案(可再启用)",
    response_model=NormalResponse,
)
async def deactivate_omni_config(
    body: OmniSelectBody, current_user: str = Depends(verify_token)
):
    """停用当前生效模型:当前生效配置重置为「未配」(清空 key)+ 软停感知,但**不删除档案**。
    与 delete 的区别:delete 会移除该档案,deactivate 仅停用、档案保留,可随后再「启用」恢复。
    """
    from miloco.config.settings import OmniModelSettings

    if _label_is_active(body.label.strip()):
        update_shared_config(model={"omni": OmniModelSettings().model_dump()})
        await _soft_stop_best_effort("停用")
    return NormalResponse(code=0, message="ok", data=_full_omni_payload())


class OmniTestBody(BaseModel):
    # 皆可省略 —— 省略则回退当前生效配置;无 key 时按 label 取该档案已存 key。
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    label: str | None = None


@router.post(
    "/omni-config/test",
    summary="测试 omni 配置连通性（OpenAI 兼容族两阶段预检+真校验 / 原生协议单阶段 chat 探测，max_tokens=1 极少量 token，不写库、不计入 miloco 用量统计）",
    response_model=NormalResponse,
)
async def test_omni_config(
    body: OmniTestBody, current_user: str = Depends(verify_token)
):
    """用表单值（缺省回退当前已保存配置）探测配置可用性。

    OpenAI 兼容族（MiMo/Qwen）两阶段：先 GET /models 验鉴权/可达，再发一次 max_tokens=1 的
    极简 chat 真正验证该模型可用；非 OpenAI 兼容族（Gemini 等原生协议）没有等价 GET /models
    预检语义，直接走 adapter 化的 chat 探测。消耗极少量 token，不计入 miloco 用量统计。
    返回 {ok, code, status, latency_ms, message}。"""
    omni = get_settings().model.omni
    model = (body.model or omni.model).strip()
    base_url = (body.base_url or omni.base_url).strip()
    # base_url 传入 _key_by_label:test 端点不写盘,是隐蔽性最高的钓鱼跳板——攻击者
    # 只需一次调用就能让后端把已存的真 key 送到攻击者的 base_url。校验 URL 一致才沿用。
    api_key = _key_by_label(
        (body.label or omni.label or "").strip(),
        body.api_key,
        base_url=base_url,
    )
    if not api_key:
        return NormalResponse(
            code=0,
            message="ok",
            data={"ok": False, "code": "no_key", "message": "未配置 API Key"},
        )
    result = await _probe.probe_omni(model, base_url, api_key)
    # 测通 + 三元组精确匹配当前 active + 熔断非 ok → 主动清熔断,与 put/activate/retry
    # 恢复路径对齐。护栏:测别的档案 / 未保存的新配置时不动状态。
    # OPEN_CONFIG 下 tick 不会自动探测(只探 OPEN_RECOVERABLE),不清则用户测通了红条仍不消失,
    # 只能靠横条上的「立即重试」或改配置重存才能恢复——「测通即恢复」是最直觉的路径。
    if result.get("ok"):
        from miloco.perception.engine.omni.circuit_breaker import (
            get_omni_circuit_breaker,
        )
        from miloco.perception.engine.omni.omni_client import resolve_omni_api_key

        live = get_settings().model.omni
        live_key = resolve_omni_api_key(live.api_key)
        tested_is_active = (
            model == live.model
            and base_url.rstrip("/") == live.base_url.rstrip("/")
            and api_key == live_key
        )
        if tested_is_active:
            cb = get_omni_circuit_breaker()
            if cb.snapshot().state != "ok":
                await cb.reset_on_config_change()
    return NormalResponse(code=0, message="ok", data=result)


class OmniModelsBody(BaseModel):
    base_url: str
    api_key: str | None = None
    label: str | None = None


@router.post(
    "/omni-config/models",
    summary="拉取某 Base URL 下可用模型列表(供模型下拉)",
    response_model=NormalResponse,
)
async def list_omni_models(
    body: OmniModelsBody, current_user: str = Depends(verify_token)
):
    """用 base_url + key(留空则按 label 取该档案已存 key)请求 GET /models,返回模型 id 列表。"""
    base_url = body.base_url.strip()
    # 同 test 端点:传 base_url 校验 URL 一致才沿用旧 key,防用已存 key 拉取攻击者 URL 的 /models。
    api_key = _key_by_label((body.label or "").strip(), body.api_key, base_url=base_url)
    if not api_key:
        # URL 本身错优先于「缺 key」暴露:无 key 时先探可达性,连不上→报 URL 错;能连上才报缺 key。
        reach = await _probe.probe_reachable(base_url)
        if reach is not None:
            return NormalResponse(
                code=0,
                message="ok",
                data={
                    "ok": False,
                    "code": reach["code"],
                    "models": [],
                    "message": reach["message"],
                },
            )
        return NormalResponse(
            code=0,
            message="ok",
            data={
                "ok": False,
                "code": "no_key",
                "models": [],
                "message": "未配置 API Key",
            },
        )
    return NormalResponse(
        code=0, message="ok", data=await _probe.fetch_models(base_url, api_key)
    )


@router.post(
    "/omni-config/retry",
    summary="用户主动触发一次 omni 探测,跳过熔断剩余 backoff",
    response_model=NormalResponse,
)
async def retry_omni_probe(current_user: str = Depends(verify_token)):
    """用户点「立即重试」时调:
    - CLOSED: no-op,返回当前 health
    - OPEN_RECOVERABLE / OPEN_CONFIG / HALF_OPEN: 跑一次 probe_omni,成功回 CLOSED。
    """
    from miloco.perception.engine.omni.circuit_breaker import (
        RETRY_COOLDOWN_SEC,
        CircuitState,
        get_omni_circuit_breaker,
    )
    from miloco.perception.engine.omni.error_classifier import (
        ClassifiedError,
        ErrorCategory,
    )

    cb = get_omni_circuit_breaker()
    if cb.state_for_test() == CircuitState.CLOSED:
        return NormalResponse(code=0, message="ok", data=_full_omni_payload())

    # HALF_OPEN 说明已有 tick 自愈或上次 retry 触发的 probe 在飞,不重复发。冷却期
    # 兜的是 `last_probe_at` 时间差,拦不住「探测中」——tick arm 后 state=HALF_OPEN
    # 且 last_probe_at 仍是上一次完成时刻,冷却已过,retry_now 对 HALF_OPEN 又是 no-op,
    # 会绕过所有拦截并发第二次 probe,两次 record_probe_result 相互覆盖导致横条闪跳。
    #
    # probe_in_flight 补 tick 已 arm 但尚未 mark_half_open 的窗口:此时 state 仍是
    # OPEN_RECOVERABLE 但 _probe_in_flight 已 True,只判 state 会漏。
    if cb.state_for_test() == CircuitState.HALF_OPEN or cb.probe_in_flight():
        return NormalResponse(code=0, message="ok", data=_full_omni_payload())

    # 冷却期内(距上次 probe 完成不足 RETRY_COOLDOWN_SEC)不发新 probe,防 UI
    # 反复点 / 脚本 curl 打爆 provider。静默返当前 snapshot——前端已在按钮层做本地
    # 冷却置灰(值同源自 health.retry_cooldown_sec),用户不需要 toast 提示;后端
    # 这里仍是硬阻,即使脚本绕过 UI 也拦得住。
    snap = cb.snapshot()
    if snap.last_probe_at_ms is not None:
        import time as _time

        elapsed_ms = int(_time.time() * 1000) - snap.last_probe_at_ms
        if elapsed_ms < int(RETRY_COOLDOWN_SEC * 1000):
            return NormalResponse(code=0, message="ok", data=_full_omni_payload())

    await cb.retry_now()
    omni = get_settings().model.omni
    if not omni.api_key:
        # 无 key:直接标记 probe 失败,回 OPEN_CONFIG
        await cb.record_probe_result(
            False,
            ClassifiedError(
                "no_key",
                "未配置 API Key",
                ErrorCategory.CONFIG,
            ),
        )
        return NormalResponse(code=0, message="ok", data=_full_omni_payload())

    try:
        result = await _probe.probe_omni(omni.model, omni.base_url, omni.api_key)
    except asyncio.CancelledError:
        # 客户端断开 HTTP(用户切页/关 tab/网络抖动)时 FastAPI 抛 CancelledError。
        # 此前 retry_now() 已把 state 置 HALF_OPEN,若不复位则 before_call 永久短路、
        # tick 只 arm OPEN_RECOVERABLE 也不会驱动新 probe,只能改配置或重启。
        # 走 record_probe_result(fail, RECOVERABLE) 回落到 OPEN_RECOVERABLE 让 tick 接管。
        await cb.record_probe_result(
            False,
            ClassifiedError(
                "cancelled",
                "重试被中断",
                ErrorCategory.RECOVERABLE,
            ),
        )
        raise
    if result.get("ok"):
        await cb.record_probe_result(True, None)
    else:
        code = result.get("code", "unreachable")
        cat = (
            ErrorCategory.CONFIG
            if code in ("bad_key", "not_found", "rejected_authed")
            else ErrorCategory.RECOVERABLE
        )
        await cb.record_probe_result(
            False,
            ClassifiedError(
                code,
                result.get("message", ""),
                cat,
                # rate_limited 时 probe_chat 会在 result 里回带 retry_after_seconds,
                # 传给 _grow_backoff_locked 让 backoff 尊重 server Retry-After。
                result.get("retry_after_seconds"),
            ),
        )
    return NormalResponse(code=0, message="ok", data=_full_omni_payload())


@router.get(
    "/omni-config/stream",
    summary="SSE 流:omni 熔断器状态变化时推送 omni_health 事件",
    dependencies=[Depends(verify_token_query_fallback)],
)
async def omni_health_stream():
    """复用 pipeline._sse_subscribers 广播通道;generator 过滤 event_type=='omni_health'。
    鉴权支持 Authorization header 或 ?token=... query(EventSource 无法传 header)。
    """
    pipeline = manager.perception_service._pipeline
    q = pipeline.subscribe_sse()

    async def event_generator():
        try:
            # 首次连上立刻推一次当前状态,让 web 拿到初始态
            from dataclasses import asdict

            from miloco.perception.engine.omni.circuit_breaker import (
                get_omni_circuit_breaker,
            )

            initial = asdict(get_omni_circuit_breaker().snapshot())
            yield {
                "event": "omni_health",
                "data": json.dumps(initial, ensure_ascii=False),
            }
            while True:
                event_type, data = await q.get()
                if event_type != "omni_health":
                    continue
                yield {
                    "event": "omni_health",
                    "data": json.dumps(data, ensure_ascii=False),
                }
        except asyncio.CancelledError:
            pass
        finally:
            pipeline.unsubscribe_sse(q)

    return EventSourceResponse(event_generator())


# =============================================================================
# 感知参数配置
# =============================================================================


class PerceptionConfigBody(BaseModel):
    video_short_edge: int | None = Field(default=None, ge=64, le=2160)
    omni_fps: int | None = Field(default=None, ge=1, le=30)
    window_size: int | None = Field(default=None, ge=1, le=60)


def _perception_config_payload() -> dict:
    s = get_settings()
    inp = s.perception.engine.get("input", {})
    return {
        "video_short_edge": inp.get("video_short_edge", 512),
        "omni_fps": inp.get("omni_fps", 1),
        "window_size": s.perception.collect.window_size,
    }


@router.get(
    "/perception-config",
    summary="获取当前感知参数",
    response_model=NormalResponse,
)
def get_perception_config(current_user: str = Depends(verify_token)):
    return NormalResponse(code=0, message="ok", data=_perception_config_payload())


@router.put(
    "/perception-config",
    summary="修改感知参数（写 config.json 并重启感知引擎使其生效）",
    response_model=NormalResponse,
)
async def put_perception_config(body: PerceptionConfigBody, current_user: str = Depends(verify_token)):
    update: dict = {}
    if body.video_short_edge is not None:
        update.setdefault("perception", {}).setdefault("engine", {}).setdefault("input", {})["video_short_edge"] = body.video_short_edge
    if body.omni_fps is not None:
        update.setdefault("perception", {}).setdefault("engine", {}).setdefault("input", {})["omni_fps"] = body.omni_fps
    if body.window_size is not None:
        update.setdefault("perception", {}).setdefault("collect", {})["window_size"] = body.window_size
    payload = _perception_config_payload()
    if update:
        # 三个参数生效路径各不同，按「新值 != 旧值」判断（前端 drawer 三字段一起 PUT）：
        #   - video_short_edge：每帧实时读 settings，写盘 + reset_settings 后下帧即生效，无需重启。
        #   - omni_fps：pipeline 每窗现读引擎内存 config.input.omni_fps（非 settings），但它经
        #     adjust_fps_for_omni 顶起的 tracker fps 有构造期派生缓存——走 apply_omni_fps_live
        #     运行时热更（原地刷 _config + 缓存），免重建引擎 / 免模型重载 / 不丢 track。
        #   - window_size：runner 构造时 cache，需 stop→start 重读（apply_config_restart）。
        omni_fps_changed = body.omni_fps is not None and body.omni_fps != payload["omni_fps"]
        window_changed = body.window_size is not None and body.window_size != payload["window_size"]
        update_shared_config(**update)
        payload = _perception_config_payload()
        # config 已写盘(不可回滚)；热更/重启失败仅带 restart_ok=False，不冒泡成 500——
        # 否则前端会把「已保存+失败」误报成「保存失败」，误导用户以为改动丢失。同步等完成再返回。
        restart_ok = True
        if omni_fps_changed:
            restart_ok = await manager.perception_service.apply_omni_fps_live(body.omni_fps) and restart_ok
        if window_changed:
            restart_ok = await manager.perception_service.apply_config_restart() and restart_ok
        if omni_fps_changed or window_changed:
            payload["restart_ok"] = restart_ok
    return NormalResponse(code=0, message="ok", data=payload)
