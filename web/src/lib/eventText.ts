/**
 * 把事件 text 里规则相关内容的 `[task_id] 规则名` 前缀 strip 为纯中文名。
 *
 * miloco 强制 rule.name 带工程指针 `[task_id]`,task_id 是 Agent 任务模型的
 * 稳定 invariant,跟前端 UI 正交。展示给住户时统一去掉,只留中文。
 * spec B2 (DB.text == webhook) 仍守:DB 里依旧带前缀,这里只在 UI 渲染层 strip。
 *
 * 兼容四种格式:
 * - 当前格式(分类 header): `[感知引擎]规则提醒：` 规则行含 `触发条件：query。触发原因：reason。`（无需 strip）
 * - 旧格式 v3(分类 header): 规则行含 `触发规则：[task_id] 规则名。` → strip [task_id]
 * - 旧格式 v2(统一 header): `[感知引擎] 提醒:` + `检测到：[task_id] 规则名。`
 * - 旧格式 v1(JSON 行): `1. {"rule_id":"...","reason":"..."}` → 反查 rule_names
 */
function stripTaskPrefix(name: string): string {
  return name.replace(/^\[[^\]]+\]\s*/, "");
}

const PERCEPTION_HEADERS = [
  "[感知引擎]规则提醒：",
  "[感知引擎]事件提醒：",
  "[感知引擎]语音提醒：",
];

export function humanizeRulesInText(
  text: string,
  rule_names?: Record<string, string>,
): string {
  if (!text) return "";
  // 按双空行分章节(build_agent_text 用 "\n\n" 拼).
  const sections = text.split(/\n\n(?=\[感知引擎\])/);
  return sections
    .map((section) => {
      // --- 当前格式：分类 header ---
      if (PERCEPTION_HEADERS.some((h) => section.startsWith(h))) {
        return section.replace(
          /触发规则：\[[^\]]+\]\s*/g,
          "触发规则：",
        );
      }

      // --- 旧格式 v2：统一 `[感知引擎] 提醒:` 前缀 ---
      if (section.startsWith("[感知引擎] 提醒:")) {
        return section.replace(
          /检测到：\[[^\]]+\]\s*/g,
          "检测到：",
        );
      }

      // --- 旧格式 v1 兼容：JSON 行 + `命中以下规则` 章节 ---
      const m = section.match(/^\[感知引擎\] (\S+?):\n([\s\S]+)$/);
      if (!m) return section;
      const [, title, body] = m;
      if (title !== "命中以下规则") return section;

      const lines = body.split("\n");
      const rendered = lines
        .map((line) => {
          const lm = line.match(/^(\d+)\.\s*(\{.+\})$/);
          if (!lm) return line;
          try {
            const obj = JSON.parse(lm[2]) as {
              rule_id?: string;
              rule_name?: string;
              reason?: string;
            };
            if (obj.rule_name) {
              const cleaned = { rule_name: stripTaskPrefix(obj.rule_name), reason: obj.reason ?? "" };
              return `${lm[1]}. ${JSON.stringify(cleaned)}`;
            }
            const rawName =
              (obj.rule_id && rule_names?.[obj.rule_id]) || obj.rule_id || "未知规则";
            const newObj = { rule_name: stripTaskPrefix(rawName), reason: obj.reason ?? "" };
            return `${lm[1]}. ${JSON.stringify(newObj)}`;
          } catch {
            return line;
          }
        })
        .join("\n");
      return `[感知引擎] ${title}:\n${rendered}`;
    })
    .join("\n\n");
}
