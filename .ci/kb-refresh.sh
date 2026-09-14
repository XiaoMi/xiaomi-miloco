#!/usr/bin/env bash
# 知识库每周刷新。固定六阶段、固定轮数，不许收敛提前停：
#   阶段1 增量同步×3轮（近30天全部 commit，逐条重新核，不许以"上轮已覆盖"为由跳过）
#   阶段2 全量同步×1轮（按 .agents/commands/kb-refresh.md 的方法，对照代码现状全量核一遍 knowledge/，
#                      补漏/改正/删过时，无数值闸、不重跑）
#   阶段3 完整审查×1轮（对本次全部改动 + 代码实现做正确性校验，就地改）
#   阶段4 review×1轮（只读，--tools 不含 Edit/Write，只出 [ISSUE] 清单）
#   阶段5 解决×1轮（独立 agent 拿 [ISSUE] 清单逐条改，回报 [FIXED]；零 ISSUE 则跳过）
#   阶段6 commit + 开一个待人工确认的 PR。人是唯一合并闸门：本脚本只开 PR、从不自动合并。
#
# 退出码：0=成功/无需改；2=前置/装配错误；3=护栏拦截（改了 knowledge/ 以外、库内产出非 .md、或阶段4
#         只读期间 knowledge/ 内容仍被改动）；4=docs 格式门失败；5=agent 系统性失败（"无改动"不可信，
#         或 claude 调用不可重试失败/退避耗尽仍失败）；
#         6=无法查询现有 PR（防重复开 PR）；7=开 PR 阶段 git/gh 失败。
#
# 断点续跑：传 --resume 时读 KB_STATE_FILE 续跑到中断点；状态文件不存在/不可解析则按全新运行开始。
# 正常跑完（含 exit 0 的早退路径）会自动删除状态文件，避免下次误续。
# F14：这只是本地/同一进程内可用的能力——CI 的一次性容器每次跑都是全新 checkout，workflow 没有接
# cache/artifact 保存 KB_STATE_FILE、也没有传 --resume，所以断点续跑在 CI 里实际不可用；CI 超时或
# 失败只能整轮重跑，不会续到中断点。另注：跨进程 --resume 到 fix 阶段不保留上次阶段4生成的 [ISSUE]
# 清单（未持久化），此时阶段5会被当成零 ISSUE 处理——同一类"本地续跑非完全保真"的已知限制。
set -uo pipefail

# ── 可配置（env）。六阶段轮数固定、不再暴露轮数类输入；仍可配的是覆盖清单窗口与退避/去重相关参数。──
KB_DIR="${KB_DIR:-knowledge}"
COMMIT_WINDOW_DAYS="${COMMIT_WINDOW_DAYS:-30}"  # 阶段1 增量同步回看的 commit 窗口
CHECKLIST_MAX="${CHECKLIST_MAX:-0}"             # 阶段1 覆盖清单最多取最近 N 条 commit（0=不限，默认全量纳入）
REPO="${REPO:-XiaoMi/xiaomi-miloco}"
CMD_SRC="${CMD_SRC:-.agents/commands/kb-refresh.md}"
OPEN_PR="${OPEN_PR:-auto}"                      # auto|no
KB_BACKOFF_SECS="${KB_BACKOFF_SECS:-300 600 1800 3600}"  # 429/限流退避序列（秒），耗尽仍失败 → exit 5
KB_STATE_FILE="${KB_STATE_FILE:-.ci/.kb-refresh-state.json}"  # 断点续跑状态文件，不进 knowledge/、不进 commit

log() { echo "[kb-refresh] $*"; }

# ── knowledge/ 内容签名（按内容而非文件名）；断点续跑状态也依赖它，故提前到此处定义 ──────
kb_sig() {
  {
    git -c core.quotepath=false diff HEAD -- "$KB_DIR"
    git -c core.quotepath=false ls-files --others --exclude-standard -- "$KB_DIR" \
      | LC_ALL=C sort | while IFS= read -r f; do printf '== %s ==\n' "$f"; cat -- "$f"; done
  } | md5sum | awk '{print $1}'
}

# ── 断点续跑：状态文件的读写。状态文件本身不进 knowledge/、也不会被 `git add -- knowledge/` 带进 commit；
#    路径护栏对它的排除见下方 OUTSIDE_BEFORE / guard_or_die 里的 `grep -vxF "$KB_STATE_FILE"`。────
_kb_state_save() {
  local phase="$1" round="$2"
  local sig; sig="$(kb_sig)"
  local dir; dir="$(dirname -- "$KB_STATE_FILE")"
  [ "$dir" = "." ] || mkdir -p "$dir" 2>/dev/null || true
  local tmp; tmp="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }
  printf '{"phase":"%s","round":%s,"kb_signature":"%s","rounds_run":%s,"agent_oks":%s,"timestamp":"%s"}\n' \
    "$phase" "$round" "$sig" "${rounds_run:-0}" "${_KB_AGENT_OKS:-0}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp" \
    && mv "$tmp" "$KB_STATE_FILE" || rm -f "$tmp"
}
_kb_state_clear() { rm -f "$KB_STATE_FILE" 2>/dev/null || true; }

RESUME=0
for _kb_arg in "$@"; do [ "$_kb_arg" = "--resume" ] && RESUME=1; done
_KB_RESUME_PHASE=""; _KB_RESUME_ROUND=0
if [ "$RESUME" -eq 1 ]; then
  if [ -s "$KB_STATE_FILE" ] && command -v jq >/dev/null 2>&1; then
    _KB_RESUME_PHASE="$(jq -r '.phase // empty' "$KB_STATE_FILE" 2>/dev/null)"
    _KB_RESUME_ROUND="$(jq -r '.round // 0' "$KB_STATE_FILE" 2>/dev/null)"
    case "$_KB_RESUME_ROUND" in ''|*[!0-9]*) _KB_RESUME_ROUND=0 ;; esac
    if [ -z "$_KB_RESUME_PHASE" ]; then
      log "--resume：状态文件存在但无法解析，按全新运行开始"
      _KB_RESUME_PHASE=""; _KB_RESUME_ROUND=0
    else
      # F12/F13：phase 白名单校验——状态文件被写坏成未知值时（如 "roun"），不能静默当成某个已知阶段
      # 跳过对应轮次（那样会打印「已完成」却其实什么都没跳过验证过），按全新运行开始更安全。
      case "$_KB_RESUME_PHASE" in
        incr|full|verify|review|fix|docs)
          log "--resume：从状态文件恢复，phase=$_KB_RESUME_PHASE round=$_KB_RESUME_ROUND"
          ;;
        *)
          log "--resume：状态文件 phase='$_KB_RESUME_PHASE' 不在白名单（incr|full|verify|review|fix|docs），按全新运行开始"
          _KB_RESUME_PHASE=""; _KB_RESUME_ROUND=0
          ;;
      esac
    fi
  else
    log "--resume：状态文件不存在或不可读，按全新运行开始"
  fi
fi

# ── 退出清理：删临时文件、还原本地被换掉的 .claude/（CI 是一次性容器、这些都是空操作）；正常退出（exit 0，
#    含各早退路径）额外清掉断点续跑状态文件，避免下次误续。────────────────────────────────
# 只处理正常退出与脚本内 exit；被信号硬杀时不保证清理（本地残留可下次重跑覆盖，无害）。
_KB_SET_STATE=""; _KB_SET_BAK=""   # settings.json：keep=原有(还原)/drop=原无(删除)
_KB_CMD_STATE=""; _KB_CMD_BAK=""   # commands/kb-refresh.md 同上
_KB_TMPS=()
_KB_AGENT_FAILS=0; _KB_AGENT_OKS=0
# F1：--resume 从 full/verify/review/fix/docs 阶段续跑时，本进程的增量同步循环可能整段被跳过（零次调用），
# 若不恢复此前已成功的调用数，会被后面“全程零成功→exit 5”的判据误杀，永远开不出 PR。从状态文件的
# agent_oks 字段恢复；F7：旧格式状态文件缺该字段时**不再预置为 1**——那等于无凭据放过"全程零成功"闸
# （旧格式 fallback 比真实记录的 0 更宽松），改为与 phase 白名单同一口径：缺关键字段就不信这份状态
# 文件，按全新运行开始（不续跑）。
if [ -n "$_KB_RESUME_PHASE" ]; then
  if [ -s "$KB_STATE_FILE" ] && command -v jq >/dev/null 2>&1; then
    _KB_AGENT_OKS="$(jq -r '.agent_oks // empty' "$KB_STATE_FILE" 2>/dev/null)"
    case "$_KB_AGENT_OKS" in
      ''|*[!0-9]*)
        log "--resume：状态文件缺 agent_oks 字段（旧格式），不可信、按全新运行开始"
        _KB_AGENT_OKS=0; _KB_RESUME_PHASE=""; _KB_RESUME_ROUND=0
        ;;
    esac
  else
    log "--resume：状态文件不存在或不可读，按全新运行开始"
    _KB_AGENT_OKS=0; _KB_RESUME_PHASE=""; _KB_RESUME_ROUND=0
  fi
  [ -n "$_KB_RESUME_PHASE" ] && log "--resume：恢复历史 agent 成功调用数 agent_oks=$_KB_AGENT_OKS"
fi
_kb_cleanup() {
  local rc=$?
  [ "$rc" -eq 0 ] && _kb_state_clear
  # F8：cp 还原失败时不删备份——原实现无条件 rm 掉唯一备份，还原失败又静默无日志，本地含 hooks 的
  # .claude/settings.json 会被永久顶掉（.gitignore 拦着 .claude/*，git 也救不回来）。只在还原成功后才删备份。
  if [ "$_KB_SET_STATE" = keep ]; then
    if cp "$_KB_SET_BAK" .claude/settings.json 2>/dev/null; then
      rm -f "$_KB_SET_BAK"
    else
      log "FATAL: 还原 .claude/settings.json 失败，备份保留在 $_KB_SET_BAK"
    fi
  elif [ "$_KB_SET_STATE" = drop ]; then
    rm -f .claude/settings.json
  fi
  if [ "$_KB_CMD_STATE" = keep ]; then
    if cp "$_KB_CMD_BAK" .claude/commands/kb-refresh.md 2>/dev/null; then
      rm -f "$_KB_CMD_BAK"
    else
      log "FATAL: 还原 .claude/commands/kb-refresh.md 失败，备份保留在 $_KB_CMD_BAK"
    fi
  elif [ "$_KB_CMD_STATE" = drop ]; then
    rm -f .claude/commands/kb-refresh.md
  fi
  [ "$_KB_CMD_STATE" = drop ] && rmdir .claude/commands .claude 2>/dev/null || true
  [ "${#_KB_TMPS[@]}" -gt 0 ] && rm -f "${_KB_TMPS[@]}" 2>/dev/null || true
}
trap _kb_cleanup EXIT

# ── 前置 ────────────────────────────────────────────────────
command -v git >/dev/null 2>&1 || { log "FATAL: 无 git"; exit 2; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { log "FATAL: 不在 git 工作区"; exit 2; }
[ -d "$KB_DIR" ] || { log "FATAL: 无 $KB_DIR 目录"; exit 2; }
# F12/F13：COMMIT_WINDOW_DAYS 须正整数（无前导零，否则 [ -ge ]/[ -gt ] 当八进制报错、或被静默当字符串比出
# 错误结果）；CHECKLIST_MAX 同规则但额外允许 0（0=不限）。
case "$COMMIT_WINDOW_DAYS" in ''|*[!0-9]*|0*) log "FATAL: COMMIT_WINDOW_DAYS 须为正整数（无前导零），实为 '$COMMIT_WINDOW_DAYS'"; exit 2 ;; esac
case "$CHECKLIST_MAX" in
  ''|*[!0-9]*) log "FATAL: CHECKLIST_MAX 须为非负整数（无前导零），实为 '$CHECKLIST_MAX'"; exit 2 ;;
  0) ;;   # 0=不限，允许
  0[0-9]*) log "FATAL: CHECKLIST_MAX 须为非负整数（无前导零），实为 '$CHECKLIST_MAX'"; exit 2 ;;
  *) ;;
esac
if [ -z "${ANTHROPIC_BASE_URL:-}" ]; then unset ANTHROPIC_BASE_URL 2>/dev/null || true; fi
# 直调 claude 须把 PR_AGENT_MODEL 映射到 claude 认的 ANTHROPIC_MODEL*
if [ -n "${PR_AGENT_MODEL:-}" ]; then
  export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-$PR_AGENT_MODEL}"
  export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-$PR_AGENT_MODEL}"
  export ANTHROPIC_DEFAULT_OPUS_MODEL="${ANTHROPIC_DEFAULT_OPUS_MODEL:-$PR_AGENT_MODEL}"
  export ANTHROPIC_DEFAULT_SONNET_MODEL="${ANTHROPIC_DEFAULT_SONNET_MODEL:-$PR_AGENT_MODEL}"
  export ANTHROPIC_DEFAULT_HAIKU_MODEL="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-$PR_AGENT_MODEL}"
else
  log "WARN: 未配置 PR_AGENT_MODEL —— 将用 claude 内置默认模型，建议在仓库 vars 配置"
fi

HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
log "baseline HEAD=$HEAD_SHA  KB_DIR=$KB_DIR  COMMIT_WINDOW_DAYS=$COMMIT_WINDOW_DAYS  CHECKLIST_MAX=$CHECKLIST_MAX"

# ── 去重（尽早）：周更常态是「上周 PR 还没人合」。已有未合并同类 PR 就别再空烧一整轮。仅 CI 适用。──
# 两路查：A 按标签 kb-auto-refresh（服务端过滤、不受列表条数限制）；B 按分支名 auto/kb-refresh-* 前缀 +
# 作者 github-actions[bot]（兜住"标签建失败退回无标签"开出的 PR）。任一路查询失败即 fail-loud exit 6，
# 不在"查不清是否已有 PR"时盲目开 PR。作者过滤绑定内置 GITHUB_TOKEN（若改用 PAT/App token 需同步改）。
if [ "$OPEN_PR" != "no" ] && [ -n "${GITHUB_ACTIONS:-}" ] && command -v gh >/dev/null 2>&1 && [ -n "${GH_TOKEN:-}" ]; then
  # 先幂等建标签（已存在则忽略）：否则首跑标签不存在时 route A 的 --label 查询会非零、被误判失败。
  gh label create kb-auto-refresh --repo "$REPO" --color 1d76db --description "知识库每周自动刷新机器人开的 PR" >/dev/null 2>&1 || true
  _gh_err="$(mktemp)"; _KB_TMPS+=("$_gh_err")
  _pr_labeled="$(gh pr list --repo "$REPO" --state open --label kb-auto-refresh --json number --jq '.[0].number // empty' 2>"$_gh_err")"; _rcA=$?
  _pr_branch="$(gh pr list --repo "$REPO" --state open --limit 2000 --json number,headRefName,author --jq '[.[]|select((.headRefName|test("^auto/kb-refresh-[0-9]{8}-[0-9]{6}$")) and (.author.login=="github-actions[bot]"))]|.[0].number // empty' 2>>"$_gh_err")"; _rcB=$?
  if [ "$_rcA" -ne 0 ] || [ "$_rcB" -ne 0 ]; then
    log "FATAL: 查询现有 PR 失败（gh rcA=$_rcA rcB=$_rcB）—— 拒绝盲目开 PR，exit 6（若 stderr 含 label 报错多半是缺 issues:write）"
    sed 's/^/  gh stderr: /' "$_gh_err" | head -5
    exit 6
  fi
  existing_pr="$_pr_labeled"; [ -z "$existing_pr" ] && existing_pr="$_pr_branch"
  if [ -n "$existing_pr" ]; then
    log "已存在未合并的知识库刷新 PR #$existing_pr —— 跳过本轮，待其处理后重跑"
    exit 0
  fi
fi

# ── commit 覆盖清单：近 N 天、动过代码目录的 commit（喂阶段1 每轮增量同步用）───────
# 超 CHECKLIST_MAX 条只取最近 N 条（防 prompt 过大），但如实标注真实总数、不拦截；被截掉的更早
# commit 由阶段1 三轮双向逐文件复审兜底。CHECKLIST_MAX=0（默认）表示不限，全量纳入、不截断也不打警告。
# pathspec 显式列举代码顶层目录（新增顶层目录时同步）。
_CL_ALL="$(git log --since="${COMMIT_WINDOW_DAYS} days ago" --no-merges \
  --pretty='- %h %s' -- backend web scripts cli plugins 2>/dev/null)"
CHECKLIST_TOTAL="$(printf '%s\n' "$_CL_ALL" | grep -c '^- ' || true)"
case "$CHECKLIST_TOTAL" in ''|*[!0-9]*) CHECKLIST_TOTAL=0 ;; esac
if [ "$CHECKLIST_MAX" -eq 0 ]; then
  CHECKLIST_BLOCK="$_CL_ALL"
else
  CHECKLIST_BLOCK="$(printf '%s\n' "$_CL_ALL" | head -n "$CHECKLIST_MAX")"
fi
if [ "$CHECKLIST_TOTAL" -eq 0 ]; then
  CHECKLIST_BLOCK="(近 ${COMMIT_WINDOW_DAYS} 天无动过代码的 commit)"
  CHECKLIST_NOTE="近 ${COMMIT_WINDOW_DAYS} 天无动过代码的 commit"
elif [ "$CHECKLIST_MAX" -ne 0 ] && [ "$CHECKLIST_TOTAL" -gt "$CHECKLIST_MAX" ]; then
  CHECKLIST_NOTE="⚠️ 近 ${COMMIT_WINDOW_DAYS} 天共 ${CHECKLIST_TOTAL} 条 commit，阶段1 只取最近 ${CHECKLIST_MAX} 条（其余由双向逐文件复审兜底）；不拦截，请人工留意"
  log "WARN: $CHECKLIST_NOTE"
else
  CHECKLIST_NOTE="近 ${COMMIT_WINDOW_DAYS} 天共 ${CHECKLIST_TOTAL} 条 commit，已全部纳入阶段1 覆盖清单"
fi
log "commit 覆盖清单：$CHECKLIST_NOTE"

# ── 台账（脚本维护，仅用于 PR 描述；agent 不碰）──────
if [ -n "${KB_LEDGER:-}" ]; then LEDGER="$KB_LEDGER"; else LEDGER="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$LEDGER"); fi
printf '# kb-refresh 台账（HEAD=%s）\n\n' "$HEAD_SHA" > "$LEDGER"
INCR_SUMMARY=""; FULL_SUMMARY=""; VERIFY_SUMMARY=""; REVIEW_SUMMARY=""; FIX_SUMMARY=""; ISSUE_COUNT=0

# 写 agent 配置：① 只读 Bash 白名单 settings.json（无它 agent 每条命令都弹权限确认、无人值守会卡死）；
# ② /kb-refresh 命令文件。每次调用前重写（见 _run_claude），防上一轮 agent 用 Write 篡改后带入下一轮。
# 写范围的最终硬保证不是白名单（前缀匹配挡不住尾随重定向），而是事后路径护栏；但护栏只能发现
# git 状态里新出现的路径，对已存在且被 .gitignore 排除的文件被就地改写内容，git 感知不到、护栏也
# 发现不了（F2），本地跑请自行留意。
# $1=allow 模式，write（默认，含 Edit/Write）| readonly（不含 Edit/Write，供阶段4 只读 review 用）。
_kb_write_agent_cfg() {
  local mode="${1:-write}"
  mkdir -p .claude/commands || { log "FATAL: 无法创建 .claude/commands"; exit 2; }
  local allow='["Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(git show*)", "Bash(git rev-parse*)", "Bash(rg *)", "Bash(grep *)", "Bash(ls)", "Bash(ls *)", "Bash(jq *)"]'
  [ "$mode" = "readonly" ] || allow='["Edit", "Write", "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(git show*)", "Bash(git rev-parse*)", "Bash(rg *)", "Bash(grep *)", "Bash(ls)", "Bash(ls *)", "Bash(jq *)"]'
  printf '{"permissions": {"allow": %s}}' "$allow" > .claude/settings.json || { log "FATAL: 写入 .claude/settings.json 失败"; exit 2; }
  # 内容校验而非只查非空：cat 因重定向失败（如目标文件只读）时，若目标原本非空，`[ -s ]` 会误判为成功，
  # 静默沿用开发者原版 settings.json（含 hooks），架空"每次调用前重写防篡改"的立意。
  grep -q '"permissions"' .claude/settings.json 2>/dev/null || { log "FATAL: 写入 .claude/settings.json 失败"; exit 2; }
  cp "$CMD_SRC" .claude/commands/kb-refresh.md || { log "FATAL: 写入 .claude/commands/kb-refresh.md 失败"; exit 2; }
  [ -s .claude/commands/kb-refresh.md ] || { log "FATAL: .claude/commands/kb-refresh.md 为空"; exit 2; }
}

# ── 装命令（本地会临时把开发者的 .claude/settings.json + commands/kb-refresh.md 换成 bot 版、退出还原；
#    别在同一仓库一边跑本机 bot 一边交互用 Claude Code。CI 无此顾虑）────
[ -f "$CMD_SRC" ] || { log "FATAL: 方法论 $CMD_SRC 缺失"; exit 2; }
command -v claude >/dev/null 2>&1 || { log "FATAL: 无 claude CLI"; exit 2; }
mkdir -p .claude/commands || { log "FATAL: 无法创建 .claude/commands"; exit 2; }
# 备份开发者原文件（原有→退出还原；原无→退出删除）。先 cp 成功再置 keep；备份失败即中止，绝不在没备份成功时覆盖。
if [ -f .claude/settings.json ]; then _KB_SET_BAK="$(mktemp)"; cp .claude/settings.json "$_KB_SET_BAK" || { log "FATAL: 备份 .claude/settings.json 失败"; exit 2; }; _KB_SET_STATE=keep; else _KB_SET_STATE=drop; fi
if [ -f .claude/commands/kb-refresh.md ]; then _KB_CMD_BAK="$(mktemp)"; cp .claude/commands/kb-refresh.md "$_KB_CMD_BAK" || { log "FATAL: 备份 .claude/commands/kb-refresh.md 失败"; exit 2; }; _KB_CMD_STATE=keep; else _KB_CMD_STATE=drop; fi
_kb_write_agent_cfg   # 早失败校验（配置能写成功再往下跑）

# ── claude 是否遇到可重试故障（429/限流/过载/超时/5xx）；$1=输出文件 $2=进程 rc $3=stderr 文件（可空）
#    F2：改用带上下文的模式，不再裸匹配三位数字（否则 offset":500、第 512 行、turns=500 都会误命中）；
#    F3：只在 [DONE] 行与 claude 原始 stderr 里找，不在 agent 全量流水（assistant/tool_use/tool_result）
#        上全文匹配——那些文本内容不可控，裸模式再收紧也挡不住"agent 引用了一段含 500 的日志"这类误报。──
#    F4：追加兜底——真 429/限流若以纯文本落在 claude 原始 stdout（非 JSON，jq 解析失败、$out 看不到
#        [DONE]）时，[DONE] 行与 jq 加工后的 $out 都看不到它；此时才退回对 claude 未经 jq 加工的原始
#        stdout（$raw）里的非 JSON 行（grep -v '^{'）跑同一套模式。M3：旧条件「jqrc≠0 或无 [DONE]」几乎
#        等价于「本次调用失败」，每次失败都会去扫 $raw（含 agent 每一次 tool_result 原文），裸词
#        "time ?out" 在仓库任意 Read/Grep 到的 timeout 相关文本上都会命中——收紧为「$out 无 [DONE] 行且
#        jq 解析失败」，只覆盖"claude 根本没吐完整结果、纯文本报错"这一种真正要救的场景，且只扫非 JSON
#        行，不碰 tool_result 正文。F3（第三轮修复过严）：门槛原本要求 $out 完全为空（[ ! -s "$out" ]），
#        但 claude 若先吐了几行合法 stream-json（$out 已有 [assistant] 等行）、之后才以纯文本报 429，
#        $out 不为空却仍然没有 [DONE]——门槛不通过导致兜底不扫 raw、误判不可重试。改为判 $out 里有没有
#        [DONE] 行，而非判 $out 是否为空。
_kb_is_retryable() {
  local out="$1" rc="$2" err="${3:-}" raw="${4:-}" jqrc="${5:-0}"
  [ "$rc" -eq 124 ] && return 0   # 外部 timeout 包装器的超时退出码
  local pat='HTTP/[0-9.]+ (429|5[0-9][0-9])|status(_code)?[ :=]+(429|5[0-9][0-9])|rate_limit_error|overloaded_error|429 Too Many|rate.?limit|overloaded|time ?out'
  grep '^\[DONE\]' "$out" 2>/dev/null | grep -qiE "$pat" && return 0
  [ -n "$err" ] && grep -qiE "$pat" "$err" 2>/dev/null && return 0
  if [ -n "$raw" ] && ! grep -q '^\[DONE\]' "$out" 2>/dev/null && [ "$jqrc" -ne 0 ]; then
    grep -v '^{' "$raw" 2>/dev/null | grep -qiE "$pat" && return 0
  fi
  return 1
}

# ── claude 单次调用（不含重试）：$1=prompt, $2=输出文件, $3=stderr 文件, $4=claude 原始 stdout 落盘文件
#    （F4：供 _kb_is_retryable 在 jq 解析失败时兜底扫限流模式）；$5=--tools 参数（默认含 Edit/Write，
#    阶段4 只读 review 传不含写工具的版本）；成功 return 0，失败 return 1
#    （结果记入 _KB_LAST_RC / _KB_LAST_JQ_RC）。──
_run_claude_once() {
  local prompt="$1" out="$2" err="$3" raw="$4" tools="${5:-Bash,Read,Glob,Grep,Edit,Write}"
  local _kb_ps
  stdbuf -oL claude \
    --permission-mode acceptEdits \
    --tools "$tools" \
    --verbose --output-format stream-json -p "$prompt" < /dev/null 2> "$err" \
    | tee "$raw" \
    | jq --unbuffered -rc '
        def T: if length>300 then .[:300]+"…" else . end;
        # oneline：把记录内换行折叠成空格，保证每条 jq 记录单行——否则 assistant 多行文本里以 "[DONE]"
        # 开头的续行会被下方 grep `^\[DONE\]` 误判成结束标记。
        def oneline: gsub("\r?\n";" ");
        if .type=="assistant" then .message.content[]?
          | if .type=="text" then "[assistant] "+(.text|oneline)
            elif .type=="tool_use" then "[tool_use] "+.name+": "+(.input|tostring|T|oneline)
            else empty end
        elif .type=="user" then .message.content[]?
          | if .type=="tool_result" then "[tool_result]"+(if .is_error then " (ERROR)" else "" end)+" "+((.content|if type=="string" then . else tostring end)|T|oneline)
            else empty end
        elif .type=="result" then "[DONE] subtype="+((.subtype//"?")|tostring)+" is_error="+((.is_error//false)|tostring)+" cost=$"+((.total_cost_usd//0)|tostring)+", turns="+((.num_turns//0)|tostring)+(if (.is_error//false) and ((.result//"")!="") then " error="+((.result)|T|oneline) else "" end)
        else empty end' | tee "$out"
  # PIPESTATUS 只在紧跟管道之后的下一条语句里有效——包括 `local` 声明本身也是一条简单命令，会把它
  # 重置成单元素数组；`_kb_ps` 必须提前在函数顶部 `local` 好，这里只做纯赋值（管道后的第一条语句），
  # 再从数组里拆分，否则分两条语句分别取值会在第二次读到 `set -u` 下的"未绑定变量"。
  _kb_ps=("${PIPESTATUS[@]}")
  _KB_LAST_RC="${_kb_ps[0]}"
  _KB_LAST_JQ_RC="${_kb_ps[2]}"
  # 防假绿：agent 没跑起来/异常收尾时，零编辑与"本就同步"难分。只有"rc=0 + 有 [DONE] + 非 is_error"算成功。
  if [ "$_KB_LAST_RC" -ne 0 ] || ! grep -q '^\[DONE\]' "$out" || grep -q '^\[DONE\].*is_error=true' "$out"; then
    log "WARN: agent 可能异常（rc=$_KB_LAST_RC，未见 [DONE] 或 [DONE] 标 is_error=true）；勿据此误判为已同步。"
    _KB_AGENT_FAILS=$((_KB_AGENT_FAILS + 1))
    return 1
  else
    _KB_AGENT_OKS=$((_KB_AGENT_OKS + 1))
    return 0
  fi
}

# ── 统一的 claude 调用入口：$1=prompt, $2=输出文件, $3=可选，claude 原始 stdout 的落盘副本路径（供调用方
#    在成功后从中还原未折叠换行的 assistant 文本，见 _kb_extract_assistant_text；不传则不留副本）。
#    $4=readonly（传 1 时不带 Edit/Write 工具，供阶段4 只读 review 用；默认含写工具）。
#    失败按 KB_BACKOFF_SECS 退避重试；不可重试或退避耗尽仍失败 → fail-loud exit 5（与 workflow 承诺的口径一致）。
_run_claude() {
  local prompt="$1" out="$2" keep_raw="${3:-}" readonly="${4:-0}"
  local err; err="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }
  _KB_TMPS+=("$err")
  local raw; raw="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }
  _KB_TMPS+=("$raw")
  local backoffs; read -r -a backoffs <<< "$KB_BACKOFF_SECS"
  local attempt=0
  local tools="Bash,Read,Glob,Grep,Edit,Write" cfg_mode="write"
  if [ "$readonly" = "1" ]; then
    tools="Bash,Read,Glob,Grep"
    cfg_mode="readonly"
  fi
  while :; do
    _kb_write_agent_cfg "$cfg_mode"   # 每次调用前重置白名单 + 命令文件，防上一轮篡改
    # m5：成功即释放本次调用的 raw/err（含完整会话转录），不等到 EXIT trap 才清。仍留在 _KB_TMPS 里兜底
    # （rm -f 幂等，EXIT trap 重复删不报错）。
    if _run_claude_once "$prompt" "$out" "$err" "$raw" "$tools"; then
      if [ -n "$keep_raw" ]; then
        cp "$raw" "$keep_raw" || { log "FATAL: 保留 claude 原始 stdout 副本失败（cp $raw -> $keep_raw），fail-loud，exit 5"; exit 5; }
      fi
      rm -f "$raw" "$err"
      return 0
    fi
    if ! _kb_is_retryable "$out" "$_KB_LAST_RC" "$err" "$raw" "$_KB_LAST_JQ_RC"; then
      log "FATAL: claude 调用失败且判定不可重试（如认证失败/参数错误），fail-loud，exit 5"
      exit 5
    fi
    if [ "$attempt" -ge "${#backoffs[@]}" ]; then
      log "FATAL: 已用尽 ${#backoffs[@]} 次退避重试仍失败，fail-loud，exit 5"
      exit 5
    fi
    local secs="${backoffs[$attempt]}"
    attempt=$((attempt + 1))
    log "第 ${attempt} 次退避，睡 ${secs} 秒，原因：claude 调用疑似遇到 429/限流/过载/超时（rc=$_KB_LAST_RC）"
    sleep "$secs"
  done
}
# 无改动退出前调用：全程无一次成功的 agent 调用时，"无改动"不可信（系统性失败）→ exit 5，防静默停摆。
_die_if_agent_failed() {
  if [ "$_KB_AGENT_OKS" -eq 0 ]; then
    log "FATAL: 全程无一次成功的 agent 调用（异常 $_KB_AGENT_FAILS 次）；'无改动'不可信 —— exit 5"
    exit 5
  fi
  [ "$_KB_AGENT_FAILS" -gt 0 ] && log "WARN: 有 $_KB_AGENT_FAILS 次 agent 异常，但也有成功且最终无改动 → 视作已同步"
  return 0
}
# 取 agent 末尾小结（写在回复末尾）；仅用于台账/PR 描述展示，不参与判定。
_tail_summary() { grep '^\[assistant\] ' "$1" 2>/dev/null | tail -6 | sed 's/^\[assistant\] //'; }
# 把 stdin 截到最多 $1 字节，iconv -c 丢弃截断处残缺多字节（无 iconv 则原样）。
_kb_emit_capped() {
  if command -v iconv >/dev/null 2>&1; then head -c "$1" | iconv -f UTF-8 -t UTF-8 -c 2>/dev/null
  else head -c "$1"; fi
}
# ── 阶段4/阶段5 强制清单/问题回报用：从 claude 原始 stdout（未经 oneline 折叠）里还原 assistant 文本，
#    保留真实换行，供 grep '^[ISSUE]'/'^[FIXED]' 之类的行首锚点精确计数——$out 里每条
#    assistant 消息已被 _run_claude_once 的 oneline 折成单行，行首锚点在消息内部多条回报之间无法区分。
_kb_extract_assistant_text() {
  local raw="$1"
  jq -r 'select(.type=="assistant") | .message.content[]? | select(.type=="text") | .text + "\n"' "$raw" 2>/dev/null
}

# ── 阶段1 增量同步（一个全新独立 agent，本轮对清单逐条重新核，不许因"上轮已覆盖"跳过）────
run_incremental() {
  local round="$1"
  INCR_SUMMARY=""
  local out; out="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$out")
  local prompt
  prompt="$(printf '/kb-refresh --mode incr --round %s --max 3 --kb %s --ci\n\n参考·commit 清单（%s）——逐条确认其 L1/L2 变更是否已在知识库反映（纯 L3 无需入库）。**不管前几轮是否已经核过、本轮一律对清单里每一条重新核一遍，禁止以"上轮已覆盖"为由跳过**。清单若为"最近 N 条"截断，更早的靠你对 knowledge/ 与代码的双向全量通读兜底，别只依赖此清单：\n%s' \
    "$round" "$KB_DIR" "$CHECKLIST_NOTE" "$CHECKLIST_BLOCK")"
  _run_claude "$prompt" "$out"
  INCR_SUMMARY="$(_tail_summary "$out")"; [ -n "$INCR_SUMMARY" ] || INCR_SUMMARY="(本轮无文字小结)"
  rm -f "$out"
}

# ── 阶段2 全量同步（按 .agents/commands/kb-refresh.md 的全量同步方法，对照代码现状全量核一遍
#    knowledge/，补漏/改正/删过时；无数值闸、不重跑）────────────────────────
run_full_sync() {
  FULL_SUMMARY=""
  local out; out="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$out")
  local prompt
  prompt="$(printf '/kb-refresh --mode full --kb %s --ci\n\n【全量同步】按 %s 的全量同步方法，对照代码现状全量核 %s/，按 %s/README.md 的 L1/L2/L3 规范补漏、改错、删掉不该有的 L3。' \
    "$KB_DIR" "$CMD_SRC" "$KB_DIR" "$KB_DIR")"
  _run_claude "$prompt" "$out"
  FULL_SUMMARY="$(_tail_summary "$out")"; [ -n "$FULL_SUMMARY" ] || FULL_SUMMARY="(全量同步无文字小结)"
  rm -f "$out"
}

# ── 阶段3 完整审查（对本次全部改动 + 代码实现做正确性校验，发现问题就地改）────────
run_verify() {
  VERIFY_SUMMARY=""
  local out; out="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$out")
  local prompt
  prompt="$(printf '你是独立的知识库审查 agent，只改 %s 下文件。【阶段3·完整审查】前两个阶段已改动知识库。跑 `git diff -- %s` 看本次全部改动，逐条核对每处新表述是否被代码证实（用 Grep/Read 回代码核，防幻觉式错改）；发现问题【就地改对】。末尾逐条给出核对结论。' \
    "$KB_DIR" "$KB_DIR")"
  _run_claude "$prompt" "$out"
  VERIFY_SUMMARY="$(_tail_summary "$out")"; [ -n "$VERIFY_SUMMARY" ] || VERIFY_SUMMARY="(完整审查无文字小结)"
  rm -f "$out"
}

# ── 阶段4 review（只读，只出 [ISSUE] 清单，不许改文件）：--tools 不含 Edit/Write（见 _run_claude 的
#    readonly=1），护栏之外的第二道硬保证；调用前后各取一次 kb_sig()，签名变了说明仍被改动，fail-loud。
run_review_readonly() {
  REVIEW_SUMMARY=""
  local _sig_before; _sig_before="$(kb_sig)"
  local prompt
  prompt="$(printf '你是严格的 PR reviewer（只读，单轮）。跑 `git diff -- %s` 看本次对知识库的全部改动，按下述维度审查：\n- 正确性：每条新表述能否被代码证实（防幻觉式错改，用 Grep/Read 回代码核）；\n- 规范：符合 knowledge/README.md 三档（L1/L2 留、L3 不写、指路不复制、最小 diff）；\n- 质量：表述准确、无冗余、无自相矛盾、内链有效。\n**只报问题，不许用 Edit/Write 改任何文件**。回报格式钉死、每条一行：\n[ISSUE] <文件:行号> | <问题> | <建议改法>\n无问题则不输出任何 [ISSUE] 行。' \
    "$KB_DIR")"
  local out raw
  out="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$out")
  raw="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$raw")
  _run_claude "$prompt" "$out" "$raw" 1
  REVIEW_SUMMARY="$(_tail_summary "$out")"; [ -n "$REVIEW_SUMMARY" ] || REVIEW_SUMMARY="(review 无文字小结)"
  _kb_extract_assistant_text "$raw" | grep '^\[ISSUE\]' > "$ISSUE_FILE" 2>/dev/null || true
  rm -f "$out" "$raw"
  # 阶段4 只读结束，恢复含 Edit/Write 的 agent 配置，供阶段5 解决使用。
  _kb_write_agent_cfg
  ISSUE_COUNT="$(grep -c '^\[ISSUE\]' "$ISSUE_FILE" 2>/dev/null || true)"
  case "$ISSUE_COUNT" in ''|*[!0-9]*) ISSUE_COUNT=0 ;; esac
  local _sig_after; _sig_after="$(kb_sig)"
  if [ "$_sig_before" != "$_sig_after" ]; then
    log "FATAL: 阶段4 应只读但改了文件"
    exit 3
  fi
}

# ── 阶段5 解决（独立 agent，拿阶段4 的 [ISSUE] 清单原样贴入，逐条解决，回报 [FIXED]）────────
run_fix() {
  local issue_list_file="$1"
  FIX_SUMMARY=""
  local out raw
  out="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$out")
  raw="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$raw")
  local issue_text; issue_text="$(cat "$issue_list_file")"
  local prompt
  prompt="$(printf '你是独立的知识库矫正 agent，只改 %s 下文件。下面是阶段4 review 给出的问题清单（原样贴入，逐条解决，一条不许漏）：\n%s\n\n每解决一条回报一行：\n[FIXED] <文件:行号> | <怎么改的>' \
    "$KB_DIR" "$issue_text")"
  _run_claude "$prompt" "$out" "$raw"
  FIX_SUMMARY="$(_tail_summary "$out")"; [ -n "$FIX_SUMMARY" ] || FIX_SUMMARY="(fix 无文字小结)"
  _kb_extract_assistant_text "$raw" > "$FIX_REPORT"
  rm -f "$out" "$raw"
}
# ── F5+F6：porcelain 展开为「一路径一行」列表。-uall 把未跟踪目录展开到文件粒度（否则新建
#    knowledge/08-new/ 只会折叠成一条 `knowledge/08-new/`，既不匹配 `^knowledge/`、后缀也不是 .md，
#    两道护栏都会误判/漏判）；-z 按 NUL 切分，rename/copy 记录的新路径与旧路径是两个独立字段
#    （`R  new\0old\0`），必须拆开分别过越界与后缀判定——原先 `sed 's/^...//'` 按行文本处理时，
#    `R  knowledge/a.md -> docs/a.md` 这一整行文本行首匹配 `^knowledge/`、行尾匹配 `\.md$`，会被两道
#    护栏一起放过，实际文件却已经搬出 knowledge/。若仍出现以 / 结尾的记录（-uall 理应已展开、不应发生），
#    原样吐出，交由调用方显式判定为异常，不能只靠后缀判断静默漏判。────────────────────────
# F2：git 状态先落盘、再解析（不再用进程替换套 git）——进程替换的生产者是子 shell，`while … < <(git …)`
# 看不见 git 自身的退出码，函数恒 rc=0；git 失败（如仓库损坏/权限问题）时护栏会 fail-open。必须在主
# shell 里直接调用（不可套进命令替换），否则失败时的 exit 出不了子 shell（同 F3 的陷阱）。
_kb_git_status_dump() {
  local f="$1"
  git -c core.quotepath=false status --porcelain -z -uall > "$f"
}
# F8：库内命中 .gitignore 的文件（如误产出的临时文件/密钥）对普通 status 与 ls-files 双盲，额外
# 扫一路 --ignored=traditional，只限定在 $KB_DIR 下（避免仓库其它角落的历史 ignore 噪声）。
# M1：`--ignored=matching` 命中 ignore 的目录只会吐一条目录级记录、不下钻到文件——若基线运行前该
# ignore 目录已存在（本地脏工作区/上次 exit≠0 残留），差集比对会认为"这个目录本来就有"而放过目录里
# 之后新写的任意文件。改用 `--ignored=traditional`（配 `-uall`）才会下钻到文件粒度逐个吐出。
_kb_git_ignored_dump() {
  local f="$1"
  git -c core.quotepath=false status --porcelain -z -uall --ignored=traditional -- "$KB_DIR" > "$f"
}

# 把 git status -z 转储（$1=文件路径，$2=可选的 XY 状态码过滤器，如 '!!' 只留 ignored 记录）解析为
# 「一路径一行」列表，从磁盘文件读取。$2 留空则不过滤（原行为）。
# F8：`--ignored=traditional` 配 `-uall` 时不会只吐 ignored 记录，普通未跟踪（??）也照样混在一起输出，
# 必须按 XY 精确过滤成 `!!` 才是真正命中 .gitignore 的文件，否则会把库内任何合法新增未跟踪文件
# 都误判成"命中 ignore"。过滤判断放在读完 rename/copy 的 orig 字段之后，避免跳过整条记录后
# 字段流错位（下一次 read 会把该记录的 orig 误当成新记录的 XY+path）。
# F1：path（或 rename/copy 记录的 old path）只要含换行符，视为不可信护栏输入——`-uall -z` 不加引号地
# 原样吐出路径，含换行的路径会被下面 `printf '%s\n' "$path"` 劈成两行，把一条越界记录伪装成两条无害记录、
# 逃过越界/后缀判定。发现即 return 9（不 exit——本函数经常在命令替换里执行，exit 出不了子 shell，
# 同 F3 陷阱），调用方须在主 shell 检查返回码后自行 exit 2。FATAL 消息直接写 stderr，不走 log()（避免
# 污染被命令替换捕获的 stdout）。
_kb_parse_status_file() {
  local f="$1" only_xy="${2:-}"
  local field XY path orig
  while IFS= read -r -d '' field; do
    XY="${field:0:2}"; path="${field:3}"
    case "$path" in
      *$'\n'*)
        printf '[kb-refresh] FATAL（护栏）：路径含换行符，拒绝放行：%s\n' "$(printf '%s' "$path" | tr '\n' '¶')" >&2
        return 9 ;;
    esac
    orig=""
    case "$XY" in
      *R*|*C*)
        IFS= read -r -d '' orig || true
        case "$orig" in
          *$'\n'*)
            printf '[kb-refresh] FATAL（护栏）：路径含换行符，拒绝放行：%s\n' "$(printf '%s' "$orig" | tr '\n' '¶')" >&2
            return 9 ;;
        esac
        ;;
    esac
    [ -n "$only_xy" ] && [ "$XY" != "$only_xy" ] && continue
    printf '%s\n' "$path"
    [ -n "$orig" ] && printf '%s\n' "$orig"
  done < "$f"
  return 0
}

# ── F7：baseline/current 比对改走此函数，而非裸 `grep -vxFf ... || true`——baseline 文件不可读时
#    grep 会以 rc=2 退出、`|| true` 把它和「rc=1 无匹配」一起吞掉，护栏在"读不到基线"时反而无条件放行
#    （fail-open）。这里显式区分：rc=1（无匹配，正常空结果）与 rc>=2（错误）分别处理，rc>=2 一律 fail-closed。
# F3：本函数不再自己 exit——它常年跑在管道+命令替换里（`nw="$(… | _kb_diff_new …)"`），是子 shell，
#    `exit` 只会结束子 shell；而 log() 是 echo（写 stdout），FATAL 文本会被命令替换收进结果变量、被
#    当成"越界文件"一起打印，还把本该 exit 2 的场景错误包装成 exit 3。改为：出错只 return 非 0（2）、
#    不产出任何 stdout，FATAL 消息写 stderr；由主 shell 里的调用方判断返回码后再决定 exit 2。
_kb_diff_new() {
  local baseline="$1"
  if [ ! -r "$baseline" ]; then
    printf '[kb-refresh] FATAL: 护栏基线文件不可读：%s，拒绝放行\n' "$baseline" >&2
    return 2
  fi
  local result rc
  result="$(grep -vxFf "$baseline")"; rc=$?
  if [ "$rc" -ge 2 ]; then
    printf '[kb-refresh] FATAL: 护栏比对失败（grep rc=%s，baseline=%s），拒绝放行\n' "$rc" "$baseline" >&2
    return 2
  fi
  printf '%s' "$result"
  return 0
}

# ── 护栏基线：只追究本次**新引入**的越界/库内非 md/库内命中 ignore 的改动（扣除运行前的既有脏文件；
#    CI 全新 checkout 为空）；显式排除 KB_STATE_FILE（断点续跑状态文件），它本就在 $KB_DIR 之外，写它
#    不算越界。git 状态读取与解析都落盘在主 shell 里直接做（不套命令替换），任何失败都能真正 exit 2。──
_KB_STATUS_TMP="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$_KB_STATUS_TMP")
_kb_git_status_dump "$_KB_STATUS_TMP" || { log "FATAL: 护栏基线读取 git 状态失败（rc=$?），拒绝放行"; exit 2; }
_KB_ALL_BEFORE="$(_kb_parse_status_file "$_KB_STATUS_TMP")"; _kb_rc=$?
[ "$_kb_rc" -eq 9 ] && exit 2   # FATAL 已由 _kb_parse_status_file 写到 stderr
[ "$_kb_rc" -eq 0 ] || { log "FATAL: 护栏基线解析 git 状态失败（rc=$_kb_rc）"; exit 2; }
if printf '%s\n' "$_KB_ALL_BEFORE" | grep -qE '/$'; then
  log "FATAL: 护栏基线遇到未展开到文件粒度的目录级记录（-uall 理应已展开），拒绝继续"; exit 2
fi
OUTSIDE_BEFORE="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$OUTSIDE_BEFORE")
# F1：末尾追加 `grep -v '^$'` 滤掉空行——干净工作区时 $_KB_ALL_BEFORE 是空字符串，`printf '%s\n' ""`
# 会吐出一个空行，若不滤掉会让基线文件变成 1 字节的"看起来非空"文件，误伤下面 --resume 的判据。
printf '%s\n' "$_KB_ALL_BEFORE" | grep -vE "^${KB_DIR}/" | grep -vxF "$KB_STATE_FILE" | grep -v '^$' | LC_ALL=C sort > "$OUTSIDE_BEFORE"
NONMD_BEFORE="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$NONMD_BEFORE")
printf '%s\n' "$_KB_ALL_BEFORE" | grep -E "^${KB_DIR}/" | grep -vE '\.md$' | LC_ALL=C sort > "$NONMD_BEFORE"
# F8：库内命中 .gitignore 的文件对 status/ls-files 双盲，单独一路 --ignored=traditional 兜底。
_KB_IGN_TMP="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$_KB_IGN_TMP")
_kb_git_ignored_dump "$_KB_IGN_TMP" || { log "FATAL: 护栏基线读取库内 ignore 状态失败（rc=$?），拒绝放行"; exit 2; }
_KB_IGN_ALL_BEFORE="$(_kb_parse_status_file "$_KB_IGN_TMP" '!!')"; _kb_rc=$?
[ "$_kb_rc" -eq 9 ] && exit 2
[ "$_kb_rc" -eq 0 ] || { log "FATAL: 护栏基线解析库内 ignore 状态失败（rc=$_kb_rc）"; exit 2; }
# M1：与主扫描同一口径，ignored 列表里出现目录级记录（/$ 结尾）同样 exit 2——traditional 正常不应产生，
# 一旦出现说明前提假设被打破，不能悄悄放行。
if printf '%s\n' "$_KB_IGN_ALL_BEFORE" | grep -qE '/$'; then
  log "FATAL: 护栏基线（库内 ignore）遇到未展开到文件粒度的目录级记录，拒绝继续"; exit 2
fi
IGNORED_BEFORE="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$IGNORED_BEFORE")
# F1：同上，滤掉空行，避免干净工作区时基线文件被 `printf '%s\n' ""` 吐出的空行误判为非空。
printf '%s\n' "$_KB_IGN_ALL_BEFORE" | grep -v '^$' | LC_ALL=C sort > "$IGNORED_BEFORE"
# M4（修法②）：--resume 若基线本身已不干净（越界/非 md/命中 ignore 的文件已残留于工作区——多半是上次
# exit 3 后人没清理就带 --resume 续跑），继续跑会把这些违规文件当"本就存在"洗进新基线、后续差集为空、
# 静默放行甚至被一并 commit。fail-closed：直接拒绝续跑，要求先手工清理工作区（不加 --resume 也可）。
# F1 修法①：判据从 [ -s "$f" ]（字节非空）改为"非空行数 > 0"——基线文件即使已滤掉空行仍可能因
# 其它原因含空行，双重防御。
_kb_has_content() {
  [ -r "$1" ] || { printf '[kb-refresh] FATAL: resume 基线不可读：%s，按有残留处理\n' "$1" >&2; return 0; }
  local c; c="$(grep -cv '^$' "$1" 2>/dev/null)"
  [ -n "$c" ] && [ "$c" -gt 0 ]
}
# 只收窄到 NONMD_BEFORE/IGNORED_BEFORE 两项——这两项才是 agent 产出的 scratch 特征；OUTSIDE_BEFORE
# 里运行前本就存在的库外脏文件是工作区既有状态，与本次 agent 产出无关，不该因它而拒绝续跑。
if [ "$RESUME" -eq 1 ]; then
  if _kb_has_content "$NONMD_BEFORE" || _kb_has_content "$IGNORED_BEFORE"; then
    log "FATAL: --resume 但工作区已有违规残留（非 md/命中 ignore 的文件），拒绝续跑 —— 请先手工清理工作区（核对 git status）后再重跑（可不加 --resume）"
    exit 2
  fi
fi
# 越界即 exit 3。fail-closed：git 状态读不到 / 解析出错（含路径含换行）→ exit 2 不放行。
guard_or_die() {
  local _status_tmp _all nw nonmd ignored rc
  _status_tmp="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }
  _kb_git_status_dump "$_status_tmp" || { log "FATAL（$1）：护栏读不到 git 状态（rc=$?），拒绝放行"; rm -f "$_status_tmp"; exit 2; }
  _all="$(_kb_parse_status_file "$_status_tmp")"; rc=$?
  rm -f "$_status_tmp"
  [ "$rc" -eq 9 ] && exit 2   # FATAL 已由 _kb_parse_status_file 写到 stderr
  [ "$rc" -eq 0 ] || { log "FATAL（$1）：护栏解析 git 状态失败（rc=$rc），拒绝放行"; exit 2; }
  if printf '%s\n' "$_all" | grep -qE '/$'; then
    log "FATAL（$1）：护栏遇到未展开到文件粒度的目录级记录（-uall 理应已展开），拒绝放行"; exit 2
  fi
  # 用 PIPESTATUS[-1] 取 _kb_diff_new 自身的退出码，而非 `$?`——脚本全局 `set -o pipefail`，若管道里
  # 更早的 grep 因"无匹配"正常返回 1（极常见，如全部文件都不越界），pipefail 会让整条管道的退出码变成
  # 那个 1，与 _kb_diff_new 真正的 0/2 混为一谈，把正常空结果误判成护栏比对失败。
  nw="$(printf '%s\n' "$_all" | grep -vE "^${KB_DIR}/" | grep -vxF "$KB_STATE_FILE" | LC_ALL=C sort | _kb_diff_new "$OUTSIDE_BEFORE"; exit "${PIPESTATUS[-1]}")"; rc=$?
  [ "$rc" -ne 0 ] && { log "FATAL（$1）：护栏比对失败（rc=$rc），拒绝放行"; exit 2; }
  if [ -n "$nw" ]; then
    log "GUARD FAILED（$1）：改动了 $KB_DIR 以外的文件，拒绝开 PR（本地请手动清理后重跑）："; echo "$nw" | sed 's/^/  /'; exit 3
  fi
  # knowledge/ 应全是 markdown；agent 若产出非 .md（scratch/临时）会绕过 docs 门(*.md)被静默提交，在此拦下。
  nonmd="$(printf '%s\n' "$_all" | grep -E "^${KB_DIR}/" | grep -vE '\.md$' | LC_ALL=C sort | _kb_diff_new "$NONMD_BEFORE"; exit "${PIPESTATUS[-1]}")"; rc=$?
  [ "$rc" -ne 0 ] && { log "FATAL（$1）：护栏比对失败（rc=$rc），拒绝放行"; exit 2; }
  if [ -n "$nonmd" ]; then
    log "GUARD FAILED（$1）：$KB_DIR 内出现非 .md 文件（疑似 agent scratch），拒绝开 PR："; echo "$nonmd" | sed 's/^/  /'; exit 3
  fi
  # F8：库内命中 .gitignore 的新增文件（如 temp/、*.key），按与非 .md 同级别拦下。
  local _ign_tmp; _ign_tmp="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }
  _kb_git_ignored_dump "$_ign_tmp" || { log "FATAL（$1）：护栏读不到库内 ignore 状态（rc=$?），拒绝放行"; rm -f "$_ign_tmp"; exit 2; }
  local _ign_all; _ign_all="$(_kb_parse_status_file "$_ign_tmp" '!!')"; rc=$?
  rm -f "$_ign_tmp"
  [ "$rc" -eq 9 ] && exit 2
  [ "$rc" -eq 0 ] || { log "FATAL（$1）：护栏解析库内 ignore 状态失败（rc=$rc），拒绝放行"; exit 2; }
  if printf '%s\n' "$_ign_all" | grep -qE '/$'; then
    log "FATAL（$1）：护栏（库内 ignore）遇到未展开到文件粒度的目录级记录，拒绝放行"; exit 2
  fi
  ignored="$(printf '%s\n' "$_ign_all" | LC_ALL=C sort | _kb_diff_new "$IGNORED_BEFORE"; exit "${PIPESTATUS[-1]}")"; rc=$?
  [ "$rc" -ne 0 ] && { log "FATAL（$1）：护栏比对失败（rc=$rc），拒绝放行"; exit 2; }
  if [ -n "$ignored" ]; then
    log "GUARD FAILED（$1）：$KB_DIR 内出现命中 .gitignore 的文件（疑似 scratch/密钥类产出），拒绝开 PR："; echo "$ignored" | sed 's/^/  /'; exit 3
  fi
}

# ── 阶段1 增量同步：固定 3 轮，不管有无变化都跑满，不许收敛提前停 ──────
rounds_run=0
if [ "$_KB_RESUME_PHASE" = "incr" ]; then
  rounds_run="$_KB_RESUME_ROUND"
elif [ -n "$_KB_RESUME_PHASE" ]; then
  # F1：resume 到 full/verify/review/fix/docs 阶段时增量同步循环整段被跳过，rounds_run 不恢复会恒为 0，
  # 导致 PR 描述误写「增量同步 0 轮」。从状态文件的 rounds_run 字段恢复（旧格式缺该字段退化用 round 字段）。
  rounds_run="$(jq -r '.rounds_run // .round // 0' "$KB_STATE_FILE" 2>/dev/null)"
  case "$rounds_run" in ''|*[!0-9]*) rounds_run=0 ;; esac
fi
for i in 1 2 3; do
  if [ -n "$_KB_RESUME_PHASE" ]; then
    if [ "$_KB_RESUME_PHASE" = "incr" ] && [ "$i" -le "$_KB_RESUME_ROUND" ]; then
      log "--resume：跳过已完成的增量同步 round $i"
      continue
    fi
    if [ "$_KB_RESUME_PHASE" != "incr" ]; then
      log "--resume：阶段1 增量同步整体已完成（phase=$_KB_RESUME_PHASE），跳过剩余增量同步轮"
      break
    fi
  fi
  log "===== 阶段1 增量同步 round $i / 3 ====="
  run_incremental "$i"; rounds_run="$i"
  guard_or_die "阶段1 增量同步 round $i"   # 每轮后即查越界，早轮越界当轮拦下
  { echo "## 阶段1·增量同步 Round $i"; echo "$INCR_SUMMARY"; echo; echo '```'
    git diff --stat -- "$KB_DIR" 2>/dev/null
    git -c core.quotepath=false ls-files --others --exclude-standard -- "$KB_DIR" | sed 's/$/  (新增文件)/'
    echo '```'; echo; } >> "$LEDGER"
  _kb_state_save incr "$i"
done
log "阶段1 增量同步共跑 $rounds_run 轮（固定 3 轮，不收敛提前停）"

# ── 阶段2 全量同步（按方法论对照代码现状全量核 knowledge/） ──────────────────
if [ -n "$_KB_RESUME_PHASE" ] && { [ "$_KB_RESUME_PHASE" = "full" ] || [ "$_KB_RESUME_PHASE" = "verify" ] || [ "$_KB_RESUME_PHASE" = "review" ] || [ "$_KB_RESUME_PHASE" = "fix" ] || [ "$_KB_RESUME_PHASE" = "docs" ]; }; then
  log "--resume：阶段2 全量同步已完成，跳过"
else
  log "===== 阶段2 全量同步 ====="
  run_full_sync
  { echo "## 阶段2·全量同步"; echo "$FULL_SUMMARY"; echo; } >> "$LEDGER"
  _kb_state_save full "$rounds_run"
fi
guard_or_die "阶段2 全量同步后"

# ── 阶段3 完整审查 ──────────────────────────────────────
if [ -n "$_KB_RESUME_PHASE" ] && { [ "$_KB_RESUME_PHASE" = "verify" ] || [ "$_KB_RESUME_PHASE" = "review" ] || [ "$_KB_RESUME_PHASE" = "fix" ] || [ "$_KB_RESUME_PHASE" = "docs" ]; }; then
  log "--resume：阶段3 完整审查已完成，跳过"
else
  log "===== 阶段3 完整审查 ====="
  run_verify
  guard_or_die "阶段3 完整审查后"
  { echo "## 阶段3·完整审查"; echo "$VERIFY_SUMMARY"; echo; } >> "$LEDGER"
  _kb_state_save verify "$rounds_run"
fi

# ── 无改动 → 不开空 PR ──────────────────────
if [ -z "$(git status --porcelain -- "$KB_DIR")" ]; then
  _die_if_agent_failed
  log "knowledge/ 与代码已同步，无改动 —— 不开 PR"; exit 0
fi

# ── 阶段4 review（只读）+ 阶段5 解决（有 [ISSUE] 才跑） ──────────────
ISSUE_FILE="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$ISSUE_FILE")
FIX_REPORT="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$FIX_REPORT")

if [ -n "$_KB_RESUME_PHASE" ] && { [ "$_KB_RESUME_PHASE" = "review" ] || [ "$_KB_RESUME_PHASE" = "fix" ] || [ "$_KB_RESUME_PHASE" = "docs" ]; }; then
  log "--resume：阶段4 review 已完成，跳过（注：跨进程续跑不保留原 [ISSUE] 清单，阶段5 视作零 ISSUE）"
  REVIEW_SUMMARY="(--resume 跳过)"; ISSUE_COUNT=0
else
  log "===== 阶段4 review（只读）====="
  run_review_readonly
  guard_or_die "阶段4 review 后"
  { echo "## 阶段4·review"; echo "$REVIEW_SUMMARY"; echo "[ISSUE] 条数：$ISSUE_COUNT"; echo; } >> "$LEDGER"
  _kb_state_save review "$rounds_run"
fi

if [ -n "$_KB_RESUME_PHASE" ] && { [ "$_KB_RESUME_PHASE" = "fix" ] || [ "$_KB_RESUME_PHASE" = "docs" ]; }; then
  log "--resume：阶段5 解决已完成，跳过"
elif [ "$ISSUE_COUNT" -eq 0 ]; then
  log "===== 阶段5 解决：阶段4 零 [ISSUE]，跳过 ====="
  FIX_SUMMARY="(零 ISSUE，跳过)"
  { echo "## 阶段5·解决"; echo "$FIX_SUMMARY"; echo; } >> "$LEDGER"
  _kb_state_save fix "$rounds_run"
else
  log "===== 阶段5 解决（待解决 $ISSUE_COUNT 条）====="
  run_fix "$ISSUE_FILE"
  guard_or_die "阶段5 解决"
  { echo "## 阶段5·解决"; echo "$FIX_SUMMARY"; echo; } >> "$LEDGER"
  _kb_state_save fix "$rounds_run"
fi

# review/fix 可能把改动全回退（判定原改动不该有）→ 无净改动就不开 PR
if [ -z "$(git status --porcelain -- "$KB_DIR")" ]; then
  _die_if_agent_failed
  log "阶段4/5 后无净改动（前面曾改动、被 review/fix 判定回退）—— 不开 PR"; exit 0
fi
# 有净改动但全程无一次成功的 agent 调用 → 疑似崩溃残留，不可信，不开 PR
[ "$_KB_AGENT_OKS" -eq 0 ] && { log "FATAL: 全程无成功 agent 调用却有残留改动 —— 不可信，exit 5"; exit 5; }

# ── docs 格式门：命令/版本/禁用规则沿用 docs.yml，prettier 与 markdownlint 都作硬闸（不过即 exit 4）。────
# 有意只查本次改动的 knowledge/*.md（不查无关旧文件、不查 README）：本门只保证 bot 本次产出格式干净，
# main 上既有的历史问题由 main 自己的 docs.yml 覆盖。markdownlint --fix + prettier --write 自愈后仍 --check
# 失败 = 不可自动修的真问题，硬拦（软化成 WARN 会让合并后 main 的 Docs 变红）。版本 pin 与 docs.yml 对齐。
if [ -n "$_KB_RESUME_PHASE" ] && [ "$_KB_RESUME_PHASE" = "docs" ]; then
  log "--resume：docs 格式门已完成，跳过"
else
  _KB_MD=()
  # m6：git 状态先落盘、再解析（同 _kb_git_status_dump 口径），不再用进程替换套 git——`< <(...)` 的
  # 生产者是子 shell，看不见 git 自身的退出码，git 失败（如仓库损坏）会被 fail-open 静默当成"无改动 md"、
  # docs 格式门被悄悄跳过。
  _KB_MDLIST_TMP="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$_KB_MDLIST_TMP")
  git -c core.quotepath=false diff -z --name-only HEAD -- "$KB_DIR" > "$_KB_MDLIST_TMP" \
    || { log "FATAL: docs 门读取改动 md 列表失败（git diff 失败），拒绝放行"; exit 2; }
  git -c core.quotepath=false ls-files -z --others --exclude-standard -- "$KB_DIR" >> "$_KB_MDLIST_TMP" \
    || { log "FATAL: docs 门读取改动 md 列表失败（git ls-files 失败），拒绝放行"; exit 2; }
  # 存在性过滤 [ -f ]：agent 删整篇 md 时 diff 仍含被删路径，喂给工具会因文件不存在误挂；只留仍存在的 .md。
  while IFS= read -r -d '' _m; do [ -n "$_m" ] && [ -f "$_m" ] && case "$_m" in *.md) _KB_MD+=("$_m") ;; esac; done < "$_KB_MDLIST_TMP"
  if ! command -v npx >/dev/null 2>&1; then
    log "无 npx/node（本地场景）—— 跳过 docs 格式门；CI 由 setup-node 保证 npx 在"
  elif [ "${#_KB_MD[@]}" -eq 0 ]; then
    log "本次无仍存在的改动 md（纯删除型）—— 跳过 docs 门"
  else
    if ! npx --yes prettier@3 --version >/dev/null 2>&1 || ! npx --yes markdownlint-cli@0.41 --version >/dev/null 2>&1; then
      log "docs 门失败：无法获取 prettier@3 / markdownlint-cli@0.41（拉包/网络问题，非格式问题）"; exit 4
    fi
    log "docs 门：对 ${#_KB_MD[@]} 个改动 md 跑 markdownlint --fix → prettier --write → 双 --check"
    # `--` end-of-options 把以 - 开头的文件名当路径。禁用的 MD013/033/041/040/034 是与 prettier 易冲突的规则。
    npx --yes markdownlint-cli@0.41 --fix --disable MD013 MD033 MD041 MD040 MD034 -- "${_KB_MD[@]}" >/dev/null 2>&1 || true
    npx --yes prettier@3 --write -- "${_KB_MD[@]}" >/dev/null 2>&1 || true
    if ! npx --yes prettier@3 --check -- "${_KB_MD[@]}"; then
      log "docs 门失败：prettier --check 不过"; exit 4
    fi
    if ! npx --yes markdownlint-cli@0.41 --disable MD013 MD033 MD041 MD040 MD034 -- "${_KB_MD[@]}"; then
      log "docs 门失败：markdownlint --check 不过（--fix 后仍有不可自动修的项）"; exit 4
    fi
    log "docs 门通过"
  fi
  # F6：同上，传当前 rounds_run 而非硬编码 0。
  _kb_state_save docs "$rounds_run"
fi

# docs 门的 --write/--fix 可能把唯一的改动归一化掉（如仅尾随空白）→ 净改动变空，按已同步退出。
if [ -z "$(git status --porcelain -- "$KB_DIR")" ]; then
  log "docs 门归一化后无净改动 —— 视作已同步，不开 PR"; exit 0
fi
[ "$_KB_AGENT_FAILS" -gt 0 ] && log "WARN: 本轮累计 $_KB_AGENT_FAILS 次 agent 异常但已产出改动 → PR 照开，绿灯≠无异常，PR 描述已标注"

# ── 摘要（进 PR 描述）────────────────────────────────────────
SUMMARY="$(mktemp)" || { log "FATAL: mktemp 失败"; exit 2; }; _KB_TMPS+=("$SUMMARY")
{
  echo "## 知识库自动刷新（待人工确认）"
  echo
  echo "- 基线 HEAD：\`$HEAD_SHA\`；固定六阶段：① 增量同步×3 轮 ② 全量同步×1 轮（按方法论对照代码现状全量核 knowledge/） ③ 完整审查×1 轮 ④ review×1 轮（只读） ⑤ 解决（有 [ISSUE] 才跑，本次 $ISSUE_COUNT 条） ⑥ commit + PR。"
  echo "- 本 PR **只含 \`$KB_DIR/\` 改动**（路径护栏已校验），**需人工审核后再合**。"
  [ "$_KB_AGENT_FAILS" -gt 0 ] && echo "- ⚠️ 本轮有 **$_KB_AGENT_FAILS** 次 agent 调用异常（见 CI 日志），覆盖质量可能打折，请人工重点复核。"
  echo
  echo "### 改动文件"
  {
    git diff --stat -- "$KB_DIR"
    git -c core.quotepath=false ls-files --others --exclude-standard -- "$KB_DIR" | sed 's/$/  (新增文件)/'
  } | sed 's/^/    /'
  # 新增 .md 无法被自动区分"正当新文档"还是 scratch → 提醒人工核。
  if [ -n "$(git -c core.quotepath=false ls-files --others --exclude-standard -- "$KB_DIR")" ]; then
    echo
    echo "> ⚠️ 上面标「新增文件」的请人工确认是正当的新知识库文档、而非 agent 误产出的 scratch。"
  fi
  echo
  echo "### 近 ${COMMIT_WINDOW_DAYS} 天动过代码的 commit（阶段1 覆盖清单）"
  echo "$CHECKLIST_NOTE"
  echo
  echo '```text'
  printf '%s\n' "$CHECKLIST_BLOCK" | sed 's/```/(code-fence)/g'
  echo '```'
  echo
  echo "### 台账（六阶段各自小结）"
  # 台账是 agent 自由文本、长度无上限，可能撑爆 GitHub PR body 65536 上限致 gh 失败 → 截到 32000 字节。
  # ``` 围栏包裹防台账内记号破坏渲染；先把内部 ``` 中和成占位符（唯一能破围栏的东西）。
  echo '```text'
  _led="$(sed 's/```/(code-fence)/g' "$LEDGER")"
  printf '%s' "$_led" | _kb_emit_capped 32000
  [ "$(printf '%s' "$_led" | wc -c)" -gt 32000 ] && printf '\n…（台账过长已截断，完整见 CI job log）\n'
  echo; echo '```'
} > "$SUMMARY"
log "摘要已生成：$SUMMARY"

# ── 开 PR（仅 CI：gh + GH_TOKEN 就绪）──────────────
# CI 且要求开 PR（OPEN_PR!=no）时缺 gh/GH_TOKEN 一律 fail-loud——不能静默绿灯退出、把本轮产出全丢；
# 只有本地（GITHUB_ACTIONS 空）或显式 OPEN_PR=no 才走下面 exit 0 的 inspect 路径。
if [ -n "${GITHUB_ACTIONS:-}" ] && [ "$OPEN_PR" != "no" ]; then
  if ! command -v gh >/dev/null 2>&1 || [ -z "${GH_TOKEN:-}" ]; then
    log "FATAL: CI 且要求开 PR（OPEN_PR!=no）但缺 gh/GH_TOKEN，拒绝静默丢弃本轮产出，exit 2"
    exit 2
  fi
fi
if [ "$OPEN_PR" = "no" ] || [ -z "${GITHUB_ACTIONS:-}" ]; then
  log "非 CI / OPEN_PR=no —— 跳过开 PR，改动保留在工作区供 inspect。"
  log "改动概览："; git diff --stat -- "$KB_DIR" | sed 's/^/  /'
  exit 0
fi

BR="auto/kb-refresh-$(date +%Y%m%d-%H%M%S)"
log "开 PR：分支 $BR"
git switch -c "$BR"           || { log "FATAL: git switch -c 失败"; exit 7; }
git add -- "$KB_DIR"          || { log "FATAL: git add 失败"; exit 7; }
git -c user.name="kb-refresh-bot" -c user.email="kb-refresh-bot@users.noreply.github.com" \
  commit -m "docs(knowledge): 每周自动同步知识库与代码（待人工确认）" >/dev/null \
                               || { log "FATAL: git commit 失败"; exit 7; }
git push -u origin "$BR"      || { log "FATAL: git push 失败"; exit 7; }
# 先不带标签建 PR，再单独打标签——避免"PR 已建成、只是打标签失败→非零→退回重试报 PR already exists"。
pr_url="$(gh pr create --repo "$REPO" --base main --head "$BR" \
  --title "docs(knowledge): 每周知识库自动刷新（待人工确认）" --body-file "$SUMMARY")" \
  || { log "FATAL: gh pr create 失败，清理孤儿分支 $BR"; git push origin --delete "$BR" 2>/dev/null || log "WARN: 清理孤儿分支 $BR 失败，请人工核实远端分支"; exit 7; }
gh pr edit "$pr_url" --repo "$REPO" --add-label "kb-auto-refresh" >/dev/null 2>&1 \
  || log "WARN: 打标签 kb-auto-refresh 失败（PR 已建成，去重靠分支前缀兜底）"
log "PR 已创建，等待人工审核合并：$pr_url"
exit 0
