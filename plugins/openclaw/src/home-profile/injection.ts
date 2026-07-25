import { readJsonFileSync } from "../utils/io.js";
import { nowLocalIso } from "../utils/time.js";
import { habitSuggestionsPath } from "./helpers.js";

/**
 * 习惯建议候选库的**只读**注入 reader。
 *
 * 状态机（record / mark_asked / resolve / 过期）已移入 miloco-cli
 * （`miloco-cli habit …`，读写同一个 task-suggestions.json）；openclaw 侧只在
 * prompt hook 里读这份 store，补齐"用户这条『好』在回应什么"的指代。故此处保留一个不写盘的
 * 轻量 reader（与状态机的过期口径一致：`asked` 且 asked_at 未超 7 天），无需引入整套 tool。
 */

// 7 天过期口径。**必须与 Python 端 habit_store.py 的 STALE_DAYS(=7) 保持一致**：
// 那边是写侧（record/mark_asked/resolve/惰性过期）的权威，这里只是读侧注入的镜像。
// 改动其一务必同步另一处，否则注入块与 CLI 会对"某 asked 是否仍在等回应"判断相反。
const STALE_MS = 7 * 86_400_000;

type OpenQuestion = { key: string; title: string; suggestion: string };

type StoredEntry = {
  key?: string;
  title?: string;
  suggestion?: string;
  status?: string;
  asked_at?: string;
};

type StoredStore = { entries?: StoredEntry[] };

function elapsedMs(fromIso: string, nowIso: string): number {
  const a = Date.parse(fromIso);
  const b = Date.parse(nowIso);
  if (Number.isNaN(a) || Number.isNaN(b)) return 0;
  return b - a;
}

/**
 * 未过期的待回应（`asked`）条目；不写盘，作废留给下次 miloco-cli 调用持久化。
 * 导出仅供单测注入 `nowIso` 精确验证 7 天边界；生产由 buildPendingSuggestionBlock 用真实 now。
 */
export function loadOpenQuestions(nowIso = nowLocalIso()): OpenQuestion[] {
  const store = readJsonFileSync<StoredStore>(habitSuggestionsPath());
  const entries = store?.entries;
  if (!Array.isArray(entries)) return [];
  return entries
    .filter(
      (e): e is StoredEntry & { key: string; asked_at: string } =>
        e.status === "asked" &&
        typeof e.key === "string" &&
        typeof e.asked_at === "string" &&
        elapsedMs(e.asked_at, nowIso) <= STALE_MS,
    )
    .map((e) => ({
      key: e.key,
      title: e.title ?? "",
      suggestion: e.suggestion ?? "",
    }));
}

/**
 * 待回应习惯建议的注入块。仅在确有未作废 `asked` 条目时返回，否则空串（正常日子完全静默）。
 *
 * 由 hooks/prompt.ts 在 full profile 的 append 段调用（habit-suggest cron 推到用户 bind 的 IM
 * 会话，回应落在同一 full 会话；本块补齐"用户这条『好』在回应什么"的指代，并把它路由到
 * `miloco-cli habit resolve`，避免肯定语被当成无意图消息丢弃）。
 */
export function buildPendingSuggestionBlock(): string {
  let open: OpenQuestion[];
  try {
    open = loadOpenQuestions();
  } catch {
    return "";
  }
  if (open.length === 0) return "";

  const items = open
    .map((e) => `- [${e.key}] ${e.title}：${e.suggestion}`)
    .join("\n");

  return `## 等用户回应的习惯建议

你此前主动向用户推荐过把下面的习惯设成任务，正在等用户回应（**请勿重复推送同一条**）：

${items}

**如何处理用户这条消息：**
- 若是肯定/选择/否定语气（"好/可以/行/就第一个/不用了/不要"等）且**没有**其它明确意图 → 这就是对上面建议的答复：
  - 同意 → **先用一句话复述命中的是哪条**，再加载 miloco-create-task skill 据该 suggestion 建任务；**建成、拿到 task_id 后** \`miloco-cli habit resolve --key <对应 key> --outcome created --task-id <新任务id>\`。若 create-task 当轮以反问/中断结束、未建成 → 先不 resolve，条目留待用户补答后再落地（勿凭空 resolve）。
  - 拒绝 → \`miloco-cli habit resolve --key <对应 key> --outcome rejected\`，简短回应即可，**之后不再就这条打扰**。
- 多条待回应时按用户指代（"第一个/那个喝水的"）定位对应 key。
- 若用户这条消息**与这些建议无关**（在说别的事）→ **忽略本段，照常处理，不要调用 resolve**。`;
}
