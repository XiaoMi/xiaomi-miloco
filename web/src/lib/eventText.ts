/**
 * 清理事件 text 里规则相关内容残留的 `[task_id]` 工程指针前缀。
 *
 * 当前格式（任务 / 规则）后端已构造成住户可读形态、DB 里就不带 `[task_id]`，
 * 前端原样渲染、无需处理。本函数只清理**同 header 的历史旧行**（触发规则 / 触发条件）
 * 以及旧 v1/v2 格式里残留的 `[task_id]` 前缀——那些旧数据 DB 里仍带前缀。
 *
 * 兼容格式:
 * - 当前格式(分类 header): `[感知引擎]规则提醒：` 块含 `任务：<任务描述>` +
 *   `规则：[规则短名] query` —— 后端已构造成住户可读形态（无 [task_id]），无需 strip。
 *   下面几条 strip 只清理**同 header 的旧行**里残留的 [task_id] 前缀（历史数据兼容）。
 * - 旧格式 v3(分类 header): 规则行含 `触发规则：[task_id] 规则名。` → strip [task_id]
 * - 旧数据(query 空时): `触发条件：[task_id] 规则名` → strip [task_id]（ascii 前缀，放过中文方括号 query）
 * - 旧格式 v2(统一 header): `[感知引擎] 提醒:` + `检测到：[task_id] 规则名。`
 * - 旧格式 v1(JSON 行): `1. {"rule_id":"...","reason":"..."}` → 反查 rule_names
 */
function stripTaskPrefix(name: string): string {
  // 前缀限定 ascii（task_id 受后端 schema 约束为 [a-z0-9_]），与 Python
  // _strip_task_prefix 同口径——不误吞以中文方括号 token 起头的规则名。
  return name.replace(/^\[[A-Za-z0-9_-]+\]\s*/, "");
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
        // 新格式（任务 / 规则）后端已 strip、无需处理；下面只清历史旧行的
        // [task_id] 前缀。行内空白用 [^\S\n]* 而非 \s*：短名退化成「仅前缀」时不吞换行。
        return section
          .replace(/触发规则：\[[A-Za-z0-9_-]+\][^\S\n]*/g, "触发规则：")
          // 旧数据：query 空时「触发条件」兜底成 [task_id] 规则名。前缀限定 task_id 形态
          // （ascii snake/kebab），放过以中文方括号 token 开头的合法 query（如「[夜间]…」）。
          .replace(/触发条件：\[[A-Za-z0-9_-]+\][^\S\n]*/g, "触发条件：");
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
