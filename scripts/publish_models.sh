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
#
# 注意:
#   · `models` 这个 tag / Release 是可变的：换掉资产后老 commit 里的 lock hash 就对不上、
#     老 commit 将构建失败。若要"老 commit 永远可构建"，请改用不可变 tag（models-v2 …）
#     并同步更新 lock 的 release_tag / base_url。
#   · required / desc 字段沿用旧 lock 里同名文件的取值；新增文件默认 required=false，
#     需要的话手工改 lock（口径要与 perception/engine/resource_validator.py 对齐）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK="$SCRIPT_DIR/models.lock.json"
REPO="${MILOCO_REPO:-XiaoMi/xiaomi-miloco}"
TAG="$(python3 -c "import json,sys;print(json.load(open('$LOCK'))['release_tag'])")"

log() { printf '%s\n' "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

need_gh() {
    command -v gh >/dev/null 2>&1 || die "未找到 gh CLI（brew install gh && gh auth login）"
    gh auth status >/dev/null 2>&1 || die "gh 未登录（gh auth login）"
}

# 按目录内的实际文件重写 lock：保留旧 lock 的 release_tag / base_url / mirrors，
# 以及同名文件的 required / desc；size + sha256 全部重算。
refresh_lock_from_dir() {
    local dir="$1"
    python3 - "$LOCK" "$dir" <<'PY'
import hashlib, json, sys
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

out = []
for p in files:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(256 * 1024), b""):
            h.update(chunk)
    prev = old.get(p.name, {})
    out.append({
        "name": p.name,
        "size": p.stat().st_size,
        "sha256": h.hexdigest(),
        "required": bool(prev.get("required", False)),
        "desc": prev.get("desc", ""),
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
    log "完成。别忘了提交 scripts/models.lock.json。"
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
    upload)       shift; cmd_upload "$@" ;;
    refresh-lock) shift; cmd_refresh_lock "$@" ;;
    # -E 而非 build.sh 里的 's/^# \?//'：BSD sed（macOS）不认 BRE 的 \?，会原样输出 '#'
    -h|--help|"") sed -n '5,20p' "${BASH_SOURCE[0]}" | sed -E 's/^#[[:space:]]?//' >&2 ;;
    *)            die "未知子命令: $1（支持 upload | refresh-lock）" ;;
esac
