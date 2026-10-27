#!/bin/bash
# runner 容器入口：把一次性登记凭据交给 runner 程序。参数已给 --jitconfig 则原样放行；
# 否则等启动器把凭据拷成文件（默认 /opt/runner/.jitconfig），读完立刻删。
# 磁盘和宿主侧都不留凭据：文件读完即删，容器命令行与 config.json 里也只有路径。
# 最后一跳只能走 run.sh --jitconfig：runner 只认这一个入口，所以凭据会出现在容器内该进程的
# 命令行上。这是可接受的——JIT 配置一次性，job 开始前就被 runner 消费掉，重放无效。
set -euo pipefail

JIT_FILE="${PRR_JIT_FILE:-/opt/runner/.jitconfig}"

if [ "${1:-}" = "--jitconfig" ]; then
    exec /opt/runner/run.sh "$@"
fi

for _ in $(seq 1 "${PRR_JIT_WAIT_SECONDS:-90}"); do
    [ -s "$JIT_FILE" ] && break
    sleep 1
done

if [ ! -s "$JIT_FILE" ]; then
    echo "没等到登记凭据（${JIT_FILE}）：启动器是不是没拷进来？" >&2
    exit 64
fi

JIT="$(cat "$JIT_FILE")"
rm -f "$JIT_FILE"        # 读完立刻抹掉

exec /opt/runner/run.sh --jitconfig "$JIT" "$@"
