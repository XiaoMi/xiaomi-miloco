/**
 * 清理事件 text 里规则相关内容残留的 `[task_id]` 工程指针前缀。
 *
 * 当前格式（任务 / 规则）后端已构造成住户可读形态、DB 里就不带 `[task_id]`，
 * 前端原样渲染、无需处理。本函数只清理**同 header 的历史旧行**（触发规则 / 触发条件）
 * 以及旧 v1/v2 格式里残留的 `[task_id]` 前缀——那些旧数据 DB 里仍带前缀。
 *
 * 兼容格式:
 * - 当前格式(分类 header): `[感知引擎]规则提醒：` 块含 `任务：<任务描述>` +
 *   `规则：[规则短名] query` + `触发状态：<已触发 / 未触发…>` —— 后端已构造成住户可读形态（无 [task_id]），无需 strip。
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

/** 「触发状态」的五种取值（后端封闭集）。折叠态渲染成 badge，展开态仍读原文本行。 */
export type TriggerStatusKind =
  | "fired" // 已触发
  | "stillIn" // 未触发（持续中）
  | "counting" // 未触发（计时中）
  | "notFired" // 未触发
  | "unknown"; // 未知

/**
 * 后端中文文案 → 枚举 kind。**必须整值精确匹配**：「未触发」是「未触发（持续中）」
 * 「未触发（计时中）」的真前缀，用 startsWith 会把后两者误判成 notFired。
 * 认不出的值（后端将来加了新状态而前端未跟上）→ 不出 badge、原文本行保留，不猜。
 */
const STATUS_TEXT_TO_KIND: Record<string, TriggerStatusKind> = {
  已触发: "fired",
  "未触发（持续中）": "stillIn",
  "未触发（计时中）": "counting",
  未触发: "notFired",
  未知: "unknown",
};

/** 规则块特有的字段行——用于区分「真规则块」与「老数据里带伪造状态行的其它块」。 */
const RULE_BLOCK_FIELD = /^(?:任务|规则|触发原因)：/;

/** 折叠态用的一段：正文 + 该段的触发状态（若有）。 */
export interface HumanizedSection {
  /** 段正文；识别出状态时已把「触发状态：…」整行摘掉（那行改由 badge 承载）。 */
  text: string;
  /** 识别出的触发状态；无该行 / 值认不出时为 undefined（不渲染 badge）。 */
  status?: TriggerStatusKind;
}

/**
 * 把 humanize 后的文本切成折叠态要渲染的段，并抽出每段的「触发状态」。
 *
 * 为什么要抽出来：折叠态每段被裁到 2 视觉行且**没有** `pre-wrap`（块内换行塌成空格），
 * 而「触发状态」排在「任务 / 规则」之后——那两行长度无上界，状态常被裁掉，功能等于不可见。
 * 抽成定长 badge 排在最前即可稳定可见。展开态不走本函数、仍显示完整原文。
 *
 * 状态是 **per 规则块**而非 per 事件：一条 event 可含多个规则块（后端 `═══` 分隔），
 * 各自状态可以不同，故按段返回。
 */
export function splitHumanizedSections(humanized: string): HumanizedSection[] {
  if (!humanized) return [];
  return humanized
    .split(/\n\n/)
    .filter((s) => s.trim())
    .map((section) => {
      const lines = section.split("\n");
      const idx = lines.findIndex((l) => l.startsWith("触发状态："));
      if (idx === -1) return { text: section };
      // 老数据兜底：折叠防线是后来才补的，DB 里旧行的「建议 / 语音指令」等模型直出字段
      // 可能带未折叠的 `\n触发状态：已触发`（只需一个 \n，不需要 \n\n）。若不加限定，
      // 那行伪造内容会从不起眼的正文**升级成 UI 里最显眼、住户最信任的 badge**。
      // 故要求本段同时带规则块的其它字段行才认。前端做不出硬边界（伪造者可以把辅助特征
      // 一起伪造），但对「fix 之前写入的老数据」有效——那时的伪造并未针对本判据。
      if (!lines.some((l, i) => i !== idx && RULE_BLOCK_FIELD.test(l))) {
        return { text: section };
      }
      const kind = STATUS_TEXT_TO_KIND[lines[idx].slice("触发状态：".length).trim()];
      if (!kind) return { text: section }; // 认不出：保留原行，不出 badge
      return { text: lines.filter((_, i) => i !== idx).join("\n"), status: kind };
    });
}

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
        // 前缀限定 ascii，与其余三处 strip 同口径（不误吞中文方括号规则名）
        return section.replace(
          /检测到：\[[A-Za-z0-9_-]+\]\s*/g,
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
