# 感知触发

## 背景与目标

传统感知流水线主要依赖摄像头画面变化来决定何时分析画面，但很多家庭事件本身是先由米家设备上报的，例如门锁状态变化、人体传感器数值变化、晾衣机状态变化等；也可以在 Miloco 主动执行米家场景成功后补一次现场确认。

“感知触发”能力让 Miloco 可以在这些米家事件发生时，按用户配置主动查看关联摄像头，把“设备事件”转换成“带视觉上下文的感知结果”，用于后续规则判断、事件沉淀和 Agent 处理。

---

## 产品面

### 能做什么

- **设备属性变化触发感知**：当指定设备的某个属性发生变化时，触发关联摄像头感知
- **设备事件触发感知**：当指定设备上报 MIoT event 时触发感知，可继续按事件参数和值筛选
- **场景触发感知**：当 Miloco 主动执行指定米家场景成功时，触发关联摄像头感知
- **属性条件筛选**：可按属性和值配置触发条件，避免任意事件都触发感知
- **感知提示**：可为每条映射附加默认提示，引导模型重点关注某类画面信息
- **最近触发日志**：查看事件是否命中映射、是否发起感知、是否成功产生结果
- **快照与视频回放**：触发后可回看本次感知对应的快照和视频片段

### 典型场景

**场景 1 — 智能晾衣机状态变化**：当“米家智能晾衣机Pro”的工作状态变成“上升中”时，触发阳台关联摄像头感知，判断附近是否有人、是否存在异常操作。

**场景 2 — 传感器数值越界**：当人体/亮度/其他数值型属性高于某阈值时，触发对应摄像头感知，补充视觉信息，帮助 Agent 判断现场情况。

**场景 3 — 场景执行后的确认**：当用户通过 Miloco 执行“回家模式”“观影模式”等米家场景后，触发摄像头感知，确认现场是否与预期一致。

### 能力边界

- 支持三类事件源：**设备属性变化**、**设备事件触发** 和 **场景触发**
- 场景触发仅覆盖 Miloco 主动执行场景成功后的回调；米家 App 侧手动执行场景是否有实时推送取决于云端事件能力，当前不把场景重命名、编辑、删除等配置变更当作感知触发
- 是否触发感知由“事件映射 + 属性条件”共同决定；未配置映射时不会触发
- 一个事件可以关联多个摄像头，但只会对本次命中的事件做一次主动感知编排
- 高频重复事件可通过“冷却时间”抑制，避免短时间内重复触发
- 当前 CLI 侧**还没有独立的 `miloco-cli automation ...` 子命令**，命令行调试通过读取 `miloco-cli` 配置后直接访问 API 完成

---

## 研发面

### 架构概览（数据流图）

```text
MiOT MQTT / 业务事件
  → MiotProxy（miot/client.py）
  → AutomationService.handle_trigger（automation/service.py）
      → 事件源匹配
      → 属性条件匹配
      → 冷却时间判定
      → 汇总关联摄像头
      → 发起主动感知（on-demand）
      → 写触发日志 / 快照 / 视频回放
      → 命中规则 / meaningful_events / Agent 回调
```

### 核心模块

**Automation Router**（`backend/miloco/src/miloco/automation/router.py`）

- 提供感知触发配置与调试 API
- 提供事件映射 CRUD
- 提供最近触发日志、手动测试触发、设备属性定义查询

**Automation Service**（`backend/miloco/src/miloco/automation/service.py`）

- 维护事件映射持久化
- 负责匹配事件源、属性条件和冷却时间
- 编排一次感知触发对应的主动感知请求
- 记录日志、快照和视频回放元数据

**MiotProxy 事件分发**（`backend/miloco/src/miloco/miot/client.py`）

- 接收米家设备属性变化事件
- 接收米家设备事件，并把事件参数展平成可筛选的触发参数
- 刷新米家场景配置变更；场景感知触发由 Miloco 执行场景成功后的回调进入
- 标准化为内部 `MiotEventTrigger` 后交给 `AutomationService`

### 属性条件支持

属性筛选支持以下比较运算：

- `等于`
- `不等于`
- `大于`
- `小于`
- `大于等于`
- `小于等于`

其中数值比较仅适用于数值型属性；离散枚举/布尔属性仅适合使用“等于 / 不等于”。

设备事件触发时先选择具体事件，再选择该事件的触发参数。未添加任何参数筛选时，该事件任意参数值都会触发；只配置部分参数时，未配置的参数不参与限制。

### Web 入口

家庭面板左侧导航新增 **“感知触发”** 页面，用于：

- 选择事件源
- 配置关联摄像头
- 选择属性与属性值
- 设置比较运算和冷却时间
- 填写感知提示
- 查看最近触发日志、快照和视频回放

---

## CLI / API 调试用法

当前还没有单独的 `miloco-cli automation` 子命令。命令行调试推荐使用 `miloco-cli` 读取服务地址和 token，再通过 `curl` 调用感知触发相关 API。

### 1. 读取服务地址和 token

```bash
SERVER_URL=$(miloco-cli config get server.url)
TOKEN=$(miloco-cli config get server.token)
```

### 2. 查看感知触发目录

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "$SERVER_URL/api/automation/catalog"
```

返回内容包含：

- 可选事件源设备列表
- 可选场景列表
- 可关联摄像头列表

### 3. 查询某个设备的属性和值定义

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "$SERVER_URL/api/automation/device-spec/<did>"
```

这个接口会返回：

- 属性 key（如 `prop.2.2`）
- 属性中文名
- 可选值列表
- 数值范围

### 4. 创建一条设备属性变化映射

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "source_type": "device",
    "source_id": "<device_did>",
    "source_name_snapshot": "米家智能晾衣机Pro",
    "camera_dids": ["rtsp_01"],
    "enabled": true,
    "query_template": "重点看阳台晾衣机附近是否有人或有异常动作。",
    "event_kinds": ["device_prop"],
    "property_filters": {
      "prop.2.2": { "op": "eq", "value": "1" }
    },
    "cooldown_seconds": 30,
    "notes": "CLI 调试示例"
  }' \
  "$SERVER_URL/api/automation/mappings"
```

### 5. 手动测试一次感知触发

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "source_type": "device",
    "source_id": "<device_did>",
    "source_name": "米家智能晾衣机Pro",
    "event_name": "device_prop",
    "changed_properties": {
      "prop.2.2": "1"
    }
  }' \
  "$SERVER_URL/api/automation/test-trigger"
```

### 6. 查看最近触发日志

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "$SERVER_URL/api/automation/logs?limit=20"
```

重点字段：

- `mapping_ids`：命中了哪些映射
- `perception_started`：是否发起了感知
- `skipped_reason`：未触发时的原因
- `snapshot_paths`：快照路径
- `clip_device_ids` / `clip_kind`：视频回放信息

---

## 如果我要修改感知触发相关功能

| 修改目标                           | 去看哪个文件                                                                      |
| ---------------------------------- | --------------------------------------------------------------------------------- |
| 修改事件映射 API                   | `backend/miloco/src/miloco/automation/router.py`                                  |
| 修改事件匹配 / 冷却时间 / 触发编排 | `backend/miloco/src/miloco/automation/service.py`                                 |
| 修改事件结构定义                   | `backend/miloco/src/miloco/automation/schema.py`                                  |
| 修改米家属性事件接入               | `backend/miloco/src/miloco/miot/client.py`、`backend/miot/src/miot/mips_cloud.py` |
| 修改页面展示与交互                 | `web/src/components/AutomationPage.tsx`                                           |

## 感知触发相关 API 路径

主要入口：

- `GET /api/automation/catalog`
- `GET /api/automation/mappings`
- `POST /api/automation/mappings`
- `PATCH /api/automation/mappings/{mapping_id}`
- `DELETE /api/automation/mappings/{mapping_id}`
- `GET /api/automation/device-spec/{did}`
- `POST /api/automation/test-trigger`
- `GET /api/automation/logs`

完整端点见 `backend/miloco/src/miloco/automation/router.py`。
