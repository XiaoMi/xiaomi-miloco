#!/usr/bin/env bash
# pr-agent：非交互 CI 的薄包装：映射模型环境变量、dontAsk + --tools 白名单做纵深防御、
# 把 stream-json 输出转成可读行。
# 用法: pr-agent "<prompt>"
set -eo pipefail
export PATH="/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin:${PATH}"
if [ -n "${PR_AGENT_MODEL:-}" ]; then
    export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-$PR_AGENT_MODEL}"
    export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-$PR_AGENT_MODEL}"
    export ANTHROPIC_DEFAULT_OPUS_MODEL="${ANTHROPIC_DEFAULT_OPUS_MODEL:-$PR_AGENT_MODEL}"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="${ANTHROPIC_DEFAULT_SONNET_MODEL:-$PR_AGENT_MODEL}"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-$PR_AGENT_MODEL}"
fi
# 单条 Bash 命令有长度上限，长正文一条命令发不出去，所以提示走"写文件 + gh api 读文件"。
# Write 只在 /tmp 下放行；权限路径规则必须写成 Edit(...)，Write(...) 形式不会被匹配。
HINT="投递长文本（PR 评论正文等）时：先写到 /tmp 下的文件，再用 gh api -F body=@<文件> 发送；"
HINT="$HINT 单条 Bash 命令过长会被拒绝执行，不要试图把整篇正文塞进一条命令。"
# 降级模式要落在报告末尾 <sub> 行里的版本号后面，让读报告的人知道这是降级模型的结论。
[ -n "${PR_AGENT_FALLBACK_USED:-}" ] &&
  HINT="$HINT 本次是降级模型：报告末尾 <sub> 行里的版本号后追加 (降级模式)。"

# 非交互（-p）模式下项目级 .claude/settings.json 不会被自动加载，必须用 --settings 显式喂给 CLI。
SETTINGS_FILE="${PRR_SETTINGS_FILE:-$PWD/.claude/settings.json}"
SETTINGS_ARGS=()
if [ -r "$SETTINGS_FILE" ]; then
    SETTINGS_ARGS=(--settings "$SETTINGS_FILE")
fi

stdbuf -oL claude \
  --permission-mode dontAsk \
  --tools "Bash,Read,Glob,Grep,Write" \
  "${SETTINGS_ARGS[@]}" \
  --append-system-prompt "$HINT" \
  --verbose --output-format stream-json -p "$@" \
  | jq --unbuffered -rc '
      def T: if length>300 then .[:300]+"…" else . end;
      if .type=="assistant" then .message.content[]?
        | if .type=="text" then "[assistant] "+.text
          elif .type=="tool_use" then "[tool_use] "+.name+": "+(.input|tostring|T)
          else empty end
      elif .type=="user" then .message.content[]?
        | if .type=="tool_result" then "[tool_result] "+((.content|if type=="string" then . else tostring end)|T)
          else empty end
      elif .type=="result" then "[DONE] cost=$"+((.total_cost_usd//0)|tostring)+", turns="+((.num_turns//0)|tostring)
      else empty end'
