# 身份识别

## 背景与目标

感知流水线能描述"画面中有人"，但无法回答"是谁"。身份识别模块解决这个问题：通过 face / body_appearance 两类视觉特征，把摄像头看到的人与家庭成员档案对应起来。

准确的身份识别让整个系统从"有人触发"升级为"谁触发"——规则可以精确到人，场景描述更自然，家庭记忆能正确归属。

---

## 产品面

### 能做什么

- **家庭成员管理**：增删改查成员（姓名/角色），每个成员持有独立的样本库
- **主动注册（两条路径）**：用户上传照片/视频直接进入 tier_a；或从系统自动积累的陌生人池（tier_u）中选取 crop 升级
- **实时识别**：随感知流水线运行，track 稳定后自动识别并写回 person_id，caption 中出现成员名
- **陌生人分配**：未能识别的 track 自动分配唯一编号，同一个人跨不同 track 用 ReID embedding 聚类合并
- **成员合并 / 拆分**：把误分裂成多个的同一人合并为一个、或把误合并的样本拆分成新成员，DB 行与磁盘样本目录同步增删

### 典型场景

**场景 1 — 新成员注册**：家里来了新保姆，需要让系统认识她。用户通过 Agent 触发 `miloco-miot-identity-register` Skill，上传视频或照片。系统展示候选 crop 拼图等待用户确认，确认后写入 tier_a。之后感知流水线识别到她时，caption 会用"保姆"而非"陌生人 #3"称呼。

**场景 2 — 从陌生人池升级**：系统在近期感知中多次看到一位陌生人，自动积累了多张高质量 crop 并归入陌生人池。用户通过 Skill 取出候选拼图，认出是经常来访的朋友，确认后从陌生人池选取 crop 写入 tier_a，跳过手动上传照片。

### 能力边界

- 识别精度取决于样本质量（图像清晰度、角度多样性）和模型能力
- 实时识别依赖感知流水线运行；流水线未启动时识别不工作，但成员管理 API 仍可用
- 陌生人池（tier_u）全内存，重启即清
- 每个摄像头持有独立的 IdentityEngine 实例，身份库全局共享；不支持"两台摄像头实时合并同一 track"
- 成员名重复时创建/更新抛 `ConflictException`

---

## 研发面

### 架构概览（数据流图）

#### 成员 CRUD 调用链

```
CLI / Agent（miloco-miot-identity Skill）
  → /api/identity/persons（person/router.py）
  → PersonService（person/service.py）
  → PersonRepo（database/person_repo.py）
  → miloco.db（person 表）
```

成员删除时，删除端点（`person/router.py` 的 delete 路由）编排两路级联：`PersonService.delete_person` 删 DB 行后，再经 `IdentityLibrary` 删除文件系统样本目录、经 `HomeProfileService.remove_subject` 清理家庭档案中绑定该成员的条目。

#### 实时识别链路（person_id 生成与消费）

```
TrackingService（DeepSORT 跟踪）
  → active tracks（track_id + bbox + ReID embedding）
  ↓
IdentityEngine（perception/engine/identity/engine.py）
  ├─ 未识别 track → 派发识别请求（Fused 路径）
  │    → gallery composite 注入 Omni fused 主调用
  │    → Omni 返回 identity_assignments → 写回 IdentityEngine 状态机
  ├─ confirmed track → 周期性重审
  ├─ unknown track → 累积 crop 到 TierUPool（陌生人池）
  └─ 返回 {track_id → person_id} 映射
  ↓
Omni 层（person_id 注入 prompt → caption 中出现成员名）
```

#### 陌生人→成员注册流程

```
IdentityEngine（识别失败 → unknown）
  → TierUPool（ReID embedding 聚类，合并同一人不同 track 的 crop）
  ↓
miloco-miot-identity-register Skill
  → 取候选拼图 → 用户确认选号 → from-cluster 写 tier_a
  ↓
IdentityLibrary 写入样本文件
  → $MILOCO_HOME/data/identity_lib/persons/<id>/tier_a/
  ↓
下次感知时 IdentityEngine 检测 tier_a 指纹变化 → 重新识别
```

### 核心模块

**IdentityLibrary**（`perception/engine/identity/library.py`）

磁盘身份库的读写封装，负责 tier_a / tier_c 样本的加载、写入、FIFO 管理。它是无状态的文件系统封装：进程内 per-camera IdentityEngine 由工厂一次性构造后共享同一份实例，而成员 CRUD / 注册流程（`person/router.py`）另建自己的实例；两端都经同一个路径单一事实源 `resolve_library_root`（`perception/engine/identity/config_loader.py`）解析根目录，保证多端读写落在同一目录。样本库默认在 `$MILOCO_HOME/data/identity_lib/persons/`。

**TierUPool**（`perception/engine/identity/tier_u.py`）

陌生人池，全内存、重启即清。内部用 ReID embedding 聚类，把同一个人不同 track 的 crop 归到同一 cluster。embedding 从跟踪侧 Track 的 ReID 特征快照获取，零额外推理。

**RegistrationSessionManager**（`perception/engine/identity/registration_session.py`）

管理注册会话生命周期（创建、pending 累积、commit、rollback）。进程内单例，由 `Manager` 懒加载持有。提供两种入库入口以匹配不同的用户确认层级：交互式两步（先预览、再按用户勾选写盘，Web/Agent 看拼图确认走此路），与一步式（直接提交预选结果，供 from-cluster / from-pool / CLI 等"用户已在上游选定候选、无需逐张勾选"的场景）。两条路径的用户确认发生在不同层级：照片/视频上传靠拼图逐张勾选，陌生人池升级靠先在候选号码图里选定 cluster。

**IdentityEngine**（`perception/engine/identity/engine.py`）

per-camera 识别管线总编排，维护每个 track 的识别状态机（none / pending / confirmed / unknown / no_person 五态）。决定何时派发识别请求，回流结果后更新 person_id 映射，以及何时将高置信结果异步写入 tier_c。每窗口比对 tier_a 指纹快照，发现变化时将所有 track 推回 pending 强制重判。

### 关键设计决策

#### tier_a / tier_c / tier_u 三层样本设计意图

- **tier_a**（`persons/<id>/tier_a/`）：用户主动登记，永久保留，代表最可靠的参照样本
- **tier_c**（`persons/<id>/tier_c/`）：系统在线推理中自动积累，FIFO 滚动更新。让身份参照跟上人物外观自然变化（换衣、不同光照），避免只靠注册时的老照片导致长期识别漂移。tier_c 写盘有严格门控条件，且在独立异步任务中执行，不阻塞每窗推理
- **tier_u**（TierUPool）：识别失败的未知 track 的临时 crop 缓冲，全内存、重启即清。为主动注册提供候选素材，用户可从中选取系统已积累的近期 crop，无需手动上传照片

**IdentityEngine 状态机**：每个 track 的识别结果需经多次 Omni 识别一致后才晋升为已提交状态（confirmed 或 unknown），避免单帧误识。Omni 连续多次判定框内确无人时，track 落定 no_person 抑制 VLM 的"无人幻觉"，且只对未确认成员生效、不翻转已确认的真本人。tier_a 指纹改变时强制所有 track 重新走识别流程，确保新增/更新参照样本立即生效；tier_c 变化不触发重判（避免自喂环）。

### 如果我要修改身份识别相关功能

| 修改目标                            | 去看哪个文件                                                |
| ----------------------------------- | ----------------------------------------------------------- |
| 修改识别状态机逻辑（何时触发/确认） | `perception/engine/identity/engine.py`（IdentityEngine）    |
| 修改陌生人池聚类逻辑                | `perception/engine/identity/tier_u.py`（TierUPool）         |
| 修改样本库读写逻辑                  | `perception/engine/identity/library.py`（IdentityLibrary）  |
| 修改注册流程（预览/commit 逻辑）    | `perception/engine/identity/registration_session.py`        |
| 修改成员 CRUD API                   | `person/router.py`、`person/service.py`                     |
| 修改成员合并 / 拆分逻辑             | `perception/engine/identity/library.py`、`person/router.py` |

### 身份识别相关 API 路径

成员管理：`/api/identity/persons` 前缀（CRUD）；上传注册：`/api/identity/register/preview`（预览）→ `/api/identity/register/commit`（确认写入）；陌生人池注册：`/api/identity/pool/fetch`（取候选）→ `/api/identity/register/from-cluster`（确认写入）。完整端点见 `person/router.py`。

### 与其他模块的关系

**上游**：身份识别嵌入在 Identity 层，每次感知周期由 Identity 编排器（`perception/engine/identity/identity.py`）调用，`{track_id → person_id}` 映射写回 `IdentityPacket` 后交给 Omni 层。

**下游**：`person_id` 注入 Omni prompt，VLM 在 caption 中以成员名代替匿名编号。成员增改与注册落库后级联 `HomeProfileService.commit()` 重渲染家庭档案 md（保证新成员 / 改名及时进档案、改名条目自动纠偏），成员删除时改走 `HomeProfileService.remove_subject` 清理绑定该成员的条目。

**共享**：per-camera IdentityEngine 共享工厂构造的同一份 IdentityLibrary 实例；成员 CRUD / 注册流程另建自己的实例。因 IdentityLibrary 无状态、且两端都经 `resolve_library_root` 解析同一根目录，读写必然落在同一份样本库。
