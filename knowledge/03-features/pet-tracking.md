# 宠物跟踪

## 背景与目标

感知流水线已支持宠物（猫/狗）的**检测**，但尚未支持宠物的**跟踪**和**身份识别**。本文档记录宠物在感知管线各层的流转现状、预留接口和当前限制。

---

## 当前状态总览

| 能力 | 状态 | 说明 |
|------|------|------|
| 宠物检测 | ✅ 已实现 | det_4C.onnx 支持 CLASS_CAT=1, CLASS_DOG=2 |
| 宠物跟踪 (MOT) | ⚠️ 代码预留 | `track_human_only=False` 分支存在但默认关闭 |
| 宠物身份识别 (ReID) | ❌ 未实现 | 无宠物 ReID 模型 |
| 宠物行为描述 | ✅ 部分实现 | Omni VLM prompt 要求描述宠物活动 |
| 宠物 IoT 设备 | ✅ 已实现 | pet-feeder / pet-collar 等 5 类设备完整定义 |

---

## 感知管线流转

```
摄像头视频流
  ↓
[Gate] 视觉/音频变化检测 → 宠物运动可触发 gate ✓
  ↓
[Detector] YOLO 检测 → cat/dog 作为 Detection 对象产出 ✓
  ↓
[Sort/DeepSort] 跟踪 → 默认 track_human_only=True，pet 不形成 track ✗
  ↓                     last_detections 保留所有类别（含 pet）✓
[_build_response] → 硬编码 HUMAN_BODY 类型 ✗
  ↓
[IdentityEngine] → 只处理 human track ✗
  ↓
[Omni] VLM → caption/suggestions 中可描述宠物 ✓（通用描述级）
```

---

## 检测模型

**文件**：`backend/miloco/src/miloco/perception/engine/identity/tracker/detector.py`

**模型**：`det_4C.onnx`（YOLOv8 架构，5 类输出）

| class_id | 类别 | 说明 |
|----------|------|------|
| 0 | human | 人体 |
| 1 | cat | 猫 |
| 2 | dog | 狗 |
| 3 | head | 人头 |
| 4 | face | 人脸 |

**便捷方法**：
- `Detector.detect_pets(image)` — 返回 CLASS_CAT + CLASS_DOG 的检测结果
- `Detector.detect_humans(image)` — 返回 CLASS_HUMAN
- `Detector.detect_faces(image)` — 返回 CLASS_FACE

---

## track_human_only 配置

**文件**：`backend/miloco/src/miloco/perception/engine/identity/sort.py`

```python
@dataclass
class SortConfig:
    track_human_only: bool = True  # 只对 HUMAN 类形成 track
```

当 `track_human_only=False` 时，跟踪器接受 CLASS_HUMAN + CLASS_CAT + CLASS_DOG 三类检测结果形成 track。

**配置路径**：
- `default_config.yaml` → `sort.track_human_only: true`
- `config.py` → `SortConfigDC.track_human_only`、`DeepSortConfigDC.track_human_only`

**DeepSortTracker 额外防护**：即使内部 MOT 产生了 pet track，`update()` 和 `update_with_detections()` 末尾都会 post-filter 只保留 CLASS_HUMAN：

```python
self._mot.tracks = [t for t in self._mot.tracks if t.class_id == self._Detection.CLASS_HUMAN]
```

---

## 预留接口

| 接口 | 位置 | 说明 |
|------|------|------|
| `track_human_only=False` 分支 | `sort.py` L347-353 | SORT 跟踪 HUMAN+CAT+DOG |
| `ObjectType.PET` | `types.py` | 宠物对象类型枚举 |
| `BoxType.PET_BODY` | `types.py` | 宠物身体框类型 |
| `detect_pets()` | `detector.py` | 检测猫和狗 |
| `_convert_type("pet")` | `tracking_service.py` | 解析为 ObjectType.PET |
| `last_detections` | `sort.py` / `deep_sort.py` | 始终保留所有类别检测结果 |

---

## 当前限制

1. **无宠物 ReID 模型**：无法区分"这只猫"和"那只猫"。需要训练专门的 pet ReID 模型。
2. **跟踪默认关闭**：`track_human_only=True` 使得 pet 不形成 track，无法建立运动轨迹。
3. **_build_response 硬编码**：即使 pet track 存在，`_build_response()` 也会将其标记为 `HUMAN_BODY`。
4. **IdentityEngine 不支持 pet**：整个状态机（pending → confirmed → unknown）只面向人类设计。
5. **无宠物面部检测**：没有猫脸/狗脸检测模型。
6. **宠物不在身份库**：`subject_id` 留空，无法注册和管理宠物身份。

---

## 启用宠物跟踪的路径（参考）

若未来要启用 pet tracking，需要改动的关键点：

1. 将 `track_human_only` 默认值改为 `False`（或新增 `track_pets` 开关）
2. 修改 `_build_response()` 根据 `class_id` 映射正确的 `ObjectType`
3. （可选）引入 pet ReID 模型支持跨帧身份关联
4. （可选）扩展 `IdentityEngine` 支持 PET 类型的状态管理
