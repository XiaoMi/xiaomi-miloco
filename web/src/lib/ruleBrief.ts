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

export interface RuleGroups {
  /** 进入（enter）与会话（session）。session 独占一个 task，两者不会同时出现。 */
  entry: TaskRuleBrief[];
  exit: TaskRuleBrief[];
  guard: TaskRuleBrief[];
}

/**
 * 按方向把规则分块。
 *
 * **三组都是数组**：后端只强制 session 独占，对 enter / exit 的条数不设上限，
 * 运行期把同方向的边沿 OR 起来。只取第一条会让其余规则的触发条件与规则名从
 * 界面上静默消失，而它们仍在生效——住户看到的规则集合与后端装载的不一致。
 */
export function groupRuleBriefs(rules: TaskRuleBrief[]): RuleGroups {
  return {
    entry: rules.filter(
      (r) => r.direction === "enter" || r.direction === "session",
    ),
    exit: rules.filter((r) => r.direction === "exit"),
    guard: rules.filter((r) => r.direction === "guard"),
  };
}
