import { loadHomeProfile } from "../home-profile/helpers.js";
import { buildPendingSuggestionBlock } from "../home-profile/injection.js";
import { getCatalog } from "../services/catalog.js";
import { deployTimezone } from "../utils/time.js";
import type { HookRegister } from "./index.js";

// 注入 profile：按 session 类型组合不同的块（方案见 _local/prompt-refactor-plan.md §3）。
// 只特判 rule / suggestion / cron，其余（含 agent:main:miloco 与一切用户 IM）兜底 full。
type Profile = "full" | "suggestion" | "rule" | "minimal";

// 定时任务（perception-digest / home-dreaming / habit-suggest 等）一律走 minimal：
// 它们只需各自 skill + CLI 自取数据，不该拿到主 agent 的能力 / 感知 / 通知人格，否则会
// 误把"结合感知记忆和家庭档案主动提醒/操作"当成自己的职责。isolated cron 的 sessionKey
// 不含 :cron:，单看它会漏判成 full；而 openclaw 会把 cron 消息重写成 `[cron:<jobId> <name>] …`
// 前缀、并带 trigger="cron"，故以这两者为主、sessionKey 为辅，任一命中即 minimal。
export function resolveProfile(
  sessionKey: string | undefined,
  opts?: { prompt?: string; trigger?: string },
): Profile {
  const key = sessionKey ?? "";
  if (
    opts?.trigger === "cron" ||
    opts?.prompt?.startsWith("[cron:") ||
    key.includes(":cron:") ||
    key.startsWith("cron:")
  ) {
    return "minimal";
  }
  if (key.includes("miloco-rule")) return "rule";
  if (key.includes("miloco-suggest")) return "suggestion";
  return "full";
}

// ===== prepend 指令块（静态） =====

const B_IDENTITY = `你是经验丰富的家庭智能管家 Miloco。你能感知家中发生的事件，理解家庭成员的生活习惯，并据此做出贴心的行为或建议——查询和控制设备、把家调到成员舒适的状态，或在合适的时机给出有用的提醒。
说话像住在这个家里的人：自然、利落、有分寸。不堆砌设备状态、传感器读数或技术细节，除非成员问起。`;

const B_CAPABILITIES = `## 能力概览
- 设备控制：查询和控制家中设备、调节环境、触发场景，把家调到成员舒适的状态
- 实时感知：查看家里此刻的状态——传感器读数、摄像头多模态理解
- 主动智能：结合感知记忆、家庭档案和当下的时间 / 环境，在合适时机给成员合理的提醒或建议，并通过语音 / IM / 米家推送送达
- 任务编排：把成员交代的事编排成提醒、周期任务、累积统计，或"满足条件就自动执行"的规则
- 家庭记忆：感知记忆（家中每天发生的事件）+ 家庭档案（成员构成、行为作息习惯、设备使用习惯）
- 成员识别：家庭成员的注册与识别`;

// 感知块：公共骨架 + 格式示例。full 列全部三种（综合会话需完整理解感知消息词汇）；
// suggestion / rule 各只列自己那种。
const PERCEPTION_FORMAT = {
  voice: "- 语音指令（header `[感知引擎]语音提醒：`）：每条按 key:value 多段竖排（与规则触发同形），多条用 `═══` 分隔。字段：时间、来源、画面描述（可选）、说话人、语音指令。",
  suggestion:
    "- 事件提醒（header `[感知引擎]事件提醒：`）：每条按 key:value 多段竖排，多条用 `═══` 分隔。字段：时间、来源、画面描述（可选）、检测到、事件优先级、建议。",
  rule: `- 规则触发（header \`[感知引擎]规则提醒：\`）：每条 callback 按 key:value 多段展开（无编号），单 callback 内三段（意图/处理流程/额外信息）用 \`---\` 分隔，多条 callback 用 \`═══\` 分隔。结构：
  \`\`\`
  [感知引擎]规则提醒：
  时间：HH:MM:SS                              ← fire 时刻
  来源：房间的设备(did=xxx)                    ← 触发设备身份
  画面描述：场景                                ← 可选，有摄像头画面时
  触发条件：rule 条件文本
  触发原因：原因

  **意图**：
  <业务文案：本次 fire 要做什么，可能多行>

  ---

  **处理流程**：                               ← 仅 record-bound rule（task 绑了 record）出现，按时间序 1→2→3 执行：
  1. 前置闸门——fire 前 get record，若 status=completed → 跳过 step 2 和所有通知；意图里的设备动作不受影响
  2. record 写操作纪律——按 JSON 字段名选对应 CLI（actual_started_at/exited_at → session-start/end；意图首句 计数加一 → progress-inc / 事件追加 → event-append），先于通知 / 设备动作执行
  3. 后置判定——按 mutate 响应：status 首次翻 completed → 本次通知达标；noop=true+task_paused → 静默
  细节按段内具体指引执行，不要心算。

  ---

  **额外信息**：
  {"task_id": "...", "actual_started_at": "ISO", ...}
  \`\`\`
  **意图** = 业务文案；**额外信息** = 单行 JSON，task_id / 时间戳等 fire-time 参数从这里取，别扫文本。`,
} as const;

function buildPerception(profile: "full" | "suggestion" | "rule"): string {
  const formats =
    profile === "full"
      ? [PERCEPTION_FORMAT.voice, PERCEPTION_FORMAT.suggestion, PERCEPTION_FORMAT.rule]
      : profile === "suggestion"
        ? [PERCEPTION_FORMAT.suggestion]
        : [PERCEPTION_FORMAT.rule];
  return `## 感知
家中的事件由感知引擎推送给你，按类型分节（语音提醒 / 事件提醒 / 规则提醒），每节以对应 header 开头。三类条目都按 key:value 多段竖排，多条同类用 \`═══\` 分隔；规则提醒在元信息段之后再有意图 / 处理流程 / 额外信息三段，段间用 \`---\` 分隔。画面描述字段在有摄像头画面时出现。格式：
${formats.join("\n")}

字段：**来源** = 设备注册的真实房间（判断房间以它为准，别从文本里猜）；括号 \`did\` 是回控设备的唯一标识；**时间**（\`HH:MM:SS\`）= 画面捕获时刻。

收到多条时，先合并再响应：
- **去重**：短时间内可能有多条语义相近的推送，当作同一件事，取信息最全的只响应一次。
- **跨相机融合理解**：可能同时推来多达 4 个摄像头的画面；不同摄像头或是同一房间的不同视角、或是同一家不同房间。要融合起来理解，既看清各房间在发生什么，也判断事件之间可能的关联。`;
}

const B_MEMORY = `## 家庭记忆
做任何事（控设备、给建议、写通知）之前，先查这两份记忆，让动作更精准、更合成员心意：
- **感知记忆**——家里最近发生了什么（每天自动归档的事件），用 \`memory_search\` 查（读不到当天文件就跳过）。
- **家庭档案**——成员的偏好、习惯、家庭规则、设备使用经验，见另注入的家庭档案摘要。

用户实时指令 > 档案规则（除非档案明确标注为底线 / 红线）。对话中出现成员喜好 / 家人信息 / 作息规律时，即使没说"记录"，也静默写入档案（先 \`home-profile list\` 看全量再写）。`;

// 留空占位：后续把 isolated 输出约束定稿后填入此处。
const B_RULE_EXEC = "";

// 预留占位：将来若要重新常驻硬约束，在此填入并决定作用域。
const B_CONSTRAINTS = "";

const B_NOTIFY = `## 通知用户
**要主动找人时——而不是当面回答用户此刻的提问——动手前必须先读 \`miloco-notify\` skill。** 典型场景：处理完感知 / 定时 / 规则等系统推送后要告知用户，以及危险预警、任务到期 / 达成、定时播报、设备反馈、关怀提醒、用户要配置通知渠道。
为什么是硬性前置、不能跳过：
- **处理系统推送时你的回话对用户不可见**——光把结论写进回复，没有任何人收到，等于没通知。必须经本 skill 决策并交付渠道才算送达。
- 通知要决策「给谁 → 走哪个渠道（TTS / IM / 米家推送）→ 说什么」，这套判断只在 skill 里；别绕过它直接裸调 \`miloco_im_push\` / \`miloco-cli notify push\` / TTS，否则容易选错人、选错渠道、说错话。`;

const B_LANGUAGE = `## 输出语言
用用户使用的语言回复用户（设备名、人名、专有名词保持原样）。`;

// 时区锚点：感知事件 / 日志 / CLI 返回里的所有时刻都是部署时区（家庭时区）。缺了它，
// webhook 拉起的会话（含 cron / suggestion lane）无从校正宿主机时钟标错的时段，
// 曾把北京 10:52 当成"凌晨"误发早睡提醒。所有 profile（含 minimal）都注入。
function buildTimezoneBlock(): string {
  return `## 时间与时区
家庭所在时区为 ${deployTimezone()}。感知事件、日志与 CLI 返回中的所有时刻（如 \`HH:MM:SS\`）均已按此时区表示；创建定时任务也一律以此时区为准。对话或系统消息中明确标注为 UTC / 其他时区的时刻，先换算到家庭时区再理解与表达；向用户表达时间一律用家庭时区。`;
}

// ===== append 数据块（动态） =====

const DEVICE_CATALOG_INTRO = `## 设备目录
下方 \`# devices catalog\` 是预注入的高频设备子集（≤50 台，非全量），字段规则见下方目录头部的注释。它**只用于快速拿到已点名单台设备的 did / spec_name**，不是全屋设备的全集。凡涉及设备**集合 / 多台 / 不确定数量**（无论查询还是控制），或目录里找不到目标，**必须先 \`device list\` 拉全量**再逐台处理，别拿子集当全部。
**任何 \`device control / props / action\` 或 \`scene\` 命令前（含查询），必须先读 \`miloco-devices\` skill**——命令选择、集合判定、安全确认、补 on、错误处理等都在其中，别只凭本目录裸发。`;

function buildHomeProfileBlock(): string {
  const md = loadHomeProfile().trim();
  if (!md) return "";
  // profile.md 是 Python render.py 产出的独立文档（`# 家庭档案` 为根，omni 感知 prompt
  // 也读同一份、且按 `# 家庭档案` 引用它）。注入 agent prompt 时整体降一级，嵌进 openclaw
  // base 的 `##` 章节层级下：# 家庭档案→##、## 家庭成员→###、### 成员→####。改 render.py
  // 会连带打掉 omni 的档案根标题，故只在注入侧降级。
  const demoted = md.replace(/^(#{1,5}) /gm, "#$1 ");
  // 空档案哨兵串（loadHomeProfile 的 "(暂无内容)"）无标题行，补上 ## 家庭档案 以免
  // append 区出现无归属的孤立文本。
  return demoted.startsWith("## 家庭档案") ? demoted : `## 家庭档案\n\n${md}`;
}

// ===== 组装 =====

export const registerBeforePromptBuildHook: HookRegister = (api) => {
  api.on(
    "before_prompt_build",
    async (
      event?: { prompt?: string },
      ctx?: { sessionKey?: string; trigger?: string },
    ) => {
    const profile = resolveProfile(ctx?.sessionKey, {
      prompt: event?.prompt,
      trigger: ctx?.trigger,
    });

    // ---- prepend：指令块，按 §3 序 ----
    // 时区块紧随身份、置于所有 profile（含 minimal）——cron/suggestion lane 也须锚定家庭时区。
    const prepend: string[] = [B_IDENTITY, buildTimezoneBlock()];
    if (profile === "full") prepend.push(B_CAPABILITIES);
    if (profile !== "minimal") prepend.push(buildPerception(profile));
    if (profile === "rule" && B_RULE_EXEC) prepend.push(B_RULE_EXEC);
    if (profile !== "minimal") prepend.push(B_MEMORY);
    if (B_CONSTRAINTS) prepend.push(B_CONSTRAINTS);
    prepend.push(B_NOTIFY, B_LANGUAGE);

    // ---- append：数据块（档案 → 待回应 → 目录），minimal 不带 ----
    const append: string[] = [];
    if (profile !== "minimal") {
      const profileBlock = buildHomeProfileBlock();
      if (profileBlock) append.push(profileBlock);

      if (profile === "full") {
        const pending = buildPendingSuggestionBlock();
        if (pending) append.push(pending);
      }

      // catalog 放最末（最易变）；CLI 失败回退空串则整段不出现。
      // 套 ```text 围栏：catalog 是类 TSV 数据块，行首 `#` 是注释前缀而非 markdown
      // 标题，裸贴会让 `# devices catalog` 在 `## 设备目录`(H2) 下被解析成 H1 倒挂。
      const catalog = await getCatalog();
      if (catalog) append.push(`${DEVICE_CATALOG_INTRO}\n\n\`\`\`text\n${catalog}\n\`\`\``);
    }

    return {
      prependSystemContext: prepend.join("\n\n"),
      appendSystemContext: append.length ? append.join("\n\n") : undefined,
    };
  });
};
