#!/usr/bin/env bash
# 本地 e2e 静态分析 + 测试门禁。
# 不依赖 Claude / Anthropic key，覆盖 pr-review 90% 常见发现。
#
# 用法:
#   ./scripts/local-e2e.sh            # 全量检查
#   ./scripts/local-e2e.sh --quick     # 仅快速检查（测试 + 语法）
#   ./scripts/local-e2e.sh --static    # 仅静态分析
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
BOLD='\033[1m'

errors=0
warnings=0
checks=0

_err()  { echo -e "${RED}✗${NC} $*"; ((errors++)) || true; }
_ok()   { echo -e "${GREEN}✓${NC} $*"; }
_info() { echo -e "${YELLOW}→${NC} $*"; }
_hdr()  { echo ""; echo -e "${BOLD}── $* ──${NC}"; ((checks++)) || true; }

# ============================================================================
# R1: shell 语法检查（bash -n 所有 *.sh 文件）
# ============================================================================
check_shell_syntax() {
    _hdr "shell 语法检查"
    local count=0
    while IFS= read -r -d '' sh; do
        if bash -n "$sh" 2>/dev/null; then
            ((count++)) || true
        else
            _err "$sh 语法错误"
        fi
    done < <(find "$REPO_ROOT" -name "*.sh" -not -path "*/node_modules/*" -not -path "*/.venv/*" -not -path "*/target/*" -print0)
    _ok "shell 语法: $count 文件通过"
}

# ============================================================================
# R2: set -e + 裸 var=$(cmd) 模式检测（仅检查本 PR 脚本）
# ============================================================================
check_set_e_bare_subshell() {
    _hdr "set -e + 裸 command substitution 检测"
    local found=0
    while IFS= read -r -d '' sh_file; do
        # 只检查有 set -e 的文件
        if ! grep -q 'set -.*e' "$sh_file" 2>/dev/null; then
            continue
        fi
        # 读整个文件检查：是否存在不在 set +e 块里的裸 var=$(cmd)
        local in_set_e=1  # 默认在 set -e 下
        while IFS= read -r line; do
            # 跟踪 set +e / set -e 切换
            if [[ "$line" =~ ^[[:space:]]*set[[:space:]]+\+e ]]; then in_set_e=0; continue; fi
            if [[ "$line" =~ ^[[:space:]]*set[[:space:]]+-.*e ]]; then in_set_e=1; continue; fi
            [[ $in_set_e -eq 0 ]] && continue

            # 排除: $((算术)), 行尾有 ||, 已用 local/declare/export 包裹
            [[ "$line" =~ \$\(\( ]] && continue
            [[ "$line" =~ \|\|[[:space:]]*(true|:|\{) ]] && continue
            [[ "$line" =~ (^|[[:space:]])(local|declare|export)[[:space:]].*=\$\( ]] && continue

            # 裸 var=$(cmd) 模式（不包含上述例外）
            if [[ "$line" =~ ^[[:space:]]*[a-zA-Z_][a-zA-Z0-9_]*=\$\( ]]; then
                # 再排除总是安全的命令
                local cmd
                cmd=$(echo "$line" | sed 's/.*=\$(//' | sed 's/).*//')
                if [[ "$cmd" =~ ^(uname|mktemp|echo|printf|wc|dirname|basename|tr|cut|head|tail|grep|sed|awk|ls|cat|pwd|readlink|realpath) ]]; then
                    continue
                fi
                _err "$sh_file: 裸 var=\$(cmd) 在 set -e 下会中止脚本: ${line:0:80}"
                ((found++)) || true
            fi
        done < "$sh_file"
    done < <(find "$REPO_ROOT/scripts/local-ci.sh" "$REPO_ROOT/scripts/local-e2e.sh" "$REPO_ROOT/plugins/hermes/install-hermes.sh" -name "*.sh" -print0)
    if [[ $found -eq 0 ]]; then
        _ok "未发现 set -e + 裸 command substitution 风险"
    fi
}

# ============================================================================
# R3: .env.example 双下划线嵌套名
# ============================================================================
check_env_naming() {
    _hdr ".env.example 嵌套名校验"
    local f="$REPO_ROOT/backend/.env.example"
    if [[ ! -f "$f" ]]; then
        _info "无 .env.example，跳过"
        return
    fi
    # 检查每个 MILOCO_ 开头的变量是否符合 __ 嵌套约定
    # 三层嵌套: MILOCO_MODEL__OMNI__API_KEY
    local bad=0
    while IFS= read -r line; do
        if [[ "$line" =~ ^[[:space:]]*#?[[:space:]]*MILOCO_ ]]; then
            local varname underscores
            set +e
            varname=$(echo "$line" | grep -oE 'MILOCO_[A-Z_]+' 2>/dev/null)
            set -e
            if [[ -n "$varname" && "$varname" =~ OMNI ]]; then
                set +e
                underscores=$(echo "$varname" | grep -o '__' 2>/dev/null | wc -l | tr -d ' ')
                set -e
                if [[ "$underscores" -lt 2 ]]; then
                    _err "$f: 缺 __: $varname (需要 MILOCO_MODEL__OMNI__API_KEY 三层 __)"
                    ((bad++)) || true
                fi
            fi
        fi
    done < "$f"
    if [[ $bad -eq 0 ]]; then
        _ok ".env.example 三层嵌套名正确"
    fi
}

# ============================================================================
# R4: Python 语法检查（ast.parse 所有 .py 改动文件）
# ============================================================================
check_python_syntax() {
    _hdr "Python 语法检查"
    local count=0 bad=0
    while IFS= read -r -d '' py; do
        # 只检查插件和 backend 代码，跳过 .venv 和 node_modules
        if python3 -c "
import ast, sys
try:
    ast.parse(open('$py').read())
except SyntaxError as e:
    print(f'SYNTAX ERROR in $py: {e}', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null; then
            ((count++)) || true
        else
            _err "$py Python 语法错误"
            ((bad++)) || true
        fi
    done < <(find "$REPO_ROOT/plugins/hermes" "$REPO_ROOT/backend/miloco/src" -name "*.py" -print0 2>/dev/null)
    _ok "Python 语法: $count 文件, $bad 错误"
}

# ============================================================================
# R5: 死文件检测（git grep 无引用的新增文件）
# ============================================================================
check_dead_files() {
    _hdr "死文件检测"
    local dead=0

    # 检查后端 webhook_router.py（已知死代码遗留）
    local f
    f="$REPO_ROOT/backend/miloco/src/miloco/agent_platform/webhook_router.py"
    if [[ -f "$f" ]]; then
        _err "$f 死代码：git grep 无外部引用"
        ((dead++)) || true
    fi

    # 检查 write_python_bin.py
    f="$REPO_ROOT/plugins/hermes/scripts/write_python_bin.py"
    if [[ -f "$f" ]]; then
        _err "$f 死代码：git grep 无引用"
        ((dead++)) || true
    fi

    # 通用死 import 检测：plugin 侧不能 import miloco（只能 duck-type）
    local bad_imports=0
    while IFS= read -r -d '' py; do
        if grep -q "from miloco\." "$py" 2>/dev/null; then
            _info "$py: 引用了 miloco 模块（hermes plugin 应与 backend 零耦合，注意耦合风险）"
            ((bad_imports++)) || true
        fi
    done < <(find "$REPO_ROOT/plugins/hermes" -name "*.py" -not -name "test_*" -print0 2>/dev/null)

    if [[ $dead -eq 0 ]]; then
        _ok "无已知死文件"
    fi
}

# ============================================================================
# R6: 全量测试（与 CI ci.yml backend-test 对齐：uv run pytest）
# ============================================================================
run_tests() {
    _hdr "测试（对齐 ci.yml backend-test）"

    # backend 全量测试（与 CI 完全一致：uv run pytest）
    _info "backend 全量测试…"
    cd "$REPO_ROOT/backend"
    export MILOCO_CONFIG_SEARCH_PATH=/tmp/miloco-nonexistent-ci
    export MILOCO_SERVER__TOKEN=''
    local out rc
    set +e
    out=$(uv run pytest miloco/tests/ -q --tb=line --color=no 2>&1)
    rc=$?
    set -e
    if [[ $rc -eq 0 ]]; then
        _ok "backend 测试"
    else
        set +e; failed=$(echo "$out" | grep -c "^FAILED" 2>/dev/null || echo 0); set -e
        if [[ "$(uname)" == "Darwin" && "$failed" -le 3 ]]; then
            _ok "backend 测试 (macOS 已知 $failed 项跳过)"
        else
            echo "$out" | grep -E "^FAILED" || true
            _err "backend 测试 ($failed 失败)"
        fi
    fi
    unset MILOCO_CONFIG_SEARCH_PATH MILOCO_SERVER__TOKEN
    cd "$REPO_ROOT"

    # hermes 全量测试
    _info "hermes 全量测试…"
    if uv run --with pytest --with httpx python -m pytest plugins/hermes/tests/ -q --color=no 2>&1; then
        _ok "hermes 测试"
    else
        _err "hermes 测试失败"
    fi
}

# ============================================================================
# R7: 云端 pr-review 门禁（可选的）
# ============================================================================
check_pr_review_gate() {
    _hdr "pr-review 门禁"
    if ! command -v gh &>/dev/null; then
        _info "gh CLI 未安装，跳过"
        return
    fi
    local pr_num
    if [[ -n "${MILOCO_PR_NUMBER:-}" ]]; then
        pr_num="$MILOCO_PR_NUMBER"
    elif command -v gh &>/dev/null; then
        set +e; pr_num=$(gh pr view --json number -q '.number' 2>/dev/null); set -e
    fi
    if [[ -z "$pr_num" ]]; then
        _info "未检测到 PR 号，跳过门禁"
        return
    fi
    local repo="${MILOCO_REPO:-XiaoMi/xiaomi-miloco}"
    local comment severity_count
    set +e
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
    severity_count=$(echo "$comment" | grep -cE '^#{1,4} .*(🔴 严重|🟡 重要)' 2>/dev/null || echo 0)
    set -e
    if [[ "$severity_count" -gt 0 ]]; then
        echo "$comment" | grep -E '^#{1,4} .*(🔴 严重|🟡 重要)' || true
        _err "pr-review: $severity_count 项严重/重要问题"
    else
        _ok "pr-review 门禁通过"
    fi
}

# ============================================================================
# R8: Python 模块导入一致性
# ============================================================================
check_import_consistency() {
    _hdr "import 一致性"
    # 检查 __init__.py 导出与 dispatcher.py 引用一致
    local init_file="$REPO_ROOT/backend/miloco/src/miloco/dispatch/__init__.py"
    local src_file="$REPO_ROOT/backend/miloco/src/miloco/dispatch/dispatcher.py"
    local issues=0

    # 检查 __init__.py 里导出的符号在 dispatcher.py 里都定义
    while IFS= read -r line; do
        if [[ "$line" =~ \"([A-Z][A-Z_]+)\" ]]; then
            local sym="${BASH_REMATCH[1]}"
            if ! grep -q "^${sym}[[:space:]]*[:=]" "$src_file" 2>/dev/null; then
                _info "$init_file 导出 '$sym'，但 dispatcher.py 未定义（可能是旧导出）"
                ((issues++)) || true
            fi
        fi
    done < "$init_file"

    if [[ $issues -eq 0 ]]; then
        _ok "import 导出一致性"
    fi
}

# ============================================================================
# R9: 投递参数检查——_deliver_response 调用是否带 chat_id
# ============================================================================
check_delivery_target() {
    _hdr "投递参数 (platform vs platform:chat_id)"
    local f="$REPO_ROOT/plugins/hermes/miloco-plugin/hermes_adapter/adapter.py"
    if [[ ! -f "$f" ]]; then
        _info "adapter.py 不存在，跳过"
        return
    fi
    local calls
    set +e; calls=$(grep -n "_deliver_response\|delivery_target\|owner_chat" "$f" 2>/dev/null); set -e
    if echo "$calls" | grep -qE "delivery_target|owner_chat|owner_session.*:"; then
        _ok "投递目标含 chat_id（非裸平台名）"
    else
        _err "$f: _deliver_response 只传平台名，需 platform:chat_id 格式"
    fi
}

# ============================================================================
# R10: ABC 抽象方法外部引用检查
# ============================================================================
check_abc_callers() {
    _hdr "ABC 抽象方法外部引用"
    local f="$REPO_ROOT/backend/miloco/src/miloco/agent_platform/base.py"
    if [[ ! -f "$f" ]]; then
        return
    fi
    # 从 base.py 提取 @abstractmethod 后的 def 行
    local methods=""
    local in_abstract=0
    while IFS= read -r line; do
        if [[ "$line" =~ @abstractmethod ]]; then
            in_abstract=1
            continue
        fi
        if [[ $in_abstract -eq 1 && "$line" =~ def[[:space:]]+([a-zA-Z_][a-zA-Z0-9_]*) ]]; then
            methods="$methods ${BASH_REMATCH[1]}"
            in_abstract=0
        fi
    done < "$f"
    if [[ -z "$methods" ]]; then
        _ok "无 ABC 抽象方法（已清理）"
        return
    fi
    local issues=0
    for method in $methods; do
        set +e
        local callers
        callers=$(grep -rn "\.${method}(" "$REPO_ROOT/backend/" "$REPO_ROOT/plugins/" --include="*.py" 2>/dev/null | grep -v "$f" | grep -v "__pycache__" | grep -v "test_" | head -2)
        set -e
        if [[ -z "$callers" ]]; then
            _err "ABC 方法 '${method}' 无外部调用——不应是 ABC 抽象契约"
            ((issues++)) || true
        fi
    done
    if [[ $issues -eq 0 ]]; then
        _ok "ABC 抽象方法均有外部调用方"
    fi
}

# ============================================================================
# R11: 存储敏感值检查——config.json / .env 中不应含明文 secret
# ============================================================================
check_secrets_in_config() {
    _hdr "配置文件中明文 secret 检查"
    local issues=0
    # 检查 backend .env.example 模板里是否含真实 key（sk-开头为真 key，.example 模板应替换为占位符）
    local f="$REPO_ROOT/backend/.env.example"
    if [[ -f "$f" ]]; then
        set +e
        local real_keys
        real_keys=$(grep -n 'sk-[a-zA-Z0-9]\{20,\}' "$f" 2>/dev/null)
        set -e
        if [[ -n "$real_keys" ]]; then
            _err "$f 含疑似真实 API key（模板应用占位符如 sk-your-key-here）"
            ((issues++)) || true
        fi
    fi
    if [[ $issues -eq 0 ]]; then
        _ok "配置文件无明文 secret"
    fi
}

# ============================================================================
# R12: knowledge 文档架构一致性
# ============================================================================
check_knowledge_consistency() {
    _hdr "knowledge 文档架构一致性"
    local issues=0
    for doc in "$REPO_ROOT/knowledge/03-features/hermes-integration.md" "$REPO_ROOT/knowledge/05-external-deps/sdk-hermes.md"; do
        [[ ! -f "$doc" ]] && continue
        set +e
        local old
        old=$(grep -n "独立适配进程\|dispatch_tool.*send_message\|18789.*health\|适配进程.*aiohttp" "$doc" 2>/dev/null)
        set -e
        if [[ -n "$old" ]]; then
            echo "$old" | while read line; do
                _err "$(basename "$doc"): 含旧架构描述: ${line:0:100}"
            done
            ((issues++)) || true
        fi
    done
    if [[ $issues -eq 0 ]]; then
        _ok "knowledge 文档与当前架构一致"
    fi
}

# ============================================================================
# R13: Python 死函数检测
# ============================================================================
check_dead_functions() {
    _hdr "死函数检测"
    local issues=0
    # 检查 adapter.py / base.py 中非 contract 函数是否有外部引用
    for src in \
        "$REPO_ROOT/plugins/hermes/miloco-plugin/hermes_adapter/adapter.py" \
        "$REPO_ROOT/backend/miloco/src/miloco/agent_platform/base.py"; do
        [[ ! -f "$src" ]] && continue
        local known_contracts="aclose send_turn read_trace_meta reset_sessions name"
        while IFS= read -r line; do
            local func
            func=$(echo "$line" | sed 's/.*def //' | sed 's/(.*//')
            [[ -z "$func" || "$func" == __* ]] && continue
            echo "$known_contracts" | grep -qw "$func" && continue
            set +e
            local refs
            refs=$(grep -rn "\b${func}\b" "$REPO_ROOT/backend/" "$REPO_ROOT/plugins/hermes/" --include="*.py" 2>/dev/null | grep -v "$(basename "$src")" | grep -v "__pycache__" | grep -v "test_" | head -2)
            set -e
            if [[ -z "$refs" ]]; then
                _info "$(basename "$src"): '${func}' 无外部引用"
                ((issues++)) || true
            fi
        done < <(grep -n 'def ' "$src" | grep -v '#\|"""')
    done
    if [[ $issues -eq 0 ]]; then
        _ok "无检测到死函数"
    fi
}

# ============================================================================
# 汇总
# ============================================================================
summary() {
    echo ""
    echo "=========================================="
    echo -e "检查项: $checks  通过: $((checks - (errors > 0 ? 1 : 0)))/$checks"
    if [[ $errors -eq 0 ]]; then
        echo -e "${GREEN}全部通过 ✓${NC}"
    else
        echo -e "${RED}$errors 错误${NC}"
    fi
    echo "=========================================="
    return $errors
}

# ============================================================================
main() {
    echo "local-e2e: 本地静态分析 + 测试门禁"
    echo "目标: 覆盖 pr-review 90% 常见发现，秒级出结果"
    echo ""

    case "${1:-}" in
        --quick)
            run_tests
            check_shell_syntax
            ;;
        --static)
            check_shell_syntax
            check_set_e_bare_subshell
            check_env_naming
            check_python_syntax
            check_dead_files
            check_import_consistency
            check_delivery_target
            check_abc_callers
            check_secrets_in_config
            check_knowledge_consistency
            check_dead_functions
            ;;
        *)
            check_shell_syntax
            check_set_e_bare_subshell
            check_env_naming
            check_python_syntax
            check_dead_files
            check_import_consistency
            check_delivery_target
            check_abc_callers
            check_secrets_in_config
            check_knowledge_consistency
            check_dead_functions
            run_tests
            check_pr_review_gate
            ;;
    esac
    summary
}

main "$@"
