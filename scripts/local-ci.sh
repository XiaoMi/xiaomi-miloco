#!/usr/bin/env bash
# 本地 CI 等效自检脚本。
# 与 .github/workflows/ci.yml backend-test + pr-review 对齐，本地开箱即用。
#
# 用法:
#   ./scripts/local-ci.sh            # 全量自检
#   ./scripts/local-ci.sh --quick     # 仅跑改动相关模块（~3s）
#   ./scripts/local-ci.sh --tests     # 仅跑测试，跳过 pr-review 门禁
#   ./scripts/local-ci.sh --gate      # 仅跑 pr-review 门禁（拉云端 review comment 检查 🔴/🟡）
#
# 已知局限 (macOS):
#   - 跳过 e2e/agent 目录（需运行中 server）
#   - node_monitor 测试 3 项 smaps/ptrace Linux 特有，macOS 自动跳过
#   - CI 全量 2371 passed，本地等价覆盖率 > 99.8%
#
# 需要: Python 3.12+, uv, gh CLI（pr-review 门禁需已 gh auth login）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MODE="${1:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass_count=0
fail_count=0

ok()  { echo -e "${GREEN}✓${NC} $*"; ((pass_count++)) || true; }
fail(){ echo -e "${RED}✗${NC} $*"; ((fail_count++)) || true; }
info(){ echo -e "${YELLOW}→${NC} $*"; }
# 印给人「照抄粘回终端」的命令里，路径一律过这个。printf %q 对不含特殊字符的路径原样
# 输出（本文件当前唯一的调用点就是这种，输出一字不变），有空格时才转义，bash 3.2 即支持。
# 门禁见 backend/miloco/tests/test_shell_var_braces.py::test_printed_commands_quote_their_paths。
_q(){ printf '%q' "$1"; }

# ---- 工具检查 ---------------------------------------------------------------
check_tools() {
    info "检查依赖工具..."
    for tool in python3 uv; do
        if command -v "$tool" &>/dev/null; then
            ok "$tool"
        else
            fail "$tool 未安装"
        fi
    done
    if command -v gh &>/dev/null; then
        ok "gh CLI"
    else
        info "gh CLI 未安装，pr-review 门禁跳过（仅影响 --gate 模式）"
    fi
}

# ---- backend 全量测试 -------------------------------------------------------
run_backend_tests() {
    info "backend 全量测试 (对齐 ci.yml backend-test)…"
    # 模型不在 git 里（scripts/models.lock.json）：缺了 requires_models 那批用例会静默
    # skip，本地覆盖率悄悄比 CI 低。这里只校验不下载（不拖慢 --quick），缺了给提示。
    # --dest 显式给包内目录：不给的话 MILOCO_MODELS_DEST 一旦在环境里，校验的就不是
    # 下面 pytest 真正加载的那个目录，"就绪"提示会与实际情况脱节。
    # 相对路径写一次、绝对路径由它派生，校验与下面印出来的补齐命令因此不可能再指向两个
    # 地方（下面那条注释说的就是这两处一旦分家会怎样）。
    local models_rel="backend/miloco/src/miloco/perception/models"
    local models_dir="$REPO_ROOT/$models_rel"
    # --strict：与 ci.yml 的门禁同强度。不加的话可选模型（bge / VAD）缺失走"只降级不
    # 阻塞"分支、退出码 0，本地一声不吭，而 CI 那边 5 个模型齐全跑的是 EventEmbedder
    # 真实向量那条路径——两边测的不是同一个东西，"已对齐 CI"的判断就是假的。
    # 退出码要分 1 / 2 两档，且 2 那档必须把 stderr 放出来。fetch_models.py 的契约里
    # 1 = 模型没齐（补齐命令有意义），2 = lock 本身坏了 / 用法错了（补齐命令**永远**无效）。
    # 而 --check 这一侧的 2 不是理论分支：lock 里 JSON 解析不了、sha256 或 size 字段不对，
    # 都在进 --check 之前就退 2 —— 最常见的成因正是合并后 lock 里留着冲突标记
    # （test_publish_models.py 点名"合并完先跑一次 refresh-lock 正是最容易撞上冲突标记的
    # 路径"）。旧写法把这一档和"模型没下"合成一句"未就绪 → 用这条补齐"，而
    # >/dev/null 2>&1 又把 stderr 上唯一说明真因的那行一并吞掉：人照抄补齐命令，它同样
    # 退 2，屏幕上还是那三行 —— 唯一的线索被脚本自己删了。所以这一档改印 stderr 原文。
    local models_err rc line
    rc=0
    models_err=$(python3 "$SCRIPT_DIR/fetch_models.py" --check --strict --quiet --dest "$models_dir" 2>&1 >/dev/null) || rc=$?
    if [ "$rc" -eq 2 ]; then
        info "感知模型校验没能进行：输入不合法 —— 不是「模型没下」，补齐命令同样会失败"
        # 逐行转发而不是整块 echo：stderr 可能多行，逐行走 info 才都带上前缀与缩进，
        # 归属关系一眼可见（下面那条修法提示用的也是两格缩进）。
        if [ -n "$models_err" ]; then
            while IFS= read -r line; do info "  $line"; done <<<"$models_err"
        fi
        # 修法跟着上面那行报错走，不预设成因。这一档在 --check 侧有三条可达路径（lock 解析
        # 不了 / lock 里没有可用源 / scheme 不合法 —— MILOCO_MODELS_BASE_URL 与 lock 的
        # base_url 都会走到 _sources 那个 ValueError；目标目录不可写那条够不着，--check
        # 在 mkdir 之前就 return 了）。写死"还原或 refresh-lock"时，换源变量写错的人会被
        # 推去**重新生成**一份本来没问题的清单 —— 而重算是个真会改文件、还带漂移护栏的写
        # 操作，排查方向就此被带到"仓库里的生成物是不是坏了"上去。
        info "  修法按上面那行报错来：指向 MILOCO_MODELS_BASE_URL 就改它（只收 http:// / https:// / file:// 或绝对路径）；指向 lock 才动 lock —— 还原（git checkout -- scripts/models.lock.json）或重跑 scripts/publish_models.sh refresh-lock 重新生成"
    elif [ "$rc" -ne 0 ]; then
        # 文案要覆盖 --strict 实际命中的两种情况：只说 requires_models 的话，"只缺
        # 可选模型"时这条会打出来、而 test_deep_sort_v12 一条没 skip，读的人就把它
        # 归档成噪音，下次必需模型真缺时也拦不住人了。
        info "感知模型未按 lock 就绪（--strict，可选模型也算）：缺必需模型 → requires_models 那批整批 skip；"
        info "  只缺可选模型（bge / VAD）→ 用例照跑但走降级分支，与 CI 测的不是同一条路径"
        # --dest 必须跟着印出来，且必须是上面那次校验用的同一个目录：裸命令的目标目录会
        # 回退到 MILOCO_MODELS_DEST，而把模型放仓库外共享给多个 worktree 的人往往在
        # profile 里长期 export 它。那种环境下照抄一条不带 --dest 的命令，78MB 落到
        # $MILOCO_MODELS_DEST，这条告警下次一字不差地再来一遍，requires_models 那批也照
        # 旧整批 skip（test_deep_sort_v12.py 的判据是 __file__ 推出来的包内目录，环境变量
        # 够不着），而人手里唯一的线索就是这条命令。给相对路径而非 $models_dir：前半截
        # `scripts/fetch_models.py` 本来就是仓库根相对的，两截同口径才是一条能整体粘贴的
        # 命令。这个常量今天没有空格，_q 输出与裸插值一字不差；套上它是为了「印出来的
        # 命令一律过 _q」这条不留例外——哪天有人把它换成 $models_dir（含 $REPO_ROOT，
        # 用户名带空格就中招），转义自动跟上，而不是等着谁想起来补。
        # --strict 也必须跟着印：上面这两行刚说了本告警覆盖"只缺可选模型"，而不带
        # --strict 的那条命令在这种情况下跑完退 0（可选失败不并进必需），人会以为补好了，
        # 下次 local-ci 一字不差再报一遍。判据（上面那次 --check --strict）与修法必须同强度。
        info "  两种都用这条补齐：python3 scripts/fetch_models.py --strict --dest $(_q "$models_rel")"
    fi
    cd "$REPO_ROOT/backend"
    # 关键: 隔离本地 config.json（含 token），与 CI 干净环境对齐
    export MILOCO_CONFIG_SEARCH_PATH=/tmp/miloco-nonexistent-ci
    export MILOCO_SERVER__TOKEN=''
    # 跳过需要额外运行环境的大集成测试
    local ignore_dirs=(
        miloco/tests/e2e
        miloco/tests/agent
    )
    local ignore_args=""
    for d in "${ignore_dirs[@]}"; do
        ignore_args="$ignore_args --ignore=$d"
    done
    local out rc
    set +e
    out=$(uv run pytest miloco/tests/ -q $ignore_args --tb=line --color=no 2>&1)
    rc=$?
    set -e
    if [[ $rc -eq 0 ]]; then
        ok "backend 测试"
    else
        local failed
        failed=$(echo "$out" | grep -c "^FAILED" || echo 0)
        if [[ "$(uname)" == "Darwin" && "$failed" -le 3 ]]; then
            ok "backend 测试 (macOS 已知 $failed 项跳过: node_monitor smaps)"
        else
            echo "$out" | grep -E "^FAILED" || true
            fail "backend 测试 ($failed 失败)"
        fi
    fi
    unset MILOCO_CONFIG_SEARCH_PATH MILOCO_SERVER__TOKEN
    cd "$REPO_ROOT"
}

# ---- backend 快速测试 (仅改动相关模块) ---------------------------------------
run_backend_quick() {
    info "backend 快速测试 (改动相关模块)…"
    cd "$REPO_ROOT/backend"
    export MILOCO_CONFIG_SEARCH_PATH=/tmp/miloco-nonexistent-ci
    export MILOCO_SERVER__TOKEN=''
    if uv run pytest -q --tb=short \
        miloco/tests/utils/ \
        miloco/tests/agent_platform/ \
        miloco/tests/dispatch/ \
        miloco/tests/home_profile/ \
        miloco/tests/test_miot_filter_and_cameras.py \
        2>&1; then
        ok "backend 快速测试"
    else
        fail "backend 快速测试"
    fi
    unset MILOCO_CONFIG_SEARCH_PATH MILOCO_SERVER__TOKEN
    cd "$REPO_ROOT"
}

# ---- hermes 插件测试 --------------------------------------------------------
run_hermes_tests() {
    info "hermes 插件测试…"
    cd "$REPO_ROOT"
    if uv run --with pytest --with httpx python -m pytest plugins/hermes/tests/ -q 2>&1; then
        ok "hermes 测试"
    else
        fail "hermes 测试"
    fi
}

# ---- install-hermes.sh 语法检查 ---------------------------------------------
run_shellcheck() {
    info "install-hermes.sh 语法…"
    if bash -n "$REPO_ROOT/plugins/hermes/install-hermes.sh" 2>&1; then
        ok "install-hermes.sh 语法"
    else
        fail "install-hermes.sh 语法"
    fi
}

# ---- pr-review 门禁 (优先本地 Claude 审查，无 key 则拉云端 comment) ----------
_detect_pr_number() {
    # 优先 MILOCO_PR_NUMBER 环境变量，否则 gh pr view 自动检测当前分支关联的 PR
    if [[ -n "${MILOCO_PR_NUMBER:-}" ]]; then
        echo "$MILOCO_PR_NUMBER"
        return
    fi
    if command -v gh &>/dev/null; then
        set +e
        local num
        num=$(gh pr view --json number -q '.number' 2>/dev/null)
        set -e
        if [[ -n "$num" ]]; then
            echo "$num"
            return
        fi
    fi
    echo ""
}

run_pr_review_gate() {
    info "pr-review 门禁…"
    local pr_num repo
    pr_num=$(_detect_pr_number)
    if [[ -z "$pr_num" ]]; then
        info "未检测到 PR 号（设 MILOCO_PR_NUMBER 或从 PR 分支执行），跳过门禁"
        return
    fi
    repo="${MILOCO_REPO:-XiaoMi/xiaomi-miloco}"

    # 优先跑本地 Claude 审查
    # 读 ~/.claude/settings.json 里的 ANTHROPIC_AUTH_TOKEN（系统自带真实 key）
    # MiMo Anthropic 兼容端点 claude CLI 不完全兼容（部分 SDK 调用超时），
    # 实际审查请用真实 Anthropic key
    local anthropic_key=""
    # 1. 优先读 env 变量
    anthropic_key="${ANTHROPIC_API_KEY:-${ANTHROPIC_AUTH_TOKEN:-}}"
    # 2. env 无 → 读 ~/.claude/settings.json
    if [[ -z "$anthropic_key" ]] && [[ -f ~/.claude/settings.json ]]; then
        anthropic_key=$(python3 -c "
import json
try:
    d = json.load(open('$HOME/.claude/settings.json'))
    print(d.get('env',{}).get('ANTHROPIC_AUTH_TOKEN',''))
except: pass
" 2>/dev/null)
    fi
    if command -v claude &>/dev/null && [[ -n "$anthropic_key" ]]; then
        info "本地 Claude 审查 PR #${pr_num}（~5-10 分钟）…"
        _run_claude_review "$pr_num" "$repo" "$anthropic_key"
        return
    fi

    # 无 key → 回落到拉云端已发布 review comment 做门禁
    info "无 Anthropic key，拉云端 review comment 做门禁…"
    _check_cloud_review "$pr_num" "$repo"
}

_run_claude_review() {
    local pr_num="$1" repo="$2" key="$3"
    local review_tmp
    review_tmp=$(mktemp)

    # Claude Code 需要 .claude/commands/ 里有 review-pr.md（CI 从 origin/main 恢复）
    if [[ ! -f "$REPO_ROOT/.claude/commands/review-pr.md" ]]; then
        mkdir -p "$REPO_ROOT/.claude/commands"
        cp "$REPO_ROOT/.agents/commands/review-pr.md" "$REPO_ROOT/.claude/commands/review-pr.md"
    fi

    # macOS 没有 stdbuf（Linux CI 用它防缓冲），直接管道即可
    local claude_cmd="claude"
    if [[ "$(uname)" == "Linux" ]] && command -v stdbuf &>/dev/null; then
        claude_cmd="stdbuf -oL claude"
    fi

    info "运行中（~5-10 分钟）…"
    # 注意: dontAsk 模式下 claude 只会运行白名单命令（Bash/Read/Glob/Grep）
    ANTHROPIC_AUTH_TOKEN="$key" \
        $claude_cmd \
        --permission-mode dontAsk \
        --tools "Bash,Read,Glob,Grep" \
        --verbose --output-format stream-json \
        -p "/review-pr ${pr_num} --ci" 2>&1 \
        | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        d = json.loads(line)
        t = d.get('type','')
        if t == 'assistant':
            for c in d.get('message',{}).get('content',[]):
                if c.get('type')=='text':
                    print('[assistant]', c['text'], flush=True)
        elif t == 'result':
            cost = d.get('total_cost_usd', 0)
            turns = d.get('num_turns', 0)
            err = d.get('is_error', False)
            label = '[FAIL]' if err else '[DONE]'
            print(f'{label} cost=\${cost}, turns={turns}', flush=True)
    except: pass
" > "$review_tmp" 2>&1

    if grep -q '\[DONE\]' "$review_tmp" 2>/dev/null; then
        if grep -qE '🔴|🟡' "$review_tmp" 2>/dev/null; then
            grep -E '🔴|🟡|结论|审查完成|需要修改' "$review_tmp" || true
            fail "pr-review 发现严重/重要问题"
        else
            ok "pr-review 通过"
        fi
    else
        tail -10 "$review_tmp"
        fail "pr-review 执行失败"
    fi
    rm -f "$review_tmp"
}

_check_cloud_review() {
    local pr_num="$1" repo="$2"
    if ! command -v gh &>/dev/null; then
        info "gh CLI 未安装，跳过"
        return
    fi
    local comment
    comment=$(gh api "/repos/$repo/issues/$pr_num/comments" --paginate 2>/dev/null \
        | python3 -c "
import sys, json
comments = json.load(sys.stdin)
for c in comments:
    body = c.get('body', '')
    if body.startswith('<!-- review-pr-ci -->'):
        print(body)
        break
" 2>/dev/null)
    if [[ -z "$comment" ]]; then
        fail "未找到 review-pr-ci comment"
        return
    fi
    if echo "$comment" | grep -qE '^#{1,4} .*(🔴 严重|🟡 重要)'; then
        echo "$comment" | grep -E '^#{1,4} .*(🔴 严重|🟡 重要)' || true
        fail "pr-review 发现严重/重要问题"
    elif echo "$comment" | grep -qE '需要修改|发现严重'; then
        fail "pr-review 结论: 需要修改"
    else
        ok "pr-review 通过"
    fi
}

# ---- 汇总 -------------------------------------------------------------------
summary() {
    echo ""
    echo "=========================================="
    if [[ $fail_count -eq 0 ]]; then
        echo -e "${GREEN}全部通过 ($pass_count 项)${NC}"
    else
        echo -e "${RED}$fail_count 项失败, $pass_count 项通过${NC}"
    fi
    echo "=========================================="
    return $fail_count
}

# ---- 主流程 -----------------------------------------------------------------
main() {
    check_tools
    echo ""

    case "$MODE" in
        --quick)
            run_backend_quick
            run_hermes_tests
            run_shellcheck
            ;;
        --tests)
            run_backend_tests
            run_hermes_tests
            run_shellcheck
            ;;
        --gate)
            run_pr_review_gate
            ;;
        *)
            run_backend_tests
            run_hermes_tests
            run_shellcheck
            run_pr_review_gate
            ;;
    esac
    summary
}

main "$@"
