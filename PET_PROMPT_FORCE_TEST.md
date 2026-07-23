# 宠物 prompt 强制注入 · 感知回归测试分支（`test/pet-prompt-force`）

> ⚠️ **本分支仅供回归测试，不并入 main。** 基于 `feat/pet-recognition` 的合并态（含最新 main）。

## 这分支干了什么

把感知 omni prompt 里「为宠物新增的 prompt」**强制无条件注入**，用来回归测量它对**其他感知输出**（caption / 人物 identities / matched_rules / suggestions）有没有负面影响。

正常生产是「家庭档案有 `## 宠物` 段 **且** `pet_recognition` 开启」才注入；本分支把唯一闸口 `prompt_builder._has_pets_for_scene()` 覆盖为**默认 True**。

强制注入的三样（每个 video 感知轮都带上）：
- **`pet_identities` 结构化字段**（进 schema + 字段说明，含弃权纪律）
- **`PET_NAMING_SPEC` 命名派生纪律**（约束 caption / suggestions / matched_rules 怎么称呼宠物）
- **宠物参考图门**（`if has_pets:` 开启，实际是否有图见下「数据依赖」）

## 怎么用（开箱即用）

1. 部署本分支（正常 build / install 流程）。**无需设任何 env / 配置**，pet prompt 即强制注入。
2. 启动日志会打一条 `【test/pet-prompt-force】pet prompt FORCED ON …` warning——看到它即确认强制生效。
3. 跑你的感知回归用例，对比感知输出质量。

### A/B 对比（同一个 build 取「关闭基线」）

同一部署上，给 backend 进程设环境变量即可关掉 pet prompt 拿基线：

```
MILOCO_FORCE_PET_PROMPT=0     # → 退回生产 gating（家庭档案有 ## 宠物 段 且 pet_recognition 开才注入；
                              #   默认二者皆无 → 等价 pet prompt 关，即 main 行为）——关闭基线
（不设 / 设别的值）            # → 强制注入（默认，本分支行为）
```

这样「开 vs 关」跑在**同一份代码**上，唯一变量就是这段 pet prompt，对比最干净。
（注：`=0` 是「退回生产逻辑」而非「硬关」——生产默认没开宠物、没注册宠物，故等价于关；若你的测试环境**开了 `pet_recognition` 且注册了宠物**，`=0` 会按生产逻辑注入，此时取基线请改用未合并本改动的分支/main。）

## 数据依赖（想测「完整生产形态」时注意）

`pet_identities` 字段 + `PET_NAMING_SPEC` 文字是**无条件注入**的；但它们引用的两样东西是**数据依赖**，不是被这个开关强制的：

| 内容 | 来源 | 无注册宠物时 |
|---|---|---|
| `## 宠物` 名单 roster | 家庭档案 profile.md（由 home_profile 渲染，受 `pet_recognition` 门） | prompt 里有"对照名单"指令但**无名单** |
| 宠物多姿态参考图 | `PetLibrary`（`build_pet_reference_content` 读盘） | **无图** |

- 只想测「额外 prompt 指令/字段」的开销影响 → 直接用，无需准备数据。
- 想测**完整生产形态**（含真实名单 + 参考图）→ 先**注册 1~2 只测试宠物**（web 或 CLI `pet` 命令）并开启 `pet_recognition`，roster 与参考图就会自然进 prompt。

## 建议对比的观测点

强制开 vs 关（`MILOCO_FORCE_PET_PROMPT=0`），同一批画面/视频，看：
- **caption**：是否变短/漏描述/被宠物指令带偏。
- **人物 identities**：人脸/track 判定是否受影响（漏判、误判 no_person）。
- **matched_rules / suggestions**：规则命中与建议是否被宠物命名纪律干扰。
- 延迟 / token：pet_identities 字段 + 纪律文字（+ 参考图，若注册了宠物）带来的 prompt 体积与耗时增量。

## 实现（就一处）

- `backend/miloco/src/miloco/perception/engine/omni/prompt_builder.py::_has_pets_for_scene()` —— 默认 `return True`，`MILOCO_FORCE_PET_PROMPT=0` 时 `return False`，首次注入打 warning。
- 唯一闸口：三处注入（字段/纪律/参考图门）全由它派生的 `SceneDescriptor.has_pets` 驱动，改这一处即全覆盖。
