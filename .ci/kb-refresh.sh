#!/usr/bin/env bash
# 知识库每周刷新。流程：拉最新 main → 算近月 commit 覆盖清单 → N 轮全量复审（每轮一个全新独立
# agent、按 knowledge/README.md 规范就地矫正、连续 CONVERGE_AFTER 轮无新增即停）→ commit 覆盖审计
# → 出 PR 前 REVIEW_ROUNDS 轮 review 修确有必要的问题 → 路径护栏（只允许改 knowledge/）→ docs 格式门
# → 开一个待人工确认的 PR。人是唯一合并闸门：本脚本只开 PR、从不自动合并。
#
# 退出码：0=成功/无需改；2=前置/装配错误；3=护栏拦截（改了 knowledge/ 以外或库内产出非 .md）；
#         4=docs 格式门失败；5=agent 系统性失败（"无改动"不可信）；6=无法查询现有 PR（防重复开 PR）；
#         7=开 PR 阶段 git/gh 失败。
set -uo pipefail

# ── 可配置（env）。workflow_dispatch 只暴露 MAX_ROUNDS 一个输入，其余是内部参数（仍可经 env 覆盖）。──
KB_DIR="${KB_DIR:-knowledge}"
MAX_ROUNDS="${MAX_ROUNDS:-8}"                   # 复审轮数上限（兜底；正常靠收敛提前停）
CONVERGE_AFTER="${CONVERGE_AFTER:-3}"           # 连续 N 轮 knowledge/ 无新增即收敛停止
REVIEW_ROUNDS="${REVIEW_ROUNDS:-3}"             # 出 PR 前的 review 轮数
COMMIT_WINDOW_DAYS="${COMMIT_WINDOW_DAYS:-30}"  # 覆盖审计回看的 commit 窗口
CHECKLIST_MAX="${CHECKLIST_MAX:-100}"           # 覆盖清单最多取最近 N 条 commit（超出截断、如实标注）
REPO="${REPO:-XiaoMi/xiaomi-miloco}"
CMD_SRC="${CMD_SRC:-.agents/commands/kb-refresh.md}"
OPEN_PR="${OPEN_PR:-auto}"                      # auto|no
KB_AGENT_CMD="${KB_AGENT_CMD:-}"               # 测试 stub：注入后不调真 claude，零成本验证编排

log() { echo "[kb-refresh] $*"; }

# ── 退出清理：删临时文件、还原本地被换掉的 .claude/（CI 是一次性容器、这些都是空操作）。────
# 只处理正常退出与脚本内 exit；被信号硬杀时不保证清理（本地残留可下次重跑覆盖，无害）。
_KB_SET_STATE=""; _KB_SET_BAK=""   # settings.json：keep=原有(还原)/drop=原无(删除)
_KB_CMD_STATE=""; _KB_CMD_BAK=""   # commands/kb-refresh.md 同上
_KB_TMPS=()
_KB_AGENT_FAILS=0; _KB_AGENT_OKS=0
_kb_cleanup() {
  [ "$_KB_SET_STATE" = keep ] && cp "$_KB_SET_BAK" .claude/settings.json 2>/dev/null
  [ "$_KB_SET_STATE" = drop ] && rm -f .claude/settings.json
  [ "$_KB_CMD_STATE" = keep ] && cp "$_KB_CMD_BAK" .claude/commands/kb-refresh.md 2>/dev/null
  [ "$_KB_CMD_STATE" = drop ] && rm -f .claude/commands/kb-refresh.md
  rm -f "$_KB_SET_BAK" "$_KB_CMD_BAK" 2>/dev/null || true
  [ "$_KB_CMD_STATE" = drop ] && rmdir .claude/commands .claude 2>/dev/null || true
  [ "${#_KB_TMPS[@]}" -gt 0 ] && rm -f "${_KB_TMPS[@]}" 2>/dev/null || true
}
trap _kb_cleanup EXIT

# ── 前置 ────────────────────────────────────────────────────
command -v git >/dev/null 2>&1 || { log "FATAL: 无 git"; exit 2; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { log "FATAL: 不在 git 工作区"; exit 2; }
[ -d "$KB_DIR" ] || { log "FATAL: 无 $KB_DIR 目录"; exit 2; }
# MAX_ROUNDS 是唯一的手动输入，校验它：须正整数（无前导零，否则 [ -ge ] 当八进制报错）、且 >= CONVERGE_AFTER
# （否则"连续 CONVERGE_AFTER 轮无新增"永远凑不满、收敛判据静默失效）。
case "$MAX_ROUNDS" in ''|*[!0-9]*|0*) log "FATAL: MAX_ROUNDS 须为正整数（无前导零），实为 '$MAX_ROUNDS'"; exit 2 ;; esac
[ "$MAX_ROUNDS" -ge "$CONVERGE_AFTER" ] || { log "FATAL: MAX_ROUNDS($MAX_ROUNDS) 必须 >= CONVERGE_AFTER($CONVERGE_AFTER)"; exit 2; }
if [ -z "${ANTHROPIC_BASE_URL:-}" ]; then unset ANTHROPIC_BASE_URL 2>/dev/null || true; fi
# 直调 claude 须把 PR_AGENT_MODEL 映射到 claude 认的 ANTHROPIC_MODEL*
if [ -n "${PR_AGENT_MODEL:-}" ]; then
  export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-$PR_AGENT_MODEL}"
  export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-$PR_AGENT_MODEL}"
  export ANTHROPIC_DEFAULT_OPUS_MODEL="${ANTHROPIC_DEFAULT_OPUS_MODEL:-$PR_AGENT_MODEL}"
  export ANTHROPIC_DEFAULT_SONNET_MODEL="${ANTHROPIC_DEFAULT_SONNET_MODEL:-$PR_AGENT_MODEL}"
  export ANTHROPIC_DEFAULT_HAIKU_MODEL="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-$PR_AGENT_MODEL}"
elif [ -z "$KB_AGENT_CMD" ]; then
  log "WARN: 未配置 PR_AGENT_MODEL —— 将用 claude 内置默认模型，建议在仓库 vars 配置"
fi

HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
log "baseline HEAD=$HEAD_SHA  KB_DIR=$KB_DIR  MAX_ROUNDS=$MAX_ROUNDS  CONVERGE_AFTER=$CONVERGE_AFTER  REVIEW_ROUNDS=$REVIEW_ROUNDS"

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

# ── commit 覆盖清单：近 N 天、动过代码目录的 commit（喂每轮 + 覆盖审计用）──────
# 超 CHECKLIST_MAX 条只取最近 N 条（防审计 prompt 过大），但如实标注真实总数、不拦截；被截掉的更早
# commit 由每轮双向逐文件复审兜底。pathspec 显式列举代码顶层目录（新增顶层目录时同步）。
_CL_ALL="$(git log --since="${COMMIT_WINDOW_DAYS} days ago" --no-merges \
  --pretty='- %h %s' -- backend web scripts cli plugins 2>/dev/null)"
CHECKLIST_TOTAL="$(printf '%s\n' "$_CL_ALL" | grep -c '^- ' || true)"
case "$CHECKLIST_TOTAL" in ''|*[!0-9]*) CHECKLIST_TOTAL=0 ;; esac
CHECKLIST_BLOCK="$(printf '%s\n' "$_CL_ALL" | head -n "$CHECKLIST_MAX")"
if [ "$CHECKLIST_TOTAL" -eq 0 ]; then
  CHECKLIST_BLOCK="(近 ${COMMIT_WINDOW_DAYS} 天无动过代码的 commit)"
  CHECKLIST_NOTE="近 ${COMMIT_WINDOW_DAYS} 天无动过代码的 commit"
elif [ "$CHECKLIST_TOTAL" -gt "$CHECKLIST_MAX" ]; then
  CHECKLIST_NOTE="⚠️ 近 ${COMMIT_WINDOW_DAYS} 天共 ${CHECKLIST_TOTAL} 条 commit，覆盖审计只取最近 ${CHECKLIST_MAX} 条（其余由双向逐文件复审兜底）；不拦截，请人工留意"
  log "WARN: $CHECKLIST_NOTE"
else
  CHECKLIST_NOTE="近 ${COMMIT_WINDOW_DAYS} 天共 ${CHECKLIST_TOTAL} 条 commit，已全部纳入覆盖审计"
fi
log "commit 覆盖清单：$CHECKLIST_NOTE"

# ── 台账（脚本维护，仅用于 PR 描述；agent 不碰）──────
if [ -n "${KB_LEDGER:-}" ]; then LEDGER="$KB_LEDGER"; else LEDGER="$(mktemp)"; _KB_TMPS+=("$LEDGER"); fi
printf '# kb-refresh 台账（HEAD=%s）\n\n' "$HEAD_SHA" > "$LEDGER"
ROUND_SUMMARY=""; AUDIT_SUMMARY=""; REVIEW_SUMMARY=""

# 写 agent 配置：① 只读 Bash 白名单 settings.json（无它 agent 每条命令都弹权限确认、无人值守会卡死）；
# ② /kb-refresh 命令文件。每次调用前重写（见 _run_claude），防上一轮 agent 用 Write 篡改后带入下一轮。
# 写范围的最终硬保证不是白名单（前缀匹配挡不住尾随重定向），而是事后路径护栏。
_kb_write_agent_cfg() {
  mkdir -p .claude/commands || { log "FATAL: 无法创建 .claude/commands"; exit 2; }
  cat > .claude/settings.json <<'SETEOF'
{"permissions": {"allow": ["Edit", "Write", "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(git show*)", "Bash(git rev-parse*)", "Bash(rg *)", "Bash(grep *)", "Bash(ls)", "Bash(ls *)", "Bash(jq *)"]}}
SETEOF
  [ -s .claude/settings.json ] || { log "FATAL: 写入 .claude/settings.json 失败"; exit 2; }
  cp "$CMD_SRC" .claude/commands/kb-refresh.md || { log "FATAL: 写入 .claude/commands/kb-refresh.md 失败"; exit 2; }
  [ -s .claude/commands/kb-refresh.md ] || { log "FATAL: .claude/commands/kb-refresh.md 为空"; exit 2; }
}

# ── 装命令（本地会临时把开发者的 .claude/settings.json + commands/kb-refresh.md 换成 bot 版、退出还原；
#    别在同一仓库一边跑本机 bot 一边交互用 Claude Code。CI 无此顾虑）────
if [ -z "$KB_AGENT_CMD" ]; then
  [ -f "$CMD_SRC" ] || { log "FATAL: 方法论 $CMD_SRC 缺失"; exit 2; }
  command -v claude >/dev/null 2>&1 || { log "FATAL: 无 claude CLI 且未注入 KB_AGENT_CMD"; exit 2; }
  mkdir -p .claude/commands || { log "FATAL: 无法创建 .claude/commands"; exit 2; }
  # 备份开发者原文件（原有→退出还原；原无→退出删除）。先 cp 成功再置 keep；备份失败即中止，绝不在没备份成功时覆盖。
  if [ -f .claude/settings.json ]; then _KB_SET_BAK="$(mktemp)"; cp .claude/settings.json "$_KB_SET_BAK" || { log "FATAL: 备份 .claude/settings.json 失败"; exit 2; }; _KB_SET_STATE=keep; else _KB_SET_STATE=drop; fi
  if [ -f .claude/commands/kb-refresh.md ]; then _KB_CMD_BAK="$(mktemp)"; cp .claude/commands/kb-refresh.md "$_KB_CMD_BAK" || { log "FATAL: 备份 .claude/commands/kb-refresh.md 失败"; exit 2; }; _KB_CMD_STATE=keep; else _KB_CMD_STATE=drop; fi
  _kb_write_agent_cfg   # 早失败校验（配置能写成功再往下跑）
fi

# ── 统一的 claude 单次调用：$1=prompt, $2=输出文件。────
_run_claude() {
  local prompt="$1" out="$2"
  [ -z "$KB_AGENT_CMD" ] && _kb_write_agent_cfg   # 每次调用前重置白名单 + 命令文件，防上一轮篡改
  stdbuf -oL claude \
    --permission-mode acceptEdits \
    --tools "Bash,Read,Glob,Grep,Edit,Write" \
    --verbose --output-format stream-json -p "$prompt" < /dev/null \
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
        elif .type=="result" then "[DONE] subtype="+((.subtype//"?")|tostring)+" is_error="+((.is_error//false)|tostring)+" cost=$"+((.total_cost_usd//0)|tostring)+", turns="+((.num_turns//0)|tostring)
        else empty end' | tee "$out"
  local rc="${PIPESTATUS[0]}"
  # 防假绿：agent 没跑起来/异常收尾时，零编辑与"本就同步"难分。只有"rc=0 + 有 [DONE] + 非 is_error"算成功。
  if [ "$rc" -ne 0 ] || ! grep -q '^\[DONE\]' "$out" || grep -q '^\[DONE\].*is_error=true' "$out"; then
    log "WARN: agent 可能异常（rc=$rc，未见 [DONE] 或 [DONE] 标 is_error=true）；勿据此误判为已同步。"
    _KB_AGENT_FAILS=$((_KB_AGENT_FAILS + 1))
  else
    _KB_AGENT_OKS=$((_KB_AGENT_OKS + 1))
  fi
}
# 无改动退出前调用：全程无一次成功的 agent 调用时，"无改动"不可信（系统性失败）→ exit 5，防静默停摆。
_die_if_agent_failed() {
  [ -n "$KB_AGENT_CMD" ] && return 0   # stub 模式跳过
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

# ── 单轮全量复审 agent（一个全新独立 agent）──────────────────
run_agent() {
  local round="$1"
  ROUND_SUMMARY=""
  if [ -n "$KB_AGENT_CMD" ]; then
    KB_ROUND="$round" KB_DIR="$KB_DIR" bash -c "$KB_AGENT_CMD" _kb "$round"
    ROUND_SUMMARY="(stub agent)"; return 0
  fi
  local out; out="$(mktemp)"; _KB_TMPS+=("$out")
  local prompt
  prompt="$(printf '/kb-refresh --round %s --max %s --kb %s --ci\n\n参考·commit 清单（%s）——逐条确认其 L1/L2 变更是否已在知识库反映（纯 L3 无需入库）。清单若为"最近 N 条"截断，更早的靠你对 knowledge/ 与代码的双向全量通读兜底，别只依赖此清单：\n%s' \
    "$round" "$MAX_ROUNDS" "$KB_DIR" "$CHECKLIST_NOTE" "$CHECKLIST_BLOCK")"
  _run_claude "$prompt" "$out"
  ROUND_SUMMARY="$(_tail_summary "$out")"; [ -n "$ROUND_SUMMARY" ] || ROUND_SUMMARY="(本轮无文字小结)"
  rm -f "$out"
}

# ── commit 覆盖审计（收敛后，逐条核清单是否已反映）──────────────
run_audit() {
  AUDIT_SUMMARY=""
  local out; out="$(mktemp)"; _KB_TMPS+=("$out")
  local prompt
  prompt="$(printf '/kb-refresh --kb %s --ci\n\n【commit 覆盖审计】前几轮已把知识库与代码对齐。下面是待核对的 commit 清单（%s）——**逐条**核对每个 commit 的 L1/L2 变更是否已在知识库如实反映：已覆盖→跳过；遗漏→按 knowledge/README.md 规范补（指路、最小 diff、不写 L3）；过期/错误→改对。**一个 commit 可能含多处 L1/L2，逐一核每处；新增的对外端点(/admin/*)与用户可见能力属 L1/L2、要指路补上。** 清单若为截断，更早的由每轮双向逐文件复审兜底。末尾逐条给 covered / 补充<文件> / 跳过(L3) 结论。清单：\n%s' \
    "$KB_DIR" "$CHECKLIST_NOTE" "$CHECKLIST_BLOCK")"
  _run_claude "$prompt" "$out"
  AUDIT_SUMMARY="$(_tail_summary "$out")"; [ -n "$AUDIT_SUMMARY" ] || AUDIT_SUMMARY="(审计无文字小结)"
  rm -f "$out"
}

# ── 出 PR 前的 review 轮（审本次 knowledge 改动 + 就地修确有必要的问题，避免过度改写）──────
# review-pr 官方命令只读且需 PR#，无法在建 PR 前跑并改；这里用自包含 review prompt 等效审+修。
# 有意不载 /kb-refresh 方法论（那是"主动补写"角色，与本阶段"最小修正、别扩写"相反）。
run_review() {
  local r="$1"
  REVIEW_SUMMARY=""
  local out; out="$(mktemp)"; _KB_TMPS+=("$out")
  local prompt
  prompt="$(printf '你是严格的 PR reviewer（第 %s/%s 轮，独立）。先跑 `git diff -- %s` 看本次对知识库的改动，按下述维度审查、【就地修复确有必要的问题】（只改 %s 下文件；**避免为凑改动而过度重写本已正确的内容**）：\n- 正确性：每条新表述能否被代码证实（防幻觉式错改，用 Grep/Read 回代码核）；\n- 规范：符合 knowledge/README.md 三档（L1/L2 留、L3 不写、指路不复制、最小 diff）；\n- 质量：表述准确、无冗余、无自相矛盾、内链有效。\n本轮无真问题则不改（不改是完全正确的结果）。探索优先 Grep/Read/Glob（简单只读 git 可用）、不猜路径、不用 MCP；不 commit/push、不碰 .claude/.git。末尾两三句小结：本轮改了什么/确认了什么。' \
    "$r" "$REVIEW_ROUNDS" "$KB_DIR" "$KB_DIR")"
  _run_claude "$prompt" "$out"
  REVIEW_SUMMARY="$(_tail_summary "$out")"; [ -n "$REVIEW_SUMMARY" ] || REVIEW_SUMMARY="(review 无文字小结)"
  rm -f "$out"
}

# ── knowledge/ 内容签名（判收敛，按内容而非文件名）──────────────
kb_sig() {
  {
    git -c core.quotepath=false diff HEAD -- "$KB_DIR"
    git -c core.quotepath=false ls-files --others --exclude-standard -- "$KB_DIR" \
      | LC_ALL=C sort | while IFS= read -r f; do printf '== %s ==\n' "$f"; cat -- "$f"; done
  } | md5sum | awk '{print $1}'
}

# ── 护栏基线：只追究本次**新引入**的越界/库内非 md 改动（扣除运行前的既有脏文件；CI 全新 checkout 为空）──
OUTSIDE_BEFORE="$(mktemp)"; _KB_TMPS+=("$OUTSIDE_BEFORE")
git -c core.quotepath=false status --porcelain | sed 's/^...//' | grep -vE "^${KB_DIR}/" | LC_ALL=C sort > "$OUTSIDE_BEFORE" || true
NONMD_BEFORE="$(mktemp)"; _KB_TMPS+=("$NONMD_BEFORE")
git -c core.quotepath=false status --porcelain | sed 's/^...//' | grep -E "^${KB_DIR}/" | grep -vE '\.md$' | LC_ALL=C sort > "$NONMD_BEFORE" || true
# 越界即 exit 3。fail-closed：git 状态读不到（环境错）→ exit 2 不放行。末尾 `|| true` 只吞 grep 无匹配的正常退出码 1。
guard_or_die() {
  local _st nw nonmd
  _st="$(git -c core.quotepath=false status --porcelain)" || { log "FATAL（$1）：护栏读不到 git 状态，拒绝放行"; exit 2; }
  nw="$(printf '%s\n' "$_st" | sed 's/^...//' | grep -vE "^${KB_DIR}/" | LC_ALL=C sort | grep -vxFf "$OUTSIDE_BEFORE" || true)"
  if [ -n "$nw" ]; then
    log "GUARD FAILED（$1）：改动了 $KB_DIR 以外的文件，拒绝开 PR（本地请手动清理后重跑）："; echo "$nw" | sed 's/^/  /'; exit 3
  fi
  # knowledge/ 应全是 markdown；agent 若产出非 .md（scratch/临时）会绕过 docs 门(*.md)被静默提交，在此拦下。
  nonmd="$(printf '%s\n' "$_st" | sed 's/^...//' | grep -E "^${KB_DIR}/" | grep -vE '\.md$' | LC_ALL=C sort | grep -vxFf "$NONMD_BEFORE" || true)"
  if [ -n "$nonmd" ]; then
    log "GUARD FAILED（$1）：$KB_DIR 内出现非 .md 文件（疑似 agent scratch），拒绝开 PR："; echo "$nonmd" | sed 's/^/  /'; exit 3
  fi
}

# ── N 轮全量复审，连续 CONVERGE_AFTER 轮无新增即停 ──────────
prev_sig="$(kb_sig)"; stable=0; rounds_run=0
for i in $(seq 1 "$MAX_ROUNDS"); do
  log "===== 复审 round $i / $MAX_ROUNDS ====="
  run_agent "$i"; rounds_run="$i"
  guard_or_die "复审 round $i"   # 每轮后即查越界，早轮越界当轮拦下
  { echo "## 复审 Round $i"; echo "$ROUND_SUMMARY"; echo; echo '```'
    git diff --stat -- "$KB_DIR" 2>/dev/null
    git -c core.quotepath=false ls-files --others --exclude-standard -- "$KB_DIR" | sed 's/$/  (新增文件)/'
    echo '```'; echo; } >> "$LEDGER"
  cur_sig="$(kb_sig)"
  if [ "$cur_sig" = "$prev_sig" ]; then
    stable=$((stable + 1)); log "round $i: 无新增（连续 stable=$stable）"
    [ "$stable" -ge "$CONVERGE_AFTER" ] && { log "已收敛（连续 $CONVERGE_AFTER 轮无新增），提前结束"; break; }
  else stable=0; log "round $i: 有新增变化"; fi
  prev_sig="$cur_sig"
done
log "复审共跑 $rounds_run 轮"

# ── commit 覆盖审计 ───────────────────────────────────────
if [ -z "$KB_AGENT_CMD" ]; then
  log "===== commit 覆盖审计 ====="
  run_audit
  { echo "## commit 覆盖审计"; echo "$AUDIT_SUMMARY"; echo; } >> "$LEDGER"
fi
guard_or_die "复审/审计后"

# ── 无改动 → 不开空 PR ──────────────────────
if [ -z "$(git status --porcelain -- "$KB_DIR")" ]; then
  _die_if_agent_failed
  log "knowledge/ 与代码已同步，无改动 —— 不开 PR"; exit 0
fi

# ── 出 PR 前跑 REVIEW_ROUNDS 轮 review ──────
if [ -z "$KB_AGENT_CMD" ]; then
  for r in $(seq 1 "$REVIEW_ROUNDS"); do
    log "===== 出 PR 前 review round $r / $REVIEW_ROUNDS ====="
    run_review "$r"
    guard_or_die "review round $r"
    { echo "## Review Round $r"; echo "$REVIEW_SUMMARY"; echo; } >> "$LEDGER"
  done
  # review 可能把改动全回退（判定原改动不该有）→ 无净改动就不开 PR
  if [ -z "$(git status --porcelain -- "$KB_DIR")" ]; then
    _die_if_agent_failed
    log "review 后无净改动（复审曾改动、被 review 判定回退）—— 不开 PR"; exit 0
  fi
  # 有净改动但全程无一次成功的 agent 调用 → 疑似崩溃残留，不可信，不开 PR
  [ "$_KB_AGENT_OKS" -eq 0 ] && { log "FATAL: 全程无成功 agent 调用却有残留改动 —— 不可信，exit 5"; exit 5; }
fi

# ── docs 格式门：命令/版本/禁用规则沿用 docs.yml，prettier 与 markdownlint 都作硬闸（不过即 exit 4）。────
# 有意只查本次改动的 knowledge/*.md（不查无关旧文件、不查 README）：本门只保证 bot 本次产出格式干净，
# main 上既有的历史问题由 main 自己的 docs.yml 覆盖。markdownlint --fix + prettier --write 自愈后仍 --check
# 失败 = 不可自动修的真问题，硬拦（软化成 WARN 会让合并后 main 的 Docs 变红）。版本 pin 与 docs.yml 对齐。
_KB_MD=()
# 存在性过滤 [ -f ]：agent 删整篇 md 时 diff 仍含被删路径，喂给工具会因文件不存在误挂；只留仍存在的 .md。
while IFS= read -r -d '' _m; do [ -n "$_m" ] && [ -f "$_m" ] && case "$_m" in *.md) _KB_MD+=("$_m") ;; esac; done < <(
  { git -c core.quotepath=false diff -z --name-only HEAD -- "$KB_DIR"
    git -c core.quotepath=false ls-files -z --others --exclude-standard -- "$KB_DIR"; })
if ! command -v npx >/dev/null 2>&1; then
  log "无 npx/node（本地场景）—— 跳过 docs 格式门；CI 由 setup-node 保证 npx 在"
elif [ "${#_KB_MD[@]}" -eq 0 ]; then
  log "本次无仍存在的改动 md（纯删除型）—— 跳过 docs 门"
else
  # 预检兼预热 npx 缓存：确认两个包能拉到，否则后面 --check 失败会被误报成格式不过。
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

# docs 门的 --write/--fix 可能把唯一的改动归一化掉（如仅尾随空白）→ 净改动变空，按已同步退出。
if [ -z "$(git status --porcelain -- "$KB_DIR")" ]; then
  log "docs 门归一化后无净改动 —— 视作已同步，不开 PR"; exit 0
fi
[ "$_KB_AGENT_FAILS" -gt 0 ] && log "WARN: 本轮累计 $_KB_AGENT_FAILS 次 agent 异常但已产出改动 → PR 照开，绿灯≠无异常，PR 描述已标注"

# ── 摘要（进 PR 描述）────────────────────────────────────────
SUMMARY="$(mktemp)"; _KB_TMPS+=("$SUMMARY")
{
  echo "## 知识库自动刷新（待人工确认）"
  echo
  echo "- 基线 HEAD：\`$HEAD_SHA\`；复审 $rounds_run 轮（每轮独立 agent，连续 $CONVERGE_AFTER 轮无新增即停）+ commit 覆盖审计 + 出 PR 前 $REVIEW_ROUNDS 轮 review。"
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
  echo "### 近 ${COMMIT_WINDOW_DAYS} 天动过代码的 commit（覆盖清单）"
  echo "$CHECKLIST_NOTE"
  echo
  echo '```text'
  printf '%s\n' "$CHECKLIST_BLOCK" | sed 's/```/(code-fence)/g'
  echo '```'
  echo
  echo "### 台账（复审各轮 + 覆盖审计 + review 小结）"
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
if [ "$OPEN_PR" = "no" ] || [ -z "${GITHUB_ACTIONS:-}" ] || ! command -v gh >/dev/null 2>&1 || [ -z "${GH_TOKEN:-}" ]; then
  log "非 CI / 无 gh/GH_TOKEN / OPEN_PR=no —— 跳过开 PR，改动保留在工作区供 inspect。"
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
  || { log "FATAL: gh pr create 失败"; exit 7; }
gh pr edit "$pr_url" --repo "$REPO" --add-label "kb-auto-refresh" >/dev/null 2>&1 \
  || log "WARN: 打标签 kb-auto-refresh 失败（PR 已建成，去重靠分支前缀兜底）"
log "PR 已创建，等待人工审核合并：$pr_url"
exit 0
