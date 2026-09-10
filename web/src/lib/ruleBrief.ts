import type { TaskRuleBrief } from "@/lib/types";

/**
 * 这条规则的触发条件能不能由住户在抽屉里改。
 *
 * 只有 omni 规则可以：它的条件就是住户写的那句自然语言。其余源的条件是服务端按
 * 谓词渲染出来的一句描述，后端会拒绝对它的 PATCH —— 给一个能编辑、提交必失败的
 * 框比不给更糟，同一屉是串行批量保存，会「部分成功」。
 */
export function ruleConditionIsEditable(rule: TaskRuleBrief): boolean {
  return rule.sourceType === "omni";
}
