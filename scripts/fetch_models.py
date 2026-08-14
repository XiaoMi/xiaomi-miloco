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
  python3 scripts/fetch_models.py --strict            # 可选模型缺失也算失败
  python3 scripts/fetch_models.py --force             # 无条件重下

关于 ``--strict``：判据的两头必须同强度。凡是拿本脚本的**退出码**当"齐没齐"唯一信号的
调用方，都得跟 ``--check`` 那侧的门禁用同一个强度 —— 否则会出现"用 ``--check --strict``
判不齐、进了补齐分支、补齐时不带 ``--strict``"这种两头错配：只有可选模型缺失时下载器退
**0**（主流程里 ``failed_optional`` 不并进 ``failed_required``），调用方的 ``if !`` 恒不
成立，一条告警都不打就往下走，而语义去重、VAD 已经静默降级了。全仓合法的"下载却不带
``--strict``"只有两处，且都不拿退出码当判据：``.github/workflows/ci.yml`` 那步（下载与门禁
刻意分成两步，紧随其后的 ``--check --strict`` 才是判据），以及 ``install-hermes.sh`` 拿旁边
checkout 当 ``file://`` 源做本地同步那步（退出码被 ``|| true`` 显式丢弃）。这条口径由
``test_every_fetch_caller_matches_its_own_gate_strength`` 钉住，改动前先看那条。

环境变量:
  MILOCO_MODELS_BASE_URL  覆盖下载源（内网镜像 / 离线源）。是**独占替换**而非"排在前面"：
                          设了它就只用它，lock 的 base_url + mirrors 全部不再兜底。
                          允许 http:// / https:// / file://，也允许直接给挂载目录的裸路径
                          （按 file:// 处理）；其余 scheme 当用法错误退 2。
                          内容一律仍按 lock 的 sha256 校验。
  MILOCO_MODELS_DEST      覆盖下载目标目录（低于 --dest）。注意：只影响"下到哪"，
                          不影响运行时模型解析口径（那是 directories.models /
                          MILOCO_DIRECTORIES__MODELS，见 config/settings.py）

退出码: 0 = 必需模型全部就绪 | 1 = 必需模型缺失或校验失败 | 2 = 用法 / lock 文件错误
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shlex
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
    """文件已存在且内容正确。**只认 sha256**，与 _fetch_one 落地时那次校验完全同源。

    曾经在这里先比一次 size 当快速否决，但那让本脚本的两条路径判据不一致：下载落地
    只看 sha，``--check`` 却先看 size。lock 里 size 与 sha256 描述的不是同一份字节时
    （手改 lock 敲错一位、换模型只更新了 sha），CI 上紧挨着的两步就会一绿一红——
    ``fetch_models.py --dest X`` 打"校验通过"退 0，``--check --strict --dest X``
    立刻判"缺失或校验不通过"退 1，而它给的修法正是刚跑成功的上一步。重跑多少次都一样，
    文案还把人指向 Release 和网络，真正坏的是 lock 自己。
    去掉之后省不了也不多花：happy path 上 size 本来就对，照样要算这一次 hash；size 不符
    才多算一次，而那时紧接着就要下几十 MB，一次 hash 是噪声。
    lock 的 size 字段仍有用（_stream 的脏文件守卫、进度显示），它自身对不对由
    publish_models.sh verify 拿线上资产的真实大小去对——那才是该报"size 不符"的地方。
    """
    if not path.is_file():
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
    # 类型先判：本函数与 _check_spec 的其余守卫同处一个 try，约定是"输入不合法一律
    # ValueError → 一行中文 + 退 2"。而 name 不是字符串时，`"/" in name` 对 list/dict
    # 这类支持 in 的类型不报错、径直落到 `name.startswith` 才抛 AttributeError ——
    # 那个异常不在读 lock 的捕获元组里，于是穿出去变成 traceback + 退 **1**。退 1 在
    # 本脚本的契约里是"必需模型缺失，重试可能有用"（install-hermes.sh 的四分支门禁正是
    # 这么读的），于是一个手写坏了的 lock 会把用户送进一个重试永远修不好的循环。
    # 与 _check_spec 里 size / required / sha256 那几条 isinstance 守卫同口径。
    if not isinstance(name, str):
        raise ValueError(f"lock 里的文件名必须是字符串：{name!r}")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"lock 里的文件名不合法（不能为空 / 含路径分隔符 / 以点开头）：{name!r}")


_HEX = frozenset("0123456789abcdefABCDEF")


def _check_spec(spec: dict[str, Any]) -> None:
    """把一条 lock 记录里"下载器真正依赖的键"在启动时一次校验掉。

    调用点在 main 读 lock 的那个 try 里，所以这里抛什么都会被收敛成"一行中文 + 退 2"。
    不在这儿拦的键会带着 KeyError / TypeError 穿到下载中途才炸，那时退的是 **1** ——
    而 1 在本脚本的契约里是"必需模型缺失"，install-hermes.sh 的四分支门禁会照这个含义
    提示用户"没下到模型、稍后重试"。用户重试多少次都没用：坏的是本地这份 lock。
    lock 既会被 refresh-lock 重生成，也会被人手编辑，手滑是常态。
    """
    _check_name(spec["name"])

    # sha256 是判"就位"的唯一依据（size 不参与），缺了会在第一次校验就 KeyError。
    # CI 缓存命中那条路最容易撞上：文件都在本地，一次网络请求都还没发就炸了。
    # 顺带钉死长度和字符集 —— 粘贴截断的摘要不会匹配上任何文件，落到用户眼前是
    # "每次都说校验不通过、重下还是不通过"，比直接说 lock 坏了难查得多。
    digest = spec.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64 or not _HEX.issuperset(digest):
        raise ValueError(f"{spec['name']}: sha256 要是 64 位十六进制字符串，实得 {digest!r}")
    # 统一转小写：比对的是 hexdigest() 的小写输出 + ``==``，lock 里写成大写（Windows 的
    # `certutil -hashfile` 输出就是大写）会让每个文件都判"校验不通过"，表现成永远下不完
    # 的重下循环，而每一步单看都正常。
    spec["sha256"] = digest.lower()

    # 可选键写成 null 必须与"整条不写"同义。这不是洁癖：``.get(key, default)`` 的默认值
    # 只在**键不存在**时生效，键在、值是 None 时原样返回 None，于是每个可选键都从为它
    # 设计的那道判据下面绕过去，而两个键漏下去的后果还完全不同 ——
    #   size:     下游 spec.get("size", 0) 拿到 None → _human() 拿它比大小 → TypeError
    #             → traceback + 退 1。--quiet 也救不了：f-string 的参数在调用 _log 之前
    #             就已经求值了。
    #   required: _required() 里 bool(None) 是 False → 必需模型被静默降级成可选。build 的
    #             --strict、CI 门禁、install-hermes 的就绪判据全照 required 判，于是少一个
    #             必需模型的包能一路退 0 发出去 —— 比炸掉更糟，因为没有任何人收到信号。
    #             这正好是 _required() 那句 fail-closed 契约的反面：漏写键它兜得住，写成
    #             null 反而兜不住。
    # 手写 lock 时先占位、回头再填是很自然的写法，两个键都撞得上。
    for key in ("size", "required"):
        if key in spec and spec[key] is None:
            del spec[key]

    # size 给了就得能参与算术：_human() 与续传的越界判断都直接拿它比大小，字符串会
    # TypeError。放在这儿而不是下载那步，是因为校验流程本身压根不读 size：没有这道拦截，
    # CI 那道 `--check --strict` 门禁会对着一份坏 lock 照常亮绿，等真正下载时才炸 ——
    # 而那时退的是 1，含义又被 install-hermes.sh 读成"重试一下就好"。
    size = spec.get("size")
    if size is not None and (not isinstance(size, int) or size < 0):
        raise ValueError(f"{spec['name']}: size 要是非负整数，实得 {size!r}")


_OK_SCHEMES = ("http", "https", "file")


def _normalize_env_source(env: str) -> str:
    """把 MILOCO_MODELS_BASE_URL 的值规范成一个能交给 urllib 的 URL。

    裸路径要兜底成 ``file://``：这个变量是离线/内网唯一的换源入口，而两个文档入口
    都没把"要带 scheme"讲硬（sync-to-remote.sh 说的是"内网源"，README 与 dev-guide
    只给了 https 的例子），于是最自然的写法就是给一个挂载目录的绝对路径。不兜底的话
    值会原样拼进 URL，``urllib.request.Request`` 抛的是 **ValueError** ——它不在下载
    循环那个 ``(URLError, OSError, TimeoutError)`` 里，会一路穿出 main：打 traceback、
    退 1（文档定义为"必需模型缺失"）而不是文件头承诺的 2，剩下几个文件也不再尝试，
    "可用 MILOCO_MODELS_BASE_URL 换源"那句提示更是永远打不出来 —— 插件安装器接住
    非 0 后打的是"下载失败（网络？）"，而用户明明是刻意离线的。

    scheme 长度为 1 时按裸路径处理：Windows 盘符 ``C:\\models`` 会被 urlparse 解析成
    scheme ``"c"``（合法 scheme 至少两字符，不会误伤）。
    转换结果会出现在下载日志和失败文案的"源：…"里，所以这层兜底不是隐身的。
    """
    scheme = urllib.parse.urlparse(env).scheme
    if not scheme or len(scheme) == 1:
        return Path(env).expanduser().resolve().as_uri()
    if scheme not in _OK_SCHEMES:
        raise ValueError(
            f"MILOCO_MODELS_BASE_URL 只支持 {' / '.join(_OK_SCHEMES)}，"
            f"实得 {scheme!r}：{env}"
        )
    return env.rstrip("/")


def _sources(lock: dict[str, Any]) -> list[str]:
    """下载源：env 独占替换，否则 lock 的 base_url（GitHub 直连）+ mirrors（加速镜像）。

    ``MILOCO_MODELS_BASE_URL`` 是**独占**的 —— 设了它就只用它，不再拼 lock 里的源。
    内网/离线场景要的正是"别再去碰外网"，"优先 + 兜底"会把请求漏到公网去。

    mirrors 与 ``scripts/manifest.json`` 的 ``download.sites`` 是同一批 GitHub 加速站
    （终端用户下大包用那份，开发者/CI 下模型用这份），两边由 test_fetch_models.py 钉死一致。
    """
    env = os.environ.get("MILOCO_MODELS_BASE_URL", "").strip()
    if env:
        return [_normalize_env_source(env)]
    urls = [u for u in [lock.get("base_url", ""), *lock.get("mirrors", [])] if u]
    for u in urls:
        # lock 里只认合法 URL，裸路径**不**兜底 —— 与 env 那侧刻意不同。lock 是提交进
        # 仓库、由 publish_models.sh refresh_lock 生成的产物，写成裸路径就是坏了，
        # 而"坏 lock 退 2"是本脚本已有的契约（见 main 里读 lock 那段）。这里若跟着
        # 兜底成 file://，就把一个一眼能定位的配置错误变成"从一个不存在的本地目录下载
        # 失败"——退 1、文案说"必需模型未就绪 / 可用 MILOCO_MODELS_BASE_URL 换源"，
        # 把人指向换源，而真正坏的是 lock 自己。
        if urllib.parse.urlparse(u).scheme not in _OK_SCHEMES:
            raise ValueError(f"lock 里的下载源不是合法 URL（需 http/https/file）：{u}")
    return [u.rstrip("/") for u in urls]


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


def _discard_part(part: Path) -> None:
    """丢弃 ``part`` 已有内容，但**保留 inode**（因而也保留 _open_part 拿到的 flock）。

    不能用 unlink：flock 锁的是 inode 而非路径。删掉目录项后锁就挂在一个没有名字的
    inode 上，别的进程在 ``_open_part`` 里会在同一路径新建 inode 并成功抢到锁——两边
    于是都往同一个 ``.part`` 写自己的字节流（正是 ``_open_part`` docstring 点名要防的
    共享 dest 场景）。交错出来的内容永远校验不过，表现成"重跑多少次都是 sha256 不符"，
    把诊断指向"镜像被投毒"，而网络和镜像其实都是好的。

    截断到 0 与删除对下游完全等价：``_stream`` 下一轮 ``stat`` 得 0 → 不带 Range、
    以 ``"wb"`` 整段重写。
    """
    try:
        os.truncate(part, 0)
    except FileNotFoundError:
        pass


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
        # 已有内容不小于目标 → 是脏文件（上次写坏 / 换了资产），从头下。
        # 截断而非删除：见 _discard_part（删了会让本进程的 flock 脱靶）。这条路径
        # 尤其要紧——目录里留着一份换代后的超长 .part 就会命中，不需要任何 sha 不符，
        # 整轮调用从第一次 _stream 起就在无锁状态下跑。
        # 只挡 offset >= lock size 这一侧：lock 比实际资产长时，落在
        # [实际长度, lock size) 窗口里的 .part 会让 Range 起点越界，由 _fetch_one
        # 的 416 分支归零（那里拿得到服务端的回答，这里只有 lock 的一面之词）。
        _discard_part(part)
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
            parsed = urllib.parse.urlparse(url)
            host = parsed.netloc or url
            # 只有网络源值得退避——"等一会儿再试"能改变结果的前提是失败来自链路抖动。
            # file:// 上读不到就是读不到，退避纯属白等，判据与 _stream 里那条
            # Range 只发给 http/https 的守卫同源。
            backoff = parsed.scheme in ("http", "https")
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
                    # 截断而非删除：删掉目录项后 flock 就脱靶，退避 sleep 的这 1-2s
                    # 里别的进程会新建同名 inode 并抢锁成功，两边交错写同一个 .part。
                    _discard_part(part)
                # http.client.HTTPException 必须在列：它直接继承 Exception，既不是
                # OSError 也不是 URLError（URLError 只包住 **建连** 阶段的错误，读响应体
                # 时 http.client 抛的原样穿出来）。最常撞上的是 IncompleteRead ——
                # 分块传输（Transfer-Encoding: chunked）下服务端中途断开，此时响应没有
                # Content-Length，_stream 里"写入字节数 != 自报长度"那道检查根本无从触发
                # （declared 是 None），截断唯一的表现形式就是 resp.read() 抛 IncompleteRead。
                # 漏掉它的代价不止是一条 traceback 顶替中文错误：异常会穿出
                # `for url in urls` 整个循环，**镜像降级彻底失效** —— 直连一次分块截断
                # 就带走整轮，剩下 3 个镜像一个都不会试。而这恰恰是镜像最该出场的场景。
                except (
                    urllib.error.URLError,
                    OSError,
                    TimeoutError,
                    http.client.HTTPException,
                ) as exc:
                    if isinstance(exc, urllib.error.HTTPError) and exc.code == 416:
                        # 416 = 续传起点越过了资产末尾。这是**我们自己攒下的状态**造成的
                        # 失败，和下面那些外部原因不同：保住 part 只会让每次重试、每个源、
                        # 以及之后每一次调用都从同一个偏移量原样复现同一个 416，永不自愈，
                        # 而日志还会打出"已保留 X 待续传"，把人指向"网络/镜像有问题"——
                        # 和唯一的解法（把这截字节丢掉）正好反向。
                        # 触发条件：lock 记的 size 比线上资产大（换代后没 refresh），且目录里
                        # 恰好留着一份落在 [实际长度, lock size) 窗口里的 .part —— 开头那道
                        # 脏文件守卫只挡 offset >= lock size 的一侧，挡不住这个窗口。
                        # 处置与 sha256 不符那条同构（都是"本地这截不能要了"）：截断归零，
                        # 同一个源的下一次重试就不带 Range 从 0 重下。
                        _log(
                            f"  ! {name}  续传起点越界（HTTP 416），丢弃已下字节从头重下",
                            quiet=quiet,
                        )
                        _discard_part(part)
                    else:
                        # 故意不删 part：截断/超时留下的字节是下一轮 Range 续传的起点。
                        _log(
                            f"  ! {name}  下载失败（{attempt}/{_MAX_RETRIES}）：{exc}",
                            quiet=quiet,
                        )
                # 重试本身照留（本地源也可能撞上 EIO / 半路被换掉），只是不再干等：
                # install-hermes.sh 拿旁边的 checkout 当 file:// 源跑同步时，源目录缺
                # 几个模型是常态（fork 的 checkout 本就可能只有一部分），而那条路径是
                # 静默的 —— 每个缺失文件白等 1s+2s，表现成安装器在两条 info 之间无声
                # 卡住十几秒，还恰好卡在"看起来像挂了"的位置。缺的那几个本来就该由
                # 后面的门禁和联网那一趟去补、去说。
                if attempt < _MAX_RETRIES and backoff:
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
        help="可选模型缺失也算失败：凡把本命令退出码当「齐没齐」判据的调用方都该带上，"
        "否则只缺可选模型时会拿到退出码 0，而门禁那边照旧判不齐",
    )
    ap.add_argument("--quiet", action="store_true", help="静默（仍会打印错误）")
    args = ap.parse_args(argv)

    lock_path = Path(args.lock) if args.lock else _DEFAULT_LOCK
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        files: list[dict[str, Any]] = lock["files"]
        for spec in files:
            _check_spec(spec)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"lock 文件不可用（{lock_path}）：{exc}", file=sys.stderr)
        return 2

    try:
        specs = _select(files, args.only, args.required_only)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    dest = Path(args.dest or os.environ.get("MILOCO_MODELS_DEST") or _DEFAULT_DEST).expanduser()

    # 与本函数里读 lock、_select 那两段同构：所有"输入不合法"都收敛成退 2，
    # 不许有异常穿出去变成 traceback + 退 1。
    #
    # 位置在 --check **之前**：源合不合法属于"这份 lock 坏没坏"，不属于"要不要下载"。
    # 放在后面的话，同一份 base_url 写成裸路径 /mnt/nas/models 的 lock，配一个已经齐了的
    # 目录，会 `--check --strict` 退 0、走下载退 2 —— 而 --check 是全仓四道门禁共用的
    # "齐没齐"判据（models_ready、ci.yml 的复判、local-ci.sh 的提示、--post-install 印给
    # 用户的那条命令），它说 OK 就必须意味着这份 lock 真的能用。可观察的后果是本地与 CI
    # 结论分裂：local-ci.sh 那步一路绿，推上去 CI 的 download 立刻退 2 红，而两边读的是
    # 同一份文件。这与 _check_spec 里 size 那段论证的是同一类失效（"校验流程本身压根不读
    # size：没有这道拦截，--check --strict 会对着一份坏 lock 照常亮绿"）—— size 那条早
    # 拦住了，源这条没有。
    #
    # 不会误伤"离线机器"：env 换源时 _sources 直接返回那一个源、根本不看 lock 里的
    # base_url（独占替换），所以内网/离线的正常用法一如既往。而 lock 是 publish_models.sh
    # refresh-lock 的生成物，base_url 恒被写成 release 地址 —— 源为空或不是合法 URL 的
    # lock 没有"合法但特殊"的解释，就是坏了。
    try:
        urls = _sources(lock)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not urls:
        print("lock 里没有可用下载源（base_url / mirrors 均为空）", file=sys.stderr)
        return 2

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
            # 印出来的补齐命令必须复刻本次判红的判据，所以 --strict 要跟着透传：本次是
            # --check --strict 判红、而 blocking 里只有可选模型时，不带 --strict 的那条
            # 命令跑完退 0（可选失败不并进 failed_required），用户看到成功的退出码，
            # 而下一次门禁一字不差地再红一遍 —— 他手上这条命令就是唯一线索，却恰好是
            # 唯一验证不出问题的那条。调用方（local-ci.sh / install-hermes.sh）的同类
            # 提示同理，都跟着门禁的强度走。
            # 两截还要同**口径**：这个 --check 分支是好几道门禁的共用实现，而调用它的
            # 处境不止"人在仓库根手敲"一种 —— install-hermes 装完之后从 ~/.hermes/... 用
            # 绝对路径调它，工作目录是用户当时所在的任意目录；local-ci.sh 也用绝对路径调。
            # 前半截硬编码相对路径、后半截 --dest 却是 expanduser 过的绝对路径的话，整行
            # 粘回去得到的是 `can't open file '/home/u/scripts/fetch_models.py'` —— 一个跟
            # "模型不齐"毫无关系的新错误，正好把人往"文件损坏 / 路径写错"带偏，正是
            # install-hermes 下载失败分支里"带上解释器"那段注释论证过的同一种失效。
            # 跟着当前进程走，两截就必然同口径（sys.executable 在个别嵌入式解释器下是
            # 空串，留 python3 兜底）。
            # 这两截同样要 quote，理由与下面的 --dest / --lock 一字不差：仓库 clone 在带
            # 空格的目录下（macOS 的 ~/Library/Mobile Documents/、用户名带空格的 home）时，
            # 裸插值整行粘回去会被 shell 从空格处切开，得到 `can't open file '/Users/li'`
            # —— 又是一个跟"模型不齐"无关的新错误，与上面这段论证的失效同类，只是触发
            # 条件从"相对路径"换成了"路径带空格"。
            fix = f"{shlex.quote(sys.executable or 'python3')} {shlex.quote(str(Path(__file__).resolve()))}"
            if args.strict:
                fix += " --strict"
            # 同口径这件事不止于路径：--lock / --only / --required-only 决定的是"拿哪一份
            # 清单、判其中哪几个文件"，不透传的话印出来的是**另一次运行**的补齐命令。
            #   --lock：判的是 /tmp/staging.lock.json，印的命令却去读仓库里的默认清单 ——
            #           两份 tag 不同就是另一组哈希，用户照抄跑完回来重判，还是"缺"。而
            #           报错文本里没有任何字指向"清单不是同一份"，这条线索他根本拿不到。
            #   --only / --required-only：反过来把范围放大，让他去下一堆本次压根没在判的
            #           文件（--required-only 时可能是几十 MB 的可选模型）。
            # 这三个都只在显式给了时才追加，默认那条命令的样子不变。
            if args.lock:
                fix += f" --lock {shlex.quote(str(args.lock))}"
            if args.required_only:
                fix += " --required-only"
            fix += "".join(f" --only {shlex.quote(n)}" for n in args.only)
            # dest 也 quote：它可能来自 --dest / MILOCO_MODELS_DEST / 包内默认路径，
            # 带空格时整行粘回去会被 shell 切成两个参数 —— 同样是"印出来却跑不了"。
            # shlex.quote 对不含特殊字符的路径原样返回，常见情形下输出不变。
            #
            # 换源变量同理，而且它连 argparse 都不在，上面按参数清点时扫不到它：
            # MILOCO_MODELS_BASE_URL 是**独占**替换（见 _sources），不带前缀印出来的那条
            # 命令走的是 lock 里的公网源 —— 与 --lock 同一类"印的是另一次运行"，后果按
            # 机器分两种：真离线的等 4 个源各退避重试一轮再退 1，内网有出口的则下载
            # **成功**，请求悄悄出了公网，正是这个变量存在理由的反面，且没有任何人收到
            # 信号。它一贯以一次性前缀写法给出（README 与 PR 描述的示例都是），也就是说
            # 并不在用户当前 shell 里，照抄那条命令等于把它丢了。
            # 另一个环境变量 MILOCO_MODELS_DEST 不必单列：它只影响 dest，而 dest 本来就
            # 以显式 --dest 印在同一行里，已经同口径了。
            env_src = os.environ.get("MILOCO_MODELS_BASE_URL", "").strip()
            prefix = f"MILOCO_MODELS_BASE_URL={shlex.quote(env_src)} " if env_src else ""
            print(
                f"缺少模型：{', '.join(blocking)}\n补齐：{prefix}{fix} --dest {shlex.quote(str(dest))}",
                file=sys.stderr,
            )
            return 1
        if bad_optional:
            _log(f"  可选模型缺失（对应功能降级，不阻塞）：{', '.join(bad_optional)}", quiet=args.quiet)
        return 0

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
    # 阻塞与措辞分开算。--strict 的语义是"两桶都算阻塞"，但并桶之后不能跟着沿用必需桶
    # 的措辞：lock 是"缺不缺得起"的唯一事实来源，silero_vad 在里面明写 required=false，
    # 文档和 resource_validator 也都按可选处理。把它报成"必需模型未就绪"，用户会拿着这
    # 句话去翻"VAD 什么时候变必需了"，而三处口径都说它可选 —— 真正的原因只是本次带了
    # --strict。同一函数的 --check 分支早就避开了这个词（用中性的"缺少模型"）。
    blocking = failed_required + (failed_optional if args.strict else [])
    if blocking:
        # 非严格模式下能进这里的只有必需桶，"必需"照实说；只有严格模式真把可选模型
        # 并进来时才换中性措辞，并把"因 --strict 才阻塞"这件事明说出来。
        if args.strict and failed_optional:
            head = (
                f"模型未就绪：{', '.join(blocking)}"
                f"（其中 {', '.join(failed_optional)} 在 lock 里是可选，因 --strict 一并阻塞）"
            )
        else:
            head = f"必需模型未就绪：{', '.join(blocking)}"
        print(
            f"{head}\n"
            f"源：{' | '.join(urls)}（可用 MILOCO_MODELS_BASE_URL 换源，或手动放置到 {dest}）",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
