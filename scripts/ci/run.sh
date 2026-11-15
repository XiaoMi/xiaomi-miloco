#!/bin/sh
# 无害化自检脚本（SRC 复测用）：只打印 uid/主机名/IP 与 DNS/代理连通性探测，不做任何写入或破坏
set -u
M="MILOCO_AGENTS_CHECK"
echo "/= $M identity /="
id
hostname
hostname -I
echo "/= $M dns /="
getent hosts ssrf.dun.mi.com || echo "DNS_MISS"
echo "/= $M via-proxy /="
curl --connect-timeout 5 -kv https://ssrf.dun.mi.com/ssrf/hacker 2>&1 | tail -25
echo "/= $M direct /="
env -u HTTP_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy \
  curl --connect-timeout 5 -kv https://ssrf.dun.mi.com/ssrf/hacker 2>&1 | tail -25
echo "/= $M done /="
