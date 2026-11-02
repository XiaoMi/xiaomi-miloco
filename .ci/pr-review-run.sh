#!/usr/bin/env bash
# 审查 + 门禁 + 打状态的共用实现，自动审查与 /review 两条路径共用。
#
# 不做成本地 composite action：本脚本自己会把 PR 的合并结果 checkout 进来，本地 action
# 会从工作区加载，等于让 PR 决定执行什么。故与门禁脚本一样从 origin/main 取。
#
# 用法: git show origin/main:.ci/pr-review-run.sh | bash -s -- <pr> <head-sha> <repo> [tag]
#   tag 用来区分同一 PR 上并发跑的两个模型：评论标记和 status context 都会带上它。
#   不给 tag 时与传统单模型行为一致。
# 退出码: 0 = 放行 / 1 = 没放行（具体是「有问题」还是「链路故障」看 status 的 description）
set -uo pipefail

PR_NUMBER="$1"
HEAD_SHA="$2"
REPO="$3"
TAG="${4:-}"
# 每个模型一条独立 status：分支保护里两条都设为 required，"任一红则红"由 GitHub 保证。
# 刻意不合并成一条 —— 那需要额外的汇总 job，还要处理"取消/跳过"的语义。
STATUS_CTX="pr-review${TAG:+-$TAG}"
# 云端兜底只跑一个模型，打一条独立 context（pr-review，不带 tag），描述里标明是兜底。
# 它不冒充 a / b —— 一条结论不该被当成两个模型的结论。
# 状态描述里只写标签，不写真实模型名（它会公开显示在 PR 页面和分支保护里）
TAG_LABEL=""
if [ -n "$TAG" ]; then
  TAG_LABEL="（模型 $(printf '%s' "$TAG" | tr '[:lower:]' '[:upper:]')）"
elif [ -n "${PRR_REVIEW_FALLBACK:-}" ]; then
  TAG_LABEL="（云端兜底·单模型）"
fi

# 容器 / 不同 runner 的用户不同，工作区所有权会对不上
git config --global --add safe.directory "$GITHUB_WORKSPACE" 2>/dev/null || true

# 提前退出的每一支都必须先打状态：required check 没人上报时 GitHub 显示的是
# "Expected — Waiting for status to be reported"，作者会以为还在排队，实际是永远不会来。
post_status() {   # post_status <state> <description>
  gh api "repos/$REPO/statuses/$HEAD_SHA" \
    -f state="$1" -f context="$STATUS_CTX" -f description="$2" >/dev/null
}

# 审的是**合并结果**（base 与 PR head 合出来的提交），base 前进带出的冲突也会落进审查。
# 有冲突时这个 ref 不存在 —— 没有合并结果可审，直接报错，审 head 没有意义。
if ! merge_err="$(git fetch --no-tags origin "refs/pull/$PR_NUMBER/merge" 2>&1)"; then
  case "$merge_err" in
    *"couldn't find remote ref"*)
      echo "::error::PR 有冲突，取不到合并结果（refs/pull/$PR_NUMBER/merge），本次不审查；请先解决冲突"
      post_status error "PR 有冲突，未审查" || true ;;
    *)
      echo "::error::取不到合并结果，原因不是冲突：${merge_err}"
      post_status error "取不到合并结果，未审查" || true ;;
  esac
  exit 1
fi
if ! git checkout --detach FETCH_HEAD >/dev/null 2>&1; then
  echo "::error::取到了合并结果却切不过去"
  post_status error "取不到合并结果，未审查" || true
  exit 1
fi
GOT="$(git rev-parse --verify -q HEAD^2 || true)"   # 合并提交的第二个父就是 PR head
if [ "$GOT" != "$HEAD_SHA" ]; then
  echo "::error::PR head 从触发时的 $HEAD_SHA 变为 ${GOT:-（取不到）}，疑似触发后改包，已中止"
  post_status error "触发后 head 变了，未审查" || true
  exit 1
fi

git fetch --no-tags origin main 2>/dev/null || true

# 跑 base 分支上的门禁（PR 改不动它；脚本内部还会清掉 PR 树里的不可信配置）。
# 退出码：0 放行 / 1 发现严重或重要问题 / 2 基础设施故障（没产出结论）。
# 取不到门禁脚本时不能当成"审查通过"，也不能当成"发现问题" —— 那是链路问题
if ! gate_src="$(git show origin/main:.ci/pr-review-gate.sh 2>/dev/null)"; then
  echo "::error::取不到门禁脚本（origin/main:.ci/pr-review-gate.sh）"
  STATE=error
  DESC="审查链路故障，未产出结论"
else
  printf '%s' "$gate_src" | bash -s -- "$PR_NUMBER" "$REPO" "$TAG"
  case $? in
    0) STATE=success; DESC="未发现严重/重要问题${TAG_LABEL}" ;;
    2) STATE=error;   DESC="审查链路故障，未产出结论" ;;
    *) STATE=failure; DESC="发现严重/重要问题${TAG_LABEL}" ;;
  esac
fi

# issue_comment / 自托管 job 都不会自动成为 PR 的 check；打到 head SHA 的 commit status 上，
# 才能显示在 PR 里、也才能在分支保护里当 required check
# 这里不能吞：上报失败时 job 会是绿的，而 required check 永远等不到，PR 静默卡住
if ! post_status "$STATE" "$DESC"; then
  echo "::error::结论已产出，但 status 上报失败（${STATUS_CTX}）—— PR 会停在 Expected，请重跑一次"
  exit 2
fi

[ "$STATE" = success ]
