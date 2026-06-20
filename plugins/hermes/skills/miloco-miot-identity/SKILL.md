---
name: miloco-miot-identity
description: 家庭成员档案 CRUD —— 创建/列出/重命名/删除成员。**身份样本注册(给某人录脸/录身形)走 miloco-miot-identity-register,本 skill 不做。**
metadata:
  author: miloco
  version: "2.1"
  date: "2026-05-20"
  openclaw:
    requires:
      bins: ["miloco-cli"]
---

# miloco-miot-identity

家庭成员档案 CRUD —— 只管 person DB 行的增删查改,**不**管样本注册(那是
[miloco-miot-identity-register](../miloco-miot-identity-register/SKILL.md) 的职责)。

## 变更说明

**v2.1(2026-05-20)** — 加 "查看身份库情况" 工作流(2e),展示**真实样本数**而不是已废弃的 face/voice 二元字段;API 已删 `face_enrolled` / `voice_enrolled` 字段。

**v2.0(2026-05-18)** — v1 内置的"人脸/声纹注册"工作流整段移除:
- 老的 `miloco-cli person biometric add` 端点已废弃,**主流程不再用人脸注册** —— 当前 identity pipeline 走 body ReID(陌生人池 + DeepSORT)而不是 face recognition。
- 任何"录脸 / 录声纹 / 注册 XXX 的样本 / 录入身份特征"语境**一律转到 [miloco-miot-identity-register](../miloco-miot-identity-register/SKILL.md)**。

## 何时激活

只在用户**单纯增删查改 person DB 行**且**不涉及样本/识别**时激活:

| 意图 | 用户说了类似… | → 路由 |
|---|---|---|
| **add** | "添加我爸叫张三" "新增一个家庭成员" | 本 skill |
| **list** | "家里有哪些人" "查看家庭成员" "成员列表" | 本 skill |
| **status** | "现在身份库是什么样的" "查一下身份库情况" "现在录入了谁,各多少样本" | 本 skill(2e) |
| **rename** | "把张三的家庭角色改成爸爸" | 本 skill |
| **delete** | "删除张三" "把张三从成员里删掉" | 本 skill |
| **登记样本 / 录入特征 / 录脸 / 注册某人** | "给王阿姨建个档案" "登记一下张三" "这是 XXX 登记" "推送响应 这是谁" | **转 miloco-miot-identity-register** |

**判断规则**:
- 涉及"样本 / 照片 / 视频 / 录入 / 注册"(任何能让系统**学到这人长什么样**的动作)→ **转 register skill**
- 只是改 DB 行(纯 name/role)→ 本 skill

## 工作流

### 第一步 · 解析意图

| 字段 | 说明 | 示例 |
|---|---|---|
| `操作` | add / list / rename / delete | "添加" / "查看" / "改名" / "删除" |
| `姓名` | 真实姓名 | "张三" |
| `家庭角色` | 家庭关系(爸爸/妈妈) | "爸爸" "妈妈" |

### 第二步 · 执行

#### 2a · 创建成员

```bash
# 真名必填;抽到家庭角色才传 --role,抽不到就省略(role 留空即可,不影响 name)
miloco-cli person add --name "<姓名>" [--role "<家庭角色>"]
```

> 注意:**只创建 DB 行,不录任何样本**。提示用户:
> "张三档案已建。要给他录身形样本(后续摄像头才能识别他),回复'给张三登记样本'或上传他的照片"
> → 走 register skill

#### 2b · 列成员(只要名单)

```bash
miloco-cli identity member list --pretty
# 或等价的:miloco-cli person list --pretty (都打到 GET /api/identity/persons)
```

返回 JSON 只含 `id / name / role / created_at / updated_at`,**不再有** `face_enrolled` /
`voice_enrolled` 字段。格式化成简洁列表:

```
家庭成员(3 人):
- 张三(爸爸)
- 李四(妈妈)
- 王五(我)
```

⚠️ **不要展示"人脸"或"声纹"列**——这两个维度已不在数据模型里。要看每人**有多少样本**走 2e。

#### 2c · 改名 / 改角色

```bash
miloco-cli person update <person_id> [--name "<新姓名>"] [--role "<新家庭角色>"]
```

只更新 DB 行的 name / role,**不动 identity_lib 下的样本目录**(目录名 = person_id,跟显示名解耦)。

#### 2d · 删除成员

```bash
miloco-cli identity member delete <person_id>
# 或等价:miloco-cli person delete <person_id>
```

⚠️ 二次确认:
> "确认删除张三(爸爸)?**该人的所有 body 样本会被级联清除,撤不回**。"

确认后执行,回复"张三已删除"。

#### 2e · 查看身份库情况

用户问"现在身份库什么样" / "录入了谁,各多少样本" / "查身份库情况" 时:

```bash
# 第 1 步:拿名单
miloco-cli identity member list --pretty
# 第 2 步:对名单里**每个** person,拿真实样本数(body + face 都看)
miloco-cli identity sample show --person <person_id> --tier a --pretty
```

`sample show` 返回的 `body_count` / `face_count` 是**磁盘真实文件数**(不依赖 with-face)。

输出范式(用真实数字,**不要**用"已录入/未录入"二元状态;**不要**显示"声纹"列):

```
家庭成员(2 人):

| 姓名 | 家庭角色 | 身体样本 | 人脸样本 | 创建时间 |
|------|---------|---------|---------|---------|
| 张三 | 爸爸     | 5 张    | 3 张    | 2026-05-18 |
| 李四 | 妈妈     | 2 张    | 0 张    | 2026-05-18 |

陌生人池(可选,问到时附加):X 个聚类,占用 Y MB
  (走 `miloco-cli identity pool status --pretty`)
```

边界:
- `body_count=0` 显示 "0 张"(不是"未录入") — 提示用户走 register skill 补样本
- `face_count=0` 显示 "0 张" — 是正常的,不强制需要 face

### 第三步 · 回复

- 创建成功 → 提示"档案已建,如要识别需登记样本(转 register skill)"
- list → 简洁列表,只有姓名/家庭角色
- status(2e) → 表格含真实 body_count/face_count(磁盘文件数),**不要**渲染"人脸/声纹"二元列
- delete → 二次确认 + 执行

## 关键规则

1. **本 skill 不调 `person biometric add`** —— 该命令已废弃。
2. **本 skill 不接 `systemEvent: concern` / 陌生人推送** —— 这类消息**整体转 register skill** 处理(它的"路径 B · 推送响应"工作流)。
3. **创建后必须主动引导用户去登记样本** —— 单纯 `person add` 出来的成员摄像头识别不到。
4. **删除是级联硬删** —— person DB 行 + identity_lib/persons/<id>/ 整目录(含 tier_a/tier_c/info)同时清。

## 异常

| 异常 | 处理 | 回复 |
|---|---|---|
| 姓名未给 | 追问 | "请问这位成员叫什么?" |
| 用户问"录脸/录样本" | **转路 register skill** | "登记样本走另一个流程,我去叫 register 接手" |
| 用户问"声纹录入情况" | 告知这期没有 | "这期没有声纹存储,只有人脸 crop + 身体样本" |
| 删除人员 | 二次确认 | "确认删除{姓名}?将清除其所有样本" |

## 示例

### 添加 — "添加我爸叫张三"

```
解析:add,姓名="张三",家庭角色="爸爸"
执行:miloco-cli person add --name "张三" --role "爸爸"
     → {"code":0,"data":{"person_id":"..."}}
回复:"张三档案已建。要让摄像头能识别他,接下来要给他录一些样本——
      回复'给张三登记样本'或者直接上传一张他的照片,我来安排录入。"
```

### 列表 — "家里有哪些人"

```
执行:miloco-cli identity member list --pretty
回复:
  家庭成员(3 人):
  - 张三(爸爸)
  - 李四(妈妈)
  - 王五(我)
```

### 查看身份库情况 — "现在身份库是什么样的"

```
执行:
  miloco-cli identity member list --pretty
  → [{id: p1, name: 张三, role: 爸爸, ...}, {id: p2, name: 李四, ...}]
  对每个 person 跑:
  miloco-cli identity sample show --person p1 --tier a --pretty
  → {body_count: 5, face_count: 3, ...}
  miloco-cli identity sample show --person p2 --tier a --pretty
  → {body_count: 2, face_count: 0, ...}
回复:
  家庭成员(2 人):

  | 姓名 | 家庭角色 | 身体样本 | 人脸样本 | 创建时间 |
  |------|---------|---------|---------|---------|
  | 张三 | 爸爸     | 5 张    | 3 张    | 2026-05-18 |
  | 李四 | 妈妈     | 2 张    | 0 张    | 2026-05-18 |
```

### 删除 — "删除张三"

```
解析:delete,target="张三"→ person_id=...
确认:"确认删除张三(爸爸)?该人的所有 body 样本会被级联清除,撤不回。"
用户确认
执行:miloco-cli identity member delete <person_id>
回复:"张三已删除"
```

### "摄像头下是张三,登记一下"

```
判断:涉及"登记样本",转 miloco-miot-identity-register
回复:(切换到 register skill,跑它的工作流;agent 框架自动接力)
```

## 边界

- ❌ 不录样本(face / body / voice)→ miloco-miot-identity-register
- ❌ 不接陌生人推送 → miloco-miot-identity-register(路径 B)
- ❌ 不控制设备 → miloco-devices
- ❌ 不做实时环境感知 → miloco-perception
- ⚠️ 删除成员级联清样本,不可撤
- ✅ 只管 person DB 行 CRUD,纯轻量操作
