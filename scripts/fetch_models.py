#!/usr/bin/env python3
"""感知 ONNX 模型下载器：从 GitHub Release 拉取 + sha256 校验（纯标准库）。

模型不再进 git —— 78MB 二进制躺在历史里，每次 CI ``actions/checkout``（默认浅克隆）
都要白付一遍流量，且以后每次换模型都在历史里永久叠一份。改为托管在固定 tag ``models``
的 Release 资产，由本脚本按 ``scripts/models.lock.json`` 锁定的 sha256 拉取。

**只用标准库**：本脚本被 ``scripts/build.sh``、CI、``plugins/hermes/install-hermes.sh``
直接以 ``python3`` 调用，多一个第三方依赖就多一处装不上的可能。面向终端用户的大包下载
仍走 ``scripts/install.py`` 的 httpx 实现（那边已在 uv 环境里），两者互不影响。

用法:
  python3 scripts/fetch_models.py                     # 下载缺失/损坏的模型到包内 models/
  python3 scripts/fetch_models.py --check             # 只校验不下载（CI 门禁）
  python3 scripts/fetch_models.py --dest DIR          # 下到指定目录（如 $MILOCO_HOME/models）
  python3 scripts/fetch_models.py --only det_4C.onnx  # 只处理指定文件（可重复）
  python3 scripts/fetch_models.py --required-only     # 跳过可选模型（省 ~25MB）
  python3 scripts/fetch_models.py --force             # 无条件重下

环境变量:
  MILOCO_MODELS_BASE_URL  覆盖下载源（内网镜像 / 离线源）。是**独占替换**而非"排在前面"：
                          设了它就只用它，lock 的 base_url + mirrors 全部不再兜底。
                          允许 http:// 与 file://（内网/离线场景），内容仍按 sha256 校验。
  MILOCO_MODELS_DEST      覆盖下载目标目录（低于 --dest）。注意：只影响"下到哪"，
                          不影响运行时模型解析口径（那是 directories.models /
                          MILOCO_DIRECTORIES__MODELS，见 config/settings.py）

退出码: 0 = 必需模型全部就绪 | 1 = 必需模型缺失或校验失败 | 2 = 用法 / lock 文件错误
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPTS_DIR.parent
_DEFAULT_LOCK = _SCRIPTS_DIR / "models.lock.json"
_DEFAULT_DEST = (
    _PROJECT_ROOT / "backend" / "miloco" / "src" / "miloco" / "perception" / "models"
)

_CHUNK = 256 * 1024
_MAX_RETRIES = 3
_TIMEOUT = 30.0


# ─── 小工具 ──────────────────────────────────────────────────────────────────


def _log(msg: str, *, quiet: bool = False) -> None:
    # 一律走 stderr：stdout 留给未来可能的机器可读输出，且不污染调用方的管道。
    if not quiet:
        print(msg, file=sys.stderr, flush=True)


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GiB"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_ready(path: Path, spec: dict[str, Any]) -> bool:
    """文件已存在且内容正确。size 不符就不必算 hash（78MB 全量 hash 不便宜）。"""
    if not path.is_file():
        return False
    if spec.get("size") and path.stat().st_size != spec["size"]:
        return False
    return _sha256(path) == spec["sha256"]


def _required(spec: dict[str, Any]) -> bool:
    """缺 ``required`` 键时 fail-closed 当必需。

    fail-open 的话，lock 少写一个键就把必需模型悄悄降级成"缺了也算成功"，
    而这条链路上所有硬失败（build 的 --strict、CI 门禁）都是靠 required 判的。
    """
    return bool(spec.get("required", True))


def _check_name(name: str) -> None:
    """模型名要直接拼进 URL 和 dest 路径，不许带路径分隔符。

    信任边界本是"lock 在仓库里"，但 ``--lock`` 可以指任意文件，而
    ``Path(dest) / "../../evil"`` 会静静逃出 dest。多一句守卫比多一次事故便宜。
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"lock 里的文件名不合法（不能为空 / 含路径分隔符 / 以点开头）：{name!r}")


def _sources(lock: dict[str, Any]) -> list[str]:
    """下载源：env 独占替换，否则 lock 的 base_url（GitHub 直连）+ mirrors（加速镜像）。

    ``MILOCO_MODELS_BASE_URL`` 是**独占**的 —— 设了它就只用它，不再拼 lock 里的源。
    内网/离线场景要的正是"别再去碰外网"，"优先 + 兜底"会把请求漏到公网去。

    mirrors 与 ``scripts/manifest.json`` 的 ``download.sites`` 是同一批 GitHub 加速站
    （终端用户下大包用那份，开发者/CI 下模型用这份），两边由 test_fetch_models.py 钉死一致。
    """
    env = os.environ.get("MILOCO_MODELS_BASE_URL", "").strip()
    if env:
        return [env.rstrip("/")]
    urls = [lock.get("base_url", ""), *lock.get("mirrors", [])]
    return [u.rstrip("/") for u in urls if u]


# ─── 下载 ────────────────────────────────────────────────────────────────────


def _declared_length(resp: Any) -> int | None:
    """本次响应体应有的字节数（206 时是本段长度，非整文件长度）。

    chunked 传输 / 老式代理不给 Content-Length，此时返回 None（无从判断截断，
    只能退回 sha256 兜底）。

    走 ``.headers`` 而不是 ``.getheader()``：后者只有 ``HTTPResponse`` 有，
    ``file://`` 拿到的 ``addinfourl`` 没有，用了会 AttributeError。
    """
    headers = getattr(resp, "headers", None)
    raw = headers.get("Content-Length") if headers is not None else None
    if raw is None:
        return None
    try:
        n = int(raw.strip())
    except (AttributeError, ValueError):
        return None
    return n if n >= 0 else None


def _open_part(dest_dir: Path, name: str) -> tuple[Path, Any]:
    """选定 ``.part`` 路径并尽量独占它，返回 (路径, 需要保活的锁句柄或 None)。

    稳定的 ``{name}.part`` 是 Range 续传的载体，但多进程共享同一 dest
    （self-hosted 共享 workspace、``install-hermes.sh --post-install`` 与 build 并行）
    会互相截断。这里用 flock 抢：抢到就用稳定名（可跨轮续传），抢不到就退到
    ``{name}.{pid}.part`` —— 本轮不续传，但绝不踩别人的字节。
    直接用 pid 名会更简单，代价是永久放弃续传，而续传正是 78MB 跨境链路的关键。
    """
    stable = dest_dir / f"{name}.part"
    try:
        import fcntl  # Windows 上没有；拿不到就退化成"无锁但仍可续传"的旧行为
    except ImportError:
        return stable, None

    fh = None
    try:
        fh = open(stable, "a+b")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if fh is not None:
            fh.close()
        return dest_dir / f"{name}.{os.getpid()}.part", None
    return stable, fh


def _stream(url: str, part: Path, expected_size: int | None, *, quiet: bool) -> None:
    """流式下载到 ``part``；``part`` 已有内容时尝试 Range 续传（78MB 跨境链路值得）。

    服务端不接 Range（返回 200）或 ``file://`` 这种无 Range 语义的源时整段重写。
    续传拼出脏内容不怕：调用方一律以 sha256 判定，不符就删掉 part 从头再来。

    收尾会比对实际写入量与响应自报的 ``Content-Length``：``http.client`` 对"没读满就
    EOF"是静默返回 ``b""``（HTTPS 下 ``suppress_ragged_eofs`` 同理），不比就会把干净
    FIN 截断误报成"sha256 不符" —— 而那条路径会删掉 part、下一次不带 Range 从 0 重来，
    跨境链路上每次断在同一位置就成了永远下不完、还把诊断指向"镜像被投毒"。
    比的是响应头而不是 lock 的 ``expected_size``：lock 陈旧时实收会**大于**期望值，
    拿 lock 比会打出反向误导并留下超长 part。
    """
    offset = part.stat().st_size if part.is_file() else 0
    if offset and expected_size and offset >= expected_size:
        # 已有内容不小于目标 → 是脏文件（上次写坏 / 换了资产），从头下
        part.unlink()
        offset = 0

    headers = {"User-Agent": "miloco-fetch-models"}
    if offset and urllib.parse.urlparse(url).scheme in ("http", "https"):
        headers["Range"] = f"bytes={offset}-"

    # URL 由仓库内 lock 的 base_url/mirrors 拼出（或维护者显式给的 MILOCO_MODELS_BASE_URL），
    # 不来自任何不可信输入；内容再经 sha256 比对，源被换掉也下不出错东西。
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        if getattr(resp, "status", 200) != 206:
            offset = 0  # 没走成续传 → 整段重写
        declared = _declared_length(resp)
        start = offset
        written = offset
        total = expected_size or 0
        show_pct = total > 0 and not quiet and sys.stderr.isatty()
        last_pct = -5
        with open(part, "ab" if offset else "wb") as f:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                if show_pct:
                    pct = written * 100 // total
                    if pct >= last_pct + 5:
                        last_pct = pct
                        print(
                            f"\r      {pct:3d}%  {_human(written)}",
                            end="",
                            file=sys.stderr,
                            flush=True,
                        )
        if show_pct:
            print("\r" + " " * 32 + "\r", end="", file=sys.stderr, flush=True)

        if declared is not None and written - start != declared:
            # 抛异常而不是 return：调用方的 except 分支只记日志、不删 part，
            # 已落地的字节因此得以保留，下一次才真的能带 Range 接着下。
            raise OSError(
                f"连接提前关闭：本段收到 {written - start} 字节，"
                f"响应自报 {declared} 字节（已保留 {written} 字节待续传）"
            )


def _fetch_one(
    spec: dict[str, Any],
    dest_dir: Path,
    urls: list[str],
    *,
    force: bool,
    quiet: bool,
) -> bool:
    name = spec["name"]
    path = dest_dir / name

    if not force and _is_ready(path, spec):
        _log(f"  ✓ {name}  {_human(spec.get('size', 0))}  已就绪", quiet=quiet)
        return True

    part, lock_fh = _open_part(dest_dir, name)
    quoted = urllib.parse.quote(name)
    try:
        for url in urls:
            host = urllib.parse.urlparse(url).netloc or url
            for attempt in range(1, _MAX_RETRIES + 1):
                try:
                    _log(
                        f"  ↓ {name}  {_human(spec.get('size', 0))}  ← {host}"
                        + (f"（第 {attempt}/{_MAX_RETRIES} 次）" if attempt > 1 else ""),
                        quiet=quiet,
                    )
                    _stream(f"{url}/{quoted}", part, spec.get("size"), quiet=quiet)
                    got = _sha256(part)
                    if got == spec["sha256"]:
                        part.replace(path)  # 原子改名：中断不会留下"看起来能用"的半个文件
                        _log(f"  ✓ {name}  校验通过", quiet=quiet)
                        return True
                    _log(
                        f"  ! {name}  sha256 不符（期望 {spec['sha256'][:12]}…，"
                        f"实得 {got[:12]}…），丢弃重下",
                        quiet=quiet,
                    )
                    part.unlink(missing_ok=True)
                except (urllib.error.URLError, OSError, TimeoutError) as exc:
                    # 故意不删 part：截断/超时留下的字节是下一轮 Range 续传的起点。
                    _log(f"  ! {name}  下载失败（{attempt}/{_MAX_RETRIES}）：{exc}", quiet=quiet)
                if attempt < _MAX_RETRIES:
                    time.sleep(2 ** (attempt - 1))

        # 所有源都试完仍未就绪：稳定名的 .part **不删** —— 它正是下一次调用带 Range
        # 续传的起点，而这恰恰是本文件为之付了 flock 复杂度的那条路径。删掉的话
        # "跨调用续传"就只在 Ctrl-C 时有效（KeyboardInterrupt 不在上面那个 except 里，
        # 也走不到这儿），而真正需要它的场景——限速链路每次断在同一位置——反而每次
        # 从 0 重来，表现成"重跑多少次都不涨"。
        # "内容确定是错的"那条路径（sha256 不符）上面已经自己删过了，不会攒垃圾。
        # pid 名的那份本轮就注定不参与续传（见 _open_part），删掉免得越攒越多。
        left = part.stat().st_size if part.is_file() else 0
        if part.name == f"{name}.part" and left:
            _log(f"  · {name}  已保留 {_human(left)} 待续传（{part.name}）", quiet=quiet)
        else:
            # 空文件没有续传价值（_open_part 用 "a+b" 开稳定名，即便一个字节都没下到
            # 也会留下 0 字节的壳），一并清掉。
            part.unlink(missing_ok=True)
        return False
    finally:
        if lock_fh is not None:
            lock_fh.close()


# ─── 主流程 ──────────────────────────────────────────────────────────────────


def _select(files: list[dict[str, Any]], only: list[str], required_only: bool) -> list[dict[str, Any]]:
    specs = files
    if only:
        want = set(only)
        unknown = want - {s["name"] for s in files}
        if unknown:
            raise ValueError(f"--only 指定了 lock 里没有的文件：{', '.join(sorted(unknown))}")
        specs = [s for s in files if s["name"] in want]
    if required_only:
        specs = [s for s in specs if _required(s)]
    if not specs:
        # `--only <可选模型> --required-only` 会选出空集，空循环再 exit 0 就是
        # "什么都没做却报成功"——CI 用它当门禁时等于门禁被静默摘掉。
        raise ValueError("选出的文件集为空（--only 与 --required-only 无交集？），无事可做")
    return specs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="下载并校验感知 ONNX 模型（源与 sha256 见 scripts/models.lock.json）"
    )
    ap.add_argument("--dest", default=None, metavar="DIR", help="目标目录（默认包内 perception/models/）")
    ap.add_argument("--lock", default=None, metavar="FILE", help="lock 文件路径（默认 scripts/models.lock.json）")
    ap.add_argument("--only", action="append", default=[], metavar="NAME", help="只处理指定文件（可重复）")
    ap.add_argument("--required-only", action="store_true", help="跳过可选模型")
    ap.add_argument("--force", action="store_true", help="无条件重下（忽略本地已就绪的文件）")
    ap.add_argument("--check", action="store_true", help="只校验不下载：缺任一必需模型即 exit 1")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="可选模型缺失也算失败（打 release tarball 时用：终端用户要拿到完整能力）",
    )
    ap.add_argument("--quiet", action="store_true", help="静默（仍会打印错误）")
    args = ap.parse_args(argv)

    lock_path = Path(args.lock) if args.lock else _DEFAULT_LOCK
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        files: list[dict[str, Any]] = lock["files"]
        for spec in files:
            _check_name(spec["name"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"lock 文件不可用（{lock_path}）：{exc}", file=sys.stderr)
        return 2

    try:
        specs = _select(files, args.only, args.required_only)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    dest = Path(args.dest or os.environ.get("MILOCO_MODELS_DEST") or _DEFAULT_DEST).expanduser()

    if args.check:
        _log(f"校验模型目录：{dest}", quiet=args.quiet)
        bad_required, bad_optional = [], []
        for spec in specs:
            if _is_ready(dest / spec["name"], spec):
                _log(f"  ✓ {spec['name']}", quiet=args.quiet)
            else:
                _log(f"  ✗ {spec['name']}  缺失或校验不通过", quiet=args.quiet)
                (bad_required if _required(spec) else bad_optional).append(spec["name"])
        blocking = bad_required + (bad_optional if args.strict else [])
        if blocking:
            print(
                f"缺少模型：{', '.join(blocking)}\n"
                f"补齐：python3 scripts/fetch_models.py --dest {dest}",
                file=sys.stderr,
            )
            return 1
        if bad_optional:
            _log(f"  可选模型缺失（对应功能降级，不阻塞）：{', '.join(bad_optional)}", quiet=args.quiet)
        return 0

    urls = _sources(lock)
    if not urls:
        print("lock 里没有可用下载源（base_url / mirrors 均为空）", file=sys.stderr)
        return 2

    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"目标目录不可写（{dest}）：{exc}", file=sys.stderr)
        return 2

    _log(f"准备感知模型 → {dest}", quiet=args.quiet)
    failed_required, failed_optional = [], []
    for spec in specs:
        if not _fetch_one(spec, dest, urls, force=args.force, quiet=args.quiet):
            (failed_required if _required(spec) else failed_optional).append(spec["name"])

    if failed_optional and not args.strict:
        # 与 resource_validator 的降级语义一致：可选模型缺了只是对应能力降级，不阻塞。
        _log(f"  可选模型未就绪（对应功能降级）：{', '.join(failed_optional)}", quiet=args.quiet)
    if args.strict:
        failed_required += failed_optional
    if failed_required:
        print(
            f"必需模型未就绪：{', '.join(failed_required)}\n"
            f"源：{' | '.join(urls)}（可用 MILOCO_MODELS_BASE_URL 换源，或手动放置到 {dest}）",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
