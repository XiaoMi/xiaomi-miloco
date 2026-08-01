"""提示词构建与响应解析 —— 模型专属的脏活全部收在边车里。

miloco 主进程只看契约(送视频段+规则 → 拿 caption + rule_hits),不关心某个
模型要怎么被问、回答长什么样。换模型时只需要改本文件与 engine.py。

设计要点:
- **不要求 JSON**。Mage-VL 模型卡明写只产自由文本(无 structured output 能力),
  对 4B 规模强套嵌套 schema 是自找解析失败。改用「逐行 `规则N: 是/否`」这种
  最低限度的结构 —— 实测该格式模型能稳定遵循,且三种自发变体都能解析:
      规则1: 是
      规则1: 否 - 沙发上没有人
      规则1: 否。画面中没有人在客厅沙发上。
- **规则判定 fail-closed**。解析不出判定即视为「未命中」,绝不猜。漏报只是少一次
  agent 提醒,误报却会让 agent 对着不存在的事实做决策。
"""

from __future__ import annotations

import re

# 「规则N: <判定><分隔><依据>」。分隔符可有可无(- / 。/ 空),依据可缺省。
_RULE_LINE_RE = re.compile(r"^\s*(?:\d+[.、]\s*)?规则\s*(\d+)\s*[:：]\s*(.+)$")
# 模型偶尔把描述编号成「1.」「2.」,清掉行首编号免得混进 caption。
_LEADING_NUM_RE = re.compile(r"^\s*\d+[.、]\s*")
# 「描述:」是我们要求的行首标记,是格式而非内容 —— 落进 caption 会一路带到
# 事件文案与 agent 上下文里,必须剥掉。
_CAPTION_TAG_RE = re.compile(r"^(?:描述|场景描述|描述内容)\s*[:：]\s*")

# 判定词。未命中侧必须先查:「不成立」同时含「成立」,顺序反了会误判。
_MISS_WORDS = ("否", "不成立", "未命中", "没有", "无", "no", "false")
_HIT_WORDS = ("是", "成立", "命中", "yes", "true")

DEFAULT_SCENE_ASK = (
    "请用中文详细描述这个家庭监控画面里的场景:有没有人、在做什么、"
    "环境里有什么值得注意的情况。"
)


def build_prompt(scene_ask: str, rules: list[dict]) -> str:
    """拼出一次推理的提问:场景描述 +(可选)逐条规则判定。

    rules 每项形如 ``{"name": ..., "query": ...}``;query 是规则的自然语言条件
    (miloco 侧已强制它写成进行时状态描述,不能是「检测到…」这类断言句)。
    """
    ask = scene_ask.strip() or DEFAULT_SCENE_ASK
    if not rules:
        return ask

    # 「描述:」这个前缀是模型自发使用的(实测),顺着它写比强推自定义标记稳。
    # 同时必须显式要求描述**独立于**规则 —— 否则模型会把描述写成规则判定的复述
    # (实测:不加这句时 caption 变成「画面中没有人在沙发上,也没有宠物」)。
    lines = [
        ask,
        "",
        "先输出一行以「描述:」开头的场景描述 —— 描述你实际看到的画面内容本身,",
        "不要复述下面的判断条件。描述控制在两三句话内,写完整,不要中途截断。",
        "",
        "然后逐条判断下列条件此刻是否成立。每条单独一行,格式严格为",
        "规则N: 是 - 依据   或   规则N: 否 - 依据",
        "",
        "需要判断的条件:",
    ]
    for i, r in enumerate(rules, 1):
        cond = (r.get("query") or r.get("name") or "").strip()
        lines.append(f"规则{i}: {cond}")
    return "\n".join(lines)


def _verdict(body: str) -> tuple[bool, str]:
    """把「是 - 沙发上有人」这类判定体拆成 (命中?, 依据)。"""
    text = body.strip()
    # 依据分隔符:优先 '-',其次中文句号(模型常写「否。画面中没有人」)。
    reason = ""
    head = text
    for sep in ("-", "—", "。", ":", "："):
        if sep in text:
            head, _, reason = text.partition(sep)
            break
    head_l = head.strip().lower()[:12]
    if any(w in head_l for w in _MISS_WORDS):
        hit = False
    elif any(w in head_l for w in _HIT_WORDS):
        hit = True
    else:
        hit = False  # fail-closed:读不懂就不算命中
    return hit, reason.strip() or head.strip()


def parse_response(raw: str, rules: list[dict]) -> tuple[str, list[dict]]:
    """把自由文本拆成 (caption, rule_hits)。

    解析失败不抛异常:caption 兜底为规则行之前的全部散文,规则兜底为未命中。
    """
    verdicts: dict[int, tuple[bool, str]] = {}
    prose: list[str] = []
    seen_rule = False

    for line in raw.splitlines():
        m = _RULE_LINE_RE.match(line)
        if m:
            seen_rule = True
            verdicts[int(m.group(1))] = _verdict(m.group(2))
            continue
        if seen_rule:
            continue  # 规则区之后的补充说明不进 caption
        s = _LEADING_NUM_RE.sub("", line).strip()
        s = _CAPTION_TAG_RE.sub("", s, count=1).strip()
        if s:
            prose.append(s)

    caption = " ".join(prose).strip() or raw.strip()

    hits = []
    for i, r in enumerate(rules, 1):
        hit, reason = verdicts.get(i, (False, "模型未给出判定"))
        hits.append({"name": r.get("name", ""), "hit": hit, "reason": reason})
    return caption, hits
