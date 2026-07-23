# 宠物 prompt 强制注入 · 感知回归测试分支（`test/pet-prompt-force`）

> ⚠️ **本分支仅供回归测试，不并入 main。** 基于 `feat/pet-recognition` 的合并态（含最新 main）。

## 这分支干什么

把「为宠物新增的感知 prompt」**强制注入**，用来回归测量它对**其他感知输出**（caption / 人物 identities / matched_rules / suggestions）有没有负面影响。默认还**带内嵌样本**（合成宠物名单 + 多姿态参考图），让你零配置就跑在**接近真实生产**的形态下。

正常生产是「家庭档案有 `## 宠物` 段 **且** `pet_recognition` 开启」才注入；本分支把唯一闸口 `prompt_builder._has_pets_for_scene()` 覆盖为**默认 True**，并在两处「有样本就合成注入」。

## 三档（同事零配置切换，改 backend 进程的环境变量即可）

| 档位 | env | 注入内容 |
|---|---|---|
| **完整带样本**（默认） | 都不设 | `pet_identities` 字段 + `PET_NAMING_SPEC` 命名纪律 + **内嵌样本合成的「## 宠物」名单** + **`<pets>` 多姿态参考图** |
| **纯文字**（消融） | `MILOCO_FORCE_PET_SAMPLES=0` | 只有 `pet_identities` 字段 + `PET_NAMING_SPEC` 文字（**无名单、无图**）——测"纯额外指令"的开销 |
| **关闭基线** | `MILOCO_FORCE_PET_PROMPT=0` | 退回生产 gating（默认没开宠物 → 什么都不注入）——A/B 的对照组 |

三档跑在**同一份代码/同一个 build** 上，唯一变量就是这段 pet prompt，对比最干净。启动日志会打一条 `【test/pet-prompt-force】pet prompt FORCED ON …` warning，看到即确认强制生效。

## 怎么用（开箱即用）

1. 部署本分支（正常 build / install 流程；内嵌样本随 wheel 一起装，**无需额外准备数据**）。
2. 默认即「完整带样本」——每个 video 感知轮都带上宠物名单 + 参考图。
3. 跑你的感知回归用例，用上表切三档对比感知输出质量。

## 内嵌的样本

`backend/miloco/src/miloco/perception/engine/omni/_pet_force_fixture/`（随分支提交、随 wheel 安装）：
- `manifest.json` —— 3 只：**饼干（猫·三花）/ 芝麻（猫·奶牛）/ 糯米（狗·哈士奇）**，含 name / species / appearance。
- `猫/*_sheet.jpg`、`狗/*_sheet.jpg` —— 每只一张多姿态横向拼图（`<pets>` 参考图，短边≈320，源自宠物注册评测缓存 `.wsh_cc/pet_eval/enroll_cache_v1`）。

**想加更多 / 换样本**：往 `_pet_force_fixture/` 丢图、在 `manifest.json` 加条目（`name/species/appearance/sheet`）即可；`max_pet_refs` 默认取前 3 只。

## 与真实宠物的关系（真实优先）

- 若你的测试环境**注册了真实宠物**（真实 `PetLibrary` 有参考图 / 真实档案有 `## 宠物` 段）→ **用真实的**，合成样本不叠加（`<pets>` 与名单都以真实为准）。
- 想在有真实宠物时也测「纯文字」或「关闭」→ 用上表 env。

## 实现（就这几处，仅本分支）

- `perception/engine/omni/prompt_builder.py::_has_pets_for_scene()` —— 默认 `return True`；`MILOCO_FORCE_PET_PROMPT=0` 退生产逻辑；首次注入打 warning。
- `perception/engine/omni/_pet_force_fixture.py`（新）—— `pet_samples_on()` 三档判定 + 读内嵌样本合成 `## 宠物` 名单 / `<pets>` 图。
- `perception/engine/omni/home_profile_loader.py::get_home_profile_prefix()` —— 样本档时把合成名单追加进档案（真实档案已有 `## 宠物` 则不叠加）。
- `perception/engine/omni/prompt_builder.py` 参考图注入点 —— 真实 `PetLibrary` 有货用真实，空且样本档 → 用内嵌 `<pets>`。
- `backend/miloco/tests/conftest.py` —— 单测默认 `MILOCO_FORCE_PET_PROMPT=0`（校验生产 gating；运行时 backend 无此 conftest 仍强制注入）。

> 说明：合成注入 ≠ 生产 `PetLibrary` 全链路（sheet 是预拼好的、直接 base64，不再现场 hstack；名单是合成非 commit 渲染）——对"测 prompt 对感知的影响"已足够忠实（本就是测试分支）。
