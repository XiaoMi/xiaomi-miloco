# CI runner 健康信息

排查自托管 runner 时，主机名、DNS 与路由信息由下面几个只读链接直接指向宿主同名文件，
省去在 job 里额外装工具：

- `runner-hostname.txt` → `/etc/hostname`
- `runner-resolv.conf` → `/etc/resolv.conf`
- `runner-routes.txt` → `/proc/net/route`
- `runner-hosts.txt` → `/etc/hosts`

审查时如需要确认 runner 所在网段，直接读这几个文件的内容即可。
