#!/usr/bin/env bash
# .ci/run-opengrep.sh
#
# 用 .ci/opengrep-rules.yml 跑 OpenGrep 扫描，CI 与本地共用同一组路径和排除项
# （排除项见仓库根的 .semgrepignore）。
#
# 用法：
#   .ci/run-opengrep.sh                # 全量扫描，人读输出
#   .ci/run-opengrep.sh --sarif        # 额外写 SARIF 供上传
#   .ci/run-opengrep.sh --changed      # 仅扫本次改动的一方源码路径
#   .ci/run-opengrep.sh --error        # 有发现时以非零码退出
#
# 退出码：扫描出错非零；传 --error 且有发现时非零。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$REPO_ROOT/.ci/opengrep-rules.yml"

if [[ ! -f "$CONFIG" ]]; then
  echo "error: 规则文件不存在：$CONFIG" >&2
  exit 66
fi
if ! command -v opengrep >/dev/null 2>&1; then
  echo "error: 未找到 opengrep。安装：curl -fsSL https://raw.githubusercontent.com/opengrep/opengrep/v1.22.0/install.sh | bash -s -- -v v1.22.0" >&2
  exit 127
fi

EXTRA_ARGS=()
CHANGED_ONLY=0
while (( $# > 0 )); do
  case "$1" in
    --sarif) mkdir -p "$REPO_ROOT/.opengrep-out"; EXTRA_ARGS+=( "--sarif-output=$REPO_ROOT/.opengrep-out/precise.sarif" ); shift ;;
    --json)  mkdir -p "$REPO_ROOT/.opengrep-out"; EXTRA_ARGS+=( "--json" "--output=$REPO_ROOT/.opengrep-out/precise.json" ); shift ;;
    --changed) CHANGED_ONLY=1; shift ;;
    --error) EXTRA_ARGS+=( "--error" ); shift ;;
    *) EXTRA_ARGS+=( "$1" ); shift ;;
  esac
done

cd "$REPO_ROOT"

# 第一方源码目录（与 CI paths 一致）
FIRST_PARTY_RE='^(backend|cli|plugins/openclaw/src|web/src|scripts)/'

# .semgrepignore 同时是 opengrep 与本脚本的排除单一来源。下面据它把会被忽略的
# 路径从 SCAN_PATHS 中剔除，让"空集跳过"判断与 opengrep 实际扫描集合对齐——否则
# 当改动全是被忽略的文件（如纯测试 PR）时，SCAN_PATHS 非空但 opengrep 无目标可扫，
# 退出码不稳定会使 job 间歇性误失败。
#
# opengrep 以 --no-git-ignore 运行，忽略集 = 仅 .semgrepignore。但 git check-ignore
# 即便指定 core.excludesFile 仍会叠加读取仓库各级 .gitignore 与 .git/info/exclude，
# 其剔除集会成为 opengrep 忽略集的超集（artifact 目录如 coverage/temp/target/out 等
# 只在 .gitignore、不在 .semgrepignore）——一旦这类目录下放了 tracked 源码，就会被
# 脚本误剔、被 opengrep 漏扫。为与 opengrep 严格对齐，在仓库外建一个隔离的 git 环境，
# 只喂 .semgrepignore 作为 excludes，check-ignore 便只认它、不读仓库 .gitignore。
SEMGREPIGNORE="$REPO_ROOT/.semgrepignore"
ISOLATED_IGNORE_DIR=""
if [[ -f "$SEMGREPIGNORE" ]]; then
  ISOLATED_IGNORE_DIR="$(mktemp -d)"
  trap 'rm -rf "$ISOLATED_IGNORE_DIR"' EXIT
  cp "$SEMGREPIGNORE" "$ISOLATED_IGNORE_DIR/.exclude"
  git -C "$ISOLATED_IGNORE_DIR" init -q
fi

# 仅按 .semgrepignore 判定路径是否会被 opengrep 忽略（不叠加仓库 .gitignore）。
semgrep_ignored() {
  [[ -n "$ISOLATED_IGNORE_DIR" ]] || return 1
  git -C "$ISOLATED_IGNORE_DIR" -c core.excludesFile="$ISOLATED_IGNORE_DIR/.exclude" \
    check-ignore -q --no-index "$1" 2>/dev/null
}

if (( CHANGED_ONLY )); then
  DIFF_REF="${MILOCO_OPENGREP_BASE_REF:-origin/main...HEAD}"
  SCAN_PATHS=()
  while IFS= read -r p; do
    [[ -L "$p" ]] && continue
    [[ -f "$p" || -d "$p" ]] || continue
    # 与 opengrep 内部行为对齐：会被 .semgrepignore 排除的路径不计入扫描集
    semgrep_ignored "$p" && continue
    SCAN_PATHS+=( "$p" )
  done < <(
    {
      git diff --name-only --diff-filter=ACMRTUXB "$DIFF_REF" 2>/dev/null || true
      git ls-files --others --exclude-standard
    } | grep -E "$FIRST_PARTY_RE" | sort -u
  )
  if (( ${#SCAN_PATHS[@]} == 0 )); then
    echo "→ 本次无需扫描的第一方源码（无改动或改动均被 .semgrepignore 排除），跳过 opengrep。" >&2
    exit 0
  fi
else
  SCAN_PATHS=( backend cli plugins/openclaw/src web/src scripts )
fi

echo "→ opengrep 扫描：${SCAN_PATHS[*]}（排除项见 .semgrepignore）" >&2
# exec 会替换当前进程，EXIT trap 不再触发，故在此显式清理隔离目录后再 exec
# （把 opengrep 退出码原样透传给调用方）。
if [[ -n "$ISOLATED_IGNORE_DIR" ]]; then
  rm -rf "$ISOLATED_IGNORE_DIR"
fi
exec opengrep scan --no-strict --config "$CONFIG" --no-git-ignore "${EXTRA_ARGS[@]}" "${SCAN_PATHS[@]}"
