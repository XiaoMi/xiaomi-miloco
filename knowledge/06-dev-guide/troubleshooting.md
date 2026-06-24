# 故障排查

## 服务无法启动

| 现象                                    | 诊断 / 解决                                                                                                                                     |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| 端口被占用（`port already in use`）     | `miloco-cli service status`（若 `running=True, managed=False` 是别的进程占了端口）→ `ss -tlnp sport = :1810` 查进程；或 `service stop && start` |
| `server.python_bin 未配置` / 不可执行   | 重跑 `install.sh`（自动探测）；或 `miloco-cli config set server.python_bin /path/to/.venv/bin/python`                                           |
| Python 版本不符                         | 要求 >= 3.11，`uv run python --version` 检查                                                                                                    |
| `workers != 1` 报 `NotImplementedError` | Server 不支持多 worker，检查环境变量 `WEB_CONCURRENCY` 是否被容器/系统设置                                                                      |

---

## 小米账号 / MiOT

| 现象                           | 解决                                                                                       |
| ------------------------------ | ------------------------------------------------------------------------------------------ |
| 设备列表为空 / API 返认证错误  | `miloco-cli account status` 查绑定状态；按提示走 `account bind` → 浏览器授权 → 粘贴 base64 |
| token 过期（自动刷新失败）     | `account unbind && bind` 重绑                                                              |
| `admin home-info` 显示设备为空 | `miloco-cli device refresh` 触发云端刷新                                                   |

---

## 设备控制

| 现象                                   | 原因 / 解决                                                                            |
| -------------------------------------- | -------------------------------------------------------------------------------------- |
| `did 'xxx' not found`                  | 设备重新配网后 did 变了 → `device refresh` + `device list` 取新 ID                     |
| `iid 'prop.x.x' not in spec`           | spec 缺失或过时 → `device spec <did>` 查支持项；为空时 `device refresh`                |
| 控制命令返回成功但设备无反应           | 查设备是否在线（`device list` 看 `online` 字段）；确认 did 在启用的家庭 scope 内       |
| 调用 action 报 `provide <iid> <value>` | 误用了 `device control` 调 action key；action 需用 `device action <did> <key> [值...]` |

---

## 感知引擎

| 现象                                      | 解决                                                                                                                                                                                    |
| ----------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 引擎未运行                                | `curl -H "Authorization: Bearer <token>" http://127.0.0.1:1810/api/perception/engine/status` 查状态；用 `engine/start` 端点启动                                                         |
| 引擎不可用（节点处于 `PREREQ_MISSING`）   | 看响应体 `engine.status` 字段：`no_omni_api_key` → `config set model.omni.api_key`；`models_missing` → 重跑 `install.sh` 补全 2 个 ONNX；`engine_init_failed` → 看 `miloco-backend.log` |
| 感知降级模式（引擎 PREREQ_MISSING）的范围 | 仅感知推理跳过；设备控制、规则 CRUD、家庭面板等功能不受影响。`/health` 在此状态下返回 200（PREREQ_MISSING 是预期等待态，非 FAILED）                                                     |
| 感知端点返回 503                          | 引擎未就绪时所有需要引擎的 API 返回 503；先启动引擎再调用                                                                                                                               |
| 无感知日志                                | 依次排查：(1) 引擎是否在运行 (2) Gate 是否过滤（环境无变化）(3) 相邻去重（descriptions 没变就不写）(4) 无感知设备 → `miloco-cli perceive devices` 确认                                  |
| Omni 调用失败                             | `config get model.omni.api_key` 是否非空；确认 Omni API 服务地址网络连通                                                                                                                |

---

## 规则

| 现象                     | 解决                                                                                                                                                  |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| 规则不触发               | `rule list --pretty` 看 `enabled=true`；`perceive_device_ids` 是有效感知设备（`perceive devices` 确认）                                               |
| 日志显示 `skipped=true`  | STATIC：幂等检查发现已达目标；非幂等：冷却期内                                                                                                        |
| DYNAMIC 规则不回调 Agent | 检查 `agent.webhook_url`（默认值见 `settings.schema.json::agent.webhook_url`）；确认 OpenClaw 进程运行且插件已加载；查 `agent.auth_bearer` 是否已写入 |
| 创建规则返回 code=2002   | 规则名重复（`ConflictException`），改用唯一名称                                                                                                       |
| `rule create` 报 422     | condition.query 措辞被拒绝：不能以"检测到/识别到/感知到"等断言性词汇开头；改为进行时状态描述                                                          |

排查规则日志：`miloco-cli rule logs --since 1h --rule <id> --pretty`

---

## CLI 连接失败

| 现象                               | 解决                                                                                                                    |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `cannot connect to Miloco backend` | `service status` 看进程是否在运行；`config get server.url` 看后端地址是否正确                                           |
| code=1003 认证失败（HTTP 401）     | `config show --unmasked` 看 token；多次重装可能 token 不同步 → 删 `config.json::server.token` 重启 backend 让它重新生成 |

---

## 摄像头连接问题

### 现象

摄像头在米家 App 中在线，但 miloco 拉流失败：感知引擎无法连接摄像头、watch 页面显示"等待码流"后超时、系统日志出现防火墙阻断记录。

### 原因

miloco 通过 PPCS P2P 协议拉取摄像头码流，底层依赖 UDP。摄像头主动向 miloco 所在机器发送 UDP 包，若系统防火墙默认 DROP 入站 UDP，连接无法建立。

### 诊断

```bash
miloco-cli doctor
```

### 解决（Linux）

```bash
# Ubuntu / Debian (ufw) — 允许局域网 UDP 入站
sudo ufw allow from 192.168.0.0/16 proto udp

# CentOS / Fedora (firewalld)
sudo firewall-cmd --zone=public \
  --add-rich-rule='rule family=ipv4 source address=192.168.0.0/16 protocol value=udp accept' \
  --permanent && sudo firewall-cmd --reload

# 通用 iptables
sudo iptables -I INPUT -p udp -s 192.168.0.0/16 -j ACCEPT
```

---

## WSL 环境摄像头连接

WSL 默认 NAT 网络模式，局域网设备无法向 WSL 发 UDP 包，需启用镜像网络模式：

**1. 启用镜像网络**（`%USERPROFILE%\.wslconfig`）：

```ini
[wsl2]
networkingMode=mirrored
```

重启 WSL：`wsl --shutdown`

**2. 配置 Hyper-V 防火墙**（Windows PowerShell 管理员）：

```powershell
Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
```

**3. 验证**：在 WSL 内运行 `miloco-cli doctor`

---

## 身份识别

| 现象                                   | 排查 / 解决                                                                                      |
| -------------------------------------- | ------------------------------------------------------------------------------------------------ |
| 陌生人池（tier_u）为空，无法注册新成员 | 确认感知引擎已启动且摄像头在线；等待有人经过镜头触发感知                                         |
| 人脸注册后感知仍输出 `unknown_<n>`     | `miloco-cli identity member list` 确认注册成功；检查感知日志确认识别请求是否到达                 |
| web 面板注册预览无候选人               | `miloco-cli identity pool fetch --cam <cam_id>` 确认陌生人池有数据；若无数据同上排查感知是否运行 |

---

## 设备欢迎

| 现象                   | 排查 / 解决                                                                                                                                                              |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 新设备绑定后无欢迎播报 | `GET /api/miot/mips_status` 检查 MQTT 连接状态；确认新设备在启用家庭 scope 内                                                                                            |
| 欢迎功能偶发不触发     | 查 `miloco-backend.log`：listener（`mips_listeners.py`）日志确认 bind / hr_change 事件是否到达；`DeviceWelcomeService` 日志确认欢迎是否实际发送（skipped / OK / FAILED） |

---

## 日志位置

| 日志          | 路径                                                                        |
| ------------- | --------------------------------------------------------------------------- |
| Server        | `$MILOCO_HOME/log/miloco-backend.log`（实时：`miloco-cli service logs -f`） |
| supervisor    | `$MILOCO_HOME/log/supervisord.log`                                          |
| CLI 调试      | `$MILOCO_HOME/log/miloco-cli.log`（`debug=true` 时启用）                    |
| OpenClaw 插件 | `$MILOCO_HOME/log/openclaw-plugin.log`（需插件配置）                        |

---

## 错误码速查

| HTTP 状态码 | 触发条件                                                            |
| ----------- | ------------------------------------------------------------------- |
| `401`       | Bearer token 无效或缺失（`AuthenticationException`，code=1003）     |
| `422`       | 请求参数 Pydantic 校验失败（code=1002）                             |
| `503`       | 感知引擎未就绪访问引擎端点；或 `server.token` 未配置访问 SPA 根路径 |

| 业务 code | 含义              | 常见场景                            |
| --------- | ----------------- | ----------------------------------- |
| 1001      | 请求参数错误      | 缺必填参数、格式不对                |
| 1002      | Pydantic 校验失败 | 请求体字段类型/格式不符（HTTP 422） |
| 1003      | 认证失败          | token 无效 / 缺失                   |
| 2001      | 资源不存在        | 规则 / 成员 / 设备 ID 不存在        |
| 2002      | 资源冲突          | 规则名重复、成员名重复              |
| 3200      | MiOT 异常         | 小米云通信失败                      |
| 3201      | OAuth 异常        | 未绑定或 token 过期                 |
| 9000      | 系统错误          | 未捕获的内部异常                    |
