// 规则就地编辑的草稿层：编辑框文本 ⇄ PATCH 载荷。
//
// 抽成独立模块而不是留在 TasksPage 里，是为了能直接做纯函数测试 ——
// 这里承载了本页几个有分支的决策（前缀识别、两侧 trim 比较、清空 = 放弃修改、
// 多条文案按行拆合），而后端只校验最终的名字串，验不出「前缀被悄悄丢掉」。
import type { TaskRuleBrief, TaskRuleDescSlot, TaskRulePatch } from "./types";

// 一条规则的可编辑字段草稿。desc 槽位一律存字符串（多条文案按行拆合），
// 免得编辑框和数组结构来回转换。
export interface RuleDraft {
  // 只存规则名去掉 `[<task_id>] ` 前缀后的后半段——前缀由 UI 固定拼回。
  nameSuffix: string;
  query: string;
  action_descriptions: string;
  on_enter_desc: string;
  on_exit_desc: string;
  on_target_desc: string;
}

// skill 约定的规则名前缀。规则名不是这个格式时（人工建的老规则）返回空串，
// 此时整条名字都交给住户改，不硬套前缀。
export function ruleNamePrefix(taskId: string, name: string): string {
  const prefix = `[${taskId}] `;
  return name.startsWith(prefix) ? prefix : "";
}

// 槽位原值 → 编辑框文本。action_descriptions 是数组，一行一条。
export function slotText(rule: TaskRuleBrief, slot: TaskRuleDescSlot): string {
  if (slot === "action_descriptions") return rule.actionDescriptions.join("\n");
  if (slot === "on_enter_desc") return rule.onEnterDesc ?? "";
  if (slot === "on_exit_desc") return rule.onExitDesc ?? "";
  return rule.onTargetDesc ?? "";
}

export function makeRuleDraft(taskId: string, rule: TaskRuleBrief): RuleDraft {
  return {
    nameSuffix: rule.name.slice(ruleNamePrefix(taskId, rule.name).length),
    query: rule.query,
    action_descriptions: slotText(rule, "action_descriptions"),
    on_enter_desc: slotText(rule, "on_enter_desc"),
    on_exit_desc: slotText(rule, "on_exit_desc"),
    on_target_desc: slotText(rule, "on_target_desc"),
  };
}

// 草稿 → PATCH 载荷：只带真改过且非空的字段。清空视作放弃这一处修改——把唯一
// 一个已配置的槽位清空反而会被 backend 的「至少配一个」校验退回。
export function ruleDiff(
  taskId: string,
  rule: TaskRuleBrief,
  draft: RuleDraft,
): TaskRulePatch | null {
  const patch: TaskRulePatch = {};
  // 两侧都 trim 再比：存量值带首尾空格时，住户只改了 A 字段，B 字段不该被「顺手」
  // 下发一次纯规范化 PATCH（那会白白吃一次校验 + 热重载）。
  // 名字这一路要「剥前缀 + trim」两侧同口径：前缀含一个空格, `]` 后多打的空格会被
  // 算进后半段, 而 rule.name.trim() 动不了那个位置 —— 否则住户一个字没改点保存,
  // 这条规则也会被静默改名 + 走一次热重载; 老规则名不合规时还会被新校验挡在保存外。
  const prefix = ruleNamePrefix(taskId, rule.name);
  const name = prefix + draft.nameSuffix.trim();
  const prevName = prefix + rule.name.slice(prefix.length).trim();
  if (draft.nameSuffix.trim() && name !== prevName) patch.name = name;
  const query = draft.query.trim();
  if (query && query !== rule.query.trim()) patch.query = query;
  for (const slot of rule.editableDescSlots) {
    const next = draft[slot].trim();
    if (!next || next === slotText(rule, slot).trim()) continue;
    if (slot === "action_descriptions") {
      patch.actionDescriptions = next
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
    } else if (slot === "on_enter_desc") patch.onEnterDesc = next;
    else if (slot === "on_exit_desc") patch.onExitDesc = next;
    else patch.onTargetDesc = next;
  }
  return Object.keys(patch).length > 0 ? patch : null;
}
