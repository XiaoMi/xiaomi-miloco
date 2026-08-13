#!/usr/bin/env bash
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
#
# 感知 ONNX 模型发布助手（维护者用，需要本机 gh 已登录且对仓库有 write 权限）。
#
# 模型托管在固定 tag `models` 的 GitHub Release，仓库内只留 scripts/models.lock.json
# 锁定文件名 + size + sha256。改模型的流程 = 上传新资产 + 刷新 lock，两步都在这里。
#
# 用法:
#   scripts/publish_models.sh upload <dir>     # 把 <dir> 下的模型上传到 models Release（覆盖同名），再按本地文件刷新 lock
#   scripts/publish_models.sh refresh-lock     # 从 Release 拉回当前资产、重算 hash 刷新 lock（网页手动换过资产后用这个）
#   scripts/publish_models.sh refresh-lock <dir>  # 按本地目录重算 lock，不联网
#   scripts/publish_models.sh verify           # 零下载对账：Release 资产清单 vs lock（CI lint job 跑的就是它）
#
# 注意:
#   · `models` 这个 tag / Release 是可变的：换掉资产后老 commit 里的 lock hash 就对不上、
#     老 commit 将构建失败。若要"老 commit 永远可构建"，请改用不可变 tag（models-v2 …）
#     并同步更新 lock 的 release_tag / base_url。
#   · required / desc 字段沿用旧 lock 里同名文件的取值（同名条目漏写 required 时按
#     必需处理，与 fetch_models.py 同口径）；新增文件默认 required=false，需要的话手工
#     改 lock（口径要与 perception/engine/resource_validator.py 对齐）。
#   · upload / refresh-lock 发现"文件集与旧 lock 不同"会直接失败，防的是静默缩表：
#     拿只含 2 个模型的目录跑一次，lock 就悄悄少 3 项、线上从此不再下发它们。
#     确属换代（真要增删模型）时 MILOCO_MODELS_ALLOW_LOCK_DRIFT=1 重跑。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK="$SCRIPT_DIR/models.lock.json"
REPO="${MILOCO_REPO:-XiaoMi/xiaomi-miloco}"
log() { printf '%s\n' "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

# lock 的读取延到 case 分派之后（也就必须晚于 die 的定义）：搁在顶层求值的话，lock 不
# 存在 / JSON 坏掉时**任何**子命令都先吃一段 Python traceback 再退 1 —— 连 --help 都
# 一个字打不出来，而 CI lint job 里 `publish_models.sh verify` 红出来的也是 traceback，
# 读的人第一反应是"脚本崩了"而不是"清单坏了"。典型触发是合并冲突标记：两个分支各自
# refresh 过 lock，合出来的工作区里这个文件必然是非法 JSON。收敛成一行中文 + 非 0，
# 与 fetch_models.py 读坏 lock 的口径一致（退出码不跟着对齐到 2：本脚本的 die 一律退 1，
# 唯一的自动消费方是 CI 那句 `run:`，只看零/非零，为此另立一套码段没有收益）。
#
# 三个子命令一律先过这道校验，**不**按"用不用得上 TAG"分叉。refresh-lock <dir> 确实用
# 不到 tag，但 refresh_lock_from_dir 里同样要 json.loads 这份 lock（那段 Python 的头一
# 句就是），按需分叉的话恰好是它绕开校验、traceback 原样还在 —— 而"合并完先跑一次
# refresh-lock"正是最容易撞上冲突标记的那条路。
#
# 走 argv 而不是把 $LOCK 插进 Python 源码串（与本文件另外三处调 python3 的写法一致）：
# 插进去的话，clone 到含单引号或反斜杠的路径下（/home/o'brien/miloco、Git Bash 的路径）
# 展开出来就是语法错误的 Python。
TAG=""
require_lock() {
    TAG="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["release_tag"])' "$LOCK" 2>/dev/null)" \
        || die "lock 不可用（不存在 / JSON 非法 / 缺 release_tag，先看看有没有合并冲突标记）: $LOCK"
    [ -n "$TAG" ] || die "lock 的 release_tag 是空串: $LOCK"
}

need_gh() {
    command -v gh >/dev/null 2>&1 || die "未找到 gh CLI（brew install gh && gh auth login）"
    gh auth status >/dev/null 2>&1 || die "gh 未登录（gh auth login）"
}

# 只比文件集、不算 hash、不写 lock —— 供 upload 在动线上资产之前预检。
# 判定与 refresh_lock_from_dir 里那份是同一套，但必须早于 gh release upload：
# 护栏只放在上传之后的话，拒绝改表时资产已经躺在公开 Release 上、lock 还是旧的，
# 从这一刻起所有人的 PR 和 main 都会被对账门禁判红，且红的原因跟他们的改动无关；
# 而脚本连"去 delete-asset"都来不及说 —— SystemExit 撞上 set -e 当场中止，
# cmd_upload 里那段善后提示根本执行不到。
#
# 收的是即将上传的那批文件名本身，不是重新扫一遍目录：要保证的是"我马上要推上去的
# 这批东西和 lock 一致"，重扫一次的话两次结果之间还能再漂一回。
check_fileset_against_lock() {
    python3 - "$LOCK" "$@" <<'PY'
import json, os, sys
from pathlib import Path

lock_path, names = Path(sys.argv[1]), {Path(a).name for a in sys.argv[2:]}
old = {f["name"] for f in json.loads(lock_path.read_text(encoding="utf-8")).get("files", [])}

if names != old and os.environ.get("MILOCO_MODELS_ALLOW_LOCK_DRIFT") != "1":
    detail = [f"  - 旧 lock 有、这次没传：{n}" for n in sorted(old - names)]
    detail += [f"  + 这次要传、旧 lock 没有：{n}" for n in sorted(names - old)]
    raise SystemExit(
        "::error:: 待上传的文件集与旧 lock 不一致，已在上传前中止（线上资产未被改动）：\n"
        + "\n".join(detail)
        + "\n确认这就是你要的（真在增删模型）后，用 MILOCO_MODELS_ALLOW_LOCK_DRIFT=1 重跑。"
        + "\n换代改名时记得同时删掉 Release 上的旧资产（gh release delete-asset），"
        "否则 CI 的对账门禁会红。"
    )
PY
}

# 按目录内的实际文件重写 lock：保留旧 lock 的 release_tag / base_url / mirrors，
# 以及同名文件的 required / desc（同名条目漏写 required 时按必需保留，与下载器同口径，
# 见下面那段注释）；size + sha256 全部重算。
refresh_lock_from_dir() {
    local dir="$1"
    python3 - "$LOCK" "$dir" <<'PY'
import hashlib, json, os, sys
from pathlib import Path

lock_path, src = Path(sys.argv[1]), Path(sys.argv[2])
lock = json.loads(lock_path.read_text(encoding="utf-8"))
old = {f["name"]: f for f in lock.get("files", [])}

files = sorted(
    p for p in src.iterdir()
    if p.is_file() and p.suffix in (".onnx", ".json") and not p.name.endswith(".part")
)
if not files:
    raise SystemExit(f"::error:: {src} 下没有 .onnx / .json 模型文件")

# 文件集必须与旧 lock 全等，否则拒绝改表。两个方向都危险，且都不会报错：
#   少了 —— 目录里只有 2 个模型（下载中断 / 手动挑了几个上传）时，lock 被无声缩到 2 项，
#           剩下 3 个从此不再下发；线上表现是"某天起某功能悄悄降级"，没有任何失败点。
#   多了 —— 目录/Release 上有 lock 之外的文件（换代残留、误传），会被以 required=false
#           默默收编进 lock，desc 空白，谁也不知道它是干嘛的、还该不该在。
names = {p.name for p in files}
if names != set(old) and os.environ.get("MILOCO_MODELS_ALLOW_LOCK_DRIFT") != "1":
    detail = [f"  - 旧 lock 有、{src} 里没有：{n}（继续会把它从 lock 删掉）" for n in sorted(set(old) - names)]
    detail += [f"  + {src} 里有、旧 lock 没有：{n}（继续会以 required=false 收进 lock）" for n in sorted(names - set(old))]
    raise SystemExit(
        "::error:: 文件集与旧 lock 不一致，拒绝静默改表：\n"
        + "\n".join(detail)
        + "\n确认这就是你要的（真在增删模型）后，用 MILOCO_MODELS_ALLOW_LOCK_DRIFT=1 重跑。"
        + "\n新增项记得手工把 required / desc 与 perception/engine/resource_validator.py 对齐。"
    )

out = []
for p in files:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(256 * 1024), b""):
            h.update(chunk)
    prev = old.get(p.name)
    out.append({
        "name": p.name,
        "size": p.stat().st_size,
        "sha256": h.hexdigest(),
        # 同名旧条目缺 required 时按"必需"读，与 fetch_models._required 同口径
        # （那边缺键 fail-closed 判必需）。两边默认值相反的话，「手工补 lock 时漏写
        # required」会被下一次 refresh 静默翻面：漏写的那阵子一切是绿的（下载器一直
        # 当它必需，没有任何信号），而上面那道护栏只比文件名集合，看不见「同名但少一
        # 个键」这种漂移，于是下一次 upload 就把它写成可选。翻面之后，凡判据里 required
        # 仍然当真的地方都跟着松掉：--required-only 直接跳过不下，不带 --strict 的
        # --check（ci.yml 那步下载）判绿，而 lock 说可选、resource_validator
        # 里仍是硬编码必需 —— 恰好是护栏那句「新增项记得手工把 required / desc 与
        # resource_validator.py 对齐」点名的那个字段，自己分了家。
        # 真正新增的文件仍默认 false —— 那条路径已由护栏显式放行并要求人工对齐。
        "required": bool(prev.get("required", True)) if prev is not None else False,
        "desc": (prev or {}).get("desc", ""),
    })

# 必需模型排前面，其余按名字，diff 稳定
out.sort(key=lambda f: (not f["required"], f["name"]))
lock["files"] = out
lock_path.write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

for f in out:
    print(f"  {'必需' if f['required'] else '可选'}  {f['name']}  {f['size']}  {f['sha256'][:16]}…")
print(f"已写入 {lock_path}（{len(out)} 个文件）")
PY
}

cmd_upload() {
    local dir="${1:-}"
    [ -n "$dir" ] || die "用法: scripts/publish_models.sh upload <dir>"
    [ -d "$dir" ] || die "目录不存在: $dir"
    need_gh

    local files=()
    while IFS= read -r f; do files+=("$f"); done < <(
        find "$dir" -maxdepth 1 -type f \( -name '*.onnx' -o -name '*.json' \) ! -name '*.part' | sort
    )
    [ ${#files[@]} -gt 0 ] || die "$dir 下没有 .onnx / .json 模型文件"

    log "将上传到 $REPO 的 Release '$TAG'（同名资产覆盖）:"
    for f in "${files[@]}"; do log "  $(du -h "$f" | cut -f1)  $(basename "$f")"; done

    # 上传是不可逆的（资产一旦推上公开 Release，只能手工 delete-asset 才撤得掉），
    # 所以文件集比对必须发生在这之前。refresh_lock_from_dir 里那道同样的护栏留着
    # 不动：refresh-lock 子命令走的是它，那条路径上并不会先动线上资产。
    check_fileset_against_lock "${files[@]}"

    if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
        gh release upload "$TAG" "${files[@]}" --clobber --repo "$REPO"
    else
        # tag 不存在时 gh 会按当前默认分支建 tag；模型 Release 标 prerelease，
        # 避免顶掉 Latest（install.sh 靠 /releases/latest/download/install.sh 引导）。
        gh release create "$TAG" --repo "$REPO" --prerelease \
            --title "Perception ONNX models" \
            --notes "Permanent asset host for Miloco perception ONNX models. Pinned by scripts/models.lock.json — do not delete." \
            "${files[@]}"
    fi

    log "刷新 lock ..."
    refresh_lock_from_dir "$dir"

    # upload 只 --clobber 同名资产，从不删除 Release 上已有的其它文件。换代改名
    # （det_4C.onnx → det_5C.onnx）后旧资产会一直挂在那儿：本地看不见、lock 里没有。
    # 上面那道文件集护栏挡住了它被静默收编（不带 dir 的 refresh-lock 会把 Release 全量
    # 拉下来交给 refresh_lock_from_dir，多出来的这个立刻判红、要 ALLOW_LOCK_DRIFT=1 才
    # 放行），但挡不住它继续挂着 —— 而它挂着的代价不是零：从此每次 refresh-lock 都要
    # 先撞一次这道红墙，谁也说不清那个名字该不该在。所以这里立刻对一次账，把残留摆到
    # 上传者面前（此刻他还知道自己刚换了什么），而不是留给几个月后的人。
    log ""
    log "对账 Release 资产与刷新后的 lock ..."
    if ! cmd_verify; then
        log ""
        log "警告: 资产已上传、lock 已刷新，但 Release 上仍有与 lock 不符的东西（见上）。"
        log "      CI 的对账门禁会红 —— 请按提示清理后再提交 lock。"
    fi
    log "完成。别忘了提交 scripts/models.lock.json。"
}

# 零下载对账：只调一次 GitHub API 拿资产清单（name/size/digest），与 lock 逐项比。
# 存在的意义是把一类**间歇性**失败变成确定性的失败点：`models` 是固定且可变的 tag，
# 资产被换而某分支的 lock 没跟着 refresh 时，构建能不能过取决于 actions/cache 有没有
# 命中 —— 命中就拿旧字节比旧 lock（通过，零请求），缓存一被逐出就下到新资产、sha256
# 不符（红）。同一个 commit 今天绿下周红，且看日志完全看不出为什么。
# 前置检查刻意留给调用方（cmd_upload 开头那句 need_gh 已查过，直接跑 verify 由 case
# 分派处查）：need_gh 里的 die 也是 exit，摆在这儿会和下面那处一样打穿 cmd_upload 的
# 非致命分支，而且更隐蔽 —— gh auth status 不是纯本地检查，它要发一次请求验 token；
# 从 cmd_upload 那次 need_gh 到这里隔着 78MiB 上传 + 78MiB 重算 hash，通常好几分钟，
# token 过期或网络抖一下就够了。搬出去之后这个函数体内再没有任何能 exit 的语句。
cmd_verify() {
    local assets
    if ! assets="$(gh api "repos/$REPO/releases/tags/$TAG" --jq '[.assets[] | {name, size, digest}]')"; then
        # 用 return 而不是 die：die 是 exit，在函数里退的是整个 shell，cmd_upload 那句
        # `if ! cmd_verify` 接不住。而那次对账是**故意**设计成非致命的 —— 跑到那儿资产
        # 已经推上 Release、lock 也已落盘，一次 API 抖动不该把「别忘了提交 lock」那行
        # 一起吞掉：维护者看到 FATAL + 退出码 1，会读成「上传失败」，于是要么重跑一遍
        # upload，要么干脆没提交刷新后的 lock，把 CI 的对账门禁留给下一个人踩。
        # 直接跑 verify 时退出码不变：case 分支拿到 1，set -e 照样让脚本以 1 退出。
        log "对账失败: 读不到 $REPO 的 Release '$TAG' 资产清单（tag 不存在？gh 无权限？网络抖动？）"
        return 1
    fi

    # 走环境变量而不是 stdin：stdin 已经被 python3 - 的 heredoc 占了。
    MILOCO_ASSETS_JSON="$assets" python3 - "$LOCK" "$REPO" "$TAG" <<'PY'
import json, os, sys
from pathlib import Path

lock_path, repo, tag = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
want = {f["name"]: f for f in json.loads(lock_path.read_text(encoding="utf-8")).get("files", [])}
have = {a["name"]: a for a in json.loads(os.environ["MILOCO_ASSETS_JSON"])}

errors, warns = [], []

for name in sorted(set(want) - set(have)):
    errors.append(f"{name}: lock 里有、Release 上没有 —— 所有构建都会在这个文件上 404")
for name in sorted(set(have) - set(want)):
    # upload 不删旧资产，换代改名后老文件会一直留着。收编那条路已被 refresh_lock_from_dir
    # 的文件集护栏堵上（多一个就判红），所以这里判 error 而非 warn 是有出口的：不清掉它，
    # 往后每次不带 dir 的 refresh-lock 都会被同一个名字挡住。
    errors.append(
        f"{name}: Release 上有、lock 里没有 —— 换代残留？确认无用后删除："
        f"\n      gh release delete-asset {tag} {name} --repo {repo}"
    )

for name in sorted(set(want) & set(have)):
    w, h = want[name], have[name]
    if int(w["size"]) != int(h["size"]):
        # size 就不同，sha256 必然也不同，不再重复报一遍
        errors.append(f"{name}: size 不符 —— lock={w['size']} release={h['size']}")
        continue
    algo, _, hexdigest = (h.get("digest") or "").partition(":")
    if not hexdigest:
        # digest 是 GitHub 后加的字段，上传较早的资产可能没有；退化成只比 size。
        warns.append(f"{name}: Release 未给出 digest（老资产），本次只比对了 size")
    elif algo != "sha256":
        warns.append(f"{name}: Release digest 是 {algo}，无法与 lock 的 sha256 比对，只比对了 size")
    elif hexdigest.lower() != str(w["sha256"]).lower():
        errors.append(f"{name}: sha256 不符 —— lock={w['sha256'][:16]}… release={hexdigest[:16]}…")

for w in warns:
    print(f"::warning:: {w}", file=sys.stderr)
if errors:
    print(f"::error:: Release '{tag}'（{repo}）的资产与 scripts/models.lock.json 不一致：", file=sys.stderr)
    for e in errors:
        print(f"    · {e}", file=sys.stderr)
    print(
        "  修法二选一：资产是对的就 scripts/publish_models.sh refresh-lock 刷新 lock；"
        "lock 是对的就把 Release 改回去。",
        file=sys.stderr,
    )
    # 两条修法都要仓库 write 权限。这一步在没改 lock 的 PR 上是非阻塞告警（见
    # ci.yml 的 verify_gate），但日志里照样是一片红字，不写清归属每个贡献者都要困惑一次。
    print(
        "  （不是维护者的话：这条与你的改动无关 —— models Release 的资产和仓库 lock "
        "暂时不一致，两条修法都要仓库写权限。at 一下维护者刷 lock 即可，别去动你自己的 PR。）",
        file=sys.stderr,
    )
    raise SystemExit(1)

print(f"✓ Release '{tag}' 的 {len(have)} 个资产与 lock 全等（name/size/sha256）", file=sys.stderr)
PY
}

cmd_refresh_lock() {
    local dir="${1:-}"
    if [ -n "$dir" ]; then
        [ -d "$dir" ] || die "目录不存在: $dir"
        refresh_lock_from_dir "$dir"
        return
    fi
    need_gh
    local tmp
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    log "从 $REPO Release '$TAG' 拉取当前资产 → $tmp ..."
    gh release download "$TAG" --repo "$REPO" --dir "$tmp"
    refresh_lock_from_dir "$tmp"
}

case "${1:-}" in
    # require_lock 排在 need_gh 之前：纯本地、确定性的那道先说话，别让一个坏 lock 先
    # 报成"gh 没登录"（need_gh 里的 gh auth status 还要发一次请求，慢且可能另有噪声）。
    upload)       require_lock; shift; cmd_upload "$@" ;;
    refresh-lock) require_lock; shift; cmd_refresh_lock "$@" ;;
    verify)       require_lock; need_gh; cmd_verify ;;
    # 从第 5 行打到抬头注释块结束（第一个非 # 行），不写死结束行号：写死的话，往抬头
    # 补一条说明就会把 --help 的尾巴无声截掉，而没人会为此跑一次 --help。
    -h|--help|"") awk 'NR<5{next} !/^#/{exit} {sub(/^#[[:space:]]?/,""); print}' "${BASH_SOURCE[0]}" >&2 ;;
    *)            die "未知子命令: $1（支持 upload | refresh-lock | verify）" ;;
esac
