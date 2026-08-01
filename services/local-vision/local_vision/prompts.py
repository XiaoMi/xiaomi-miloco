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

# 判定词**只在行首成词时才算判定**,绝不做「整句里含不含某个字」的子串匹配。
#
# 子串匹配在中文里是错的,而且错的方向要命:随便一句散文里的「但是 / 于是 / 总是」
# 都含「是」,于是「画面中看到一个人影,但是不能确定」会被读成命中 —— 得到一条
# hit=True 而依据恰好在说反话的记录,正是 fail-closed 要杜绝的东西。
# 改成锚定行首:读不出判定就落未判定(未命中),宁可漏报。
_VERDICT_RE = re.compile(
    r"^\s*(?:"
    # 「是否…」是模型在复述问句,不是回答 —— 必须先于肯定分支拦掉。
    r"(?P<question>是否)"
    # 否定侧刻意宽松、不要求词边界:多认一个否定只会漏报,方向是安全的。
    r"|(?P<neg>不是|不成立|未命中|不满足|未满足|没有|不|否|无|非|未|not|no|false)"
    # 肯定侧必须**成词收尾**(后面是行尾或标点/分隔符),否则「有人在客厅沙发上,
    # 但是现在已经不在了」这种复述条件的句子会因为以「有」开头被判成命中。
    r"|(?P<pos>(?:是的|是|成立|满足|命中|有|yes|true)(?=$|[\s,，。、;；:：!！?？\-—]))"
    r")",
    re.IGNORECASE,
)

DEFAULT_SCENE_ASK = (
    "请用中文详细描述这个家庭监控画面里的场景:有没有人、在做什么、"
    "环境里有什么值得注意的情况。"
)


def build_prompt(
    scene_ask: str, rules: list[dict], camera_note: str = ""
) -> str:
    """拼出一次推理的提问:场景描述 +(可选)逐条规则判定 +(可选)机位须知。

    rules 每项形如 ``{"name": ..., "query": ...}``;query 是规则的自然语言条件
    (miloco 侧已强制它写成进行时状态描述,不能是「检测到…」这类断言句)。

    ``camera_note`` 是用户在面板上给这台相机写的机位说明。它**必须**:
    - 作为**补充**,而不是取代任务提问 —— 否则整个提问会退化成一句用户指令,
      模型连"描述场景"这个任务都不知道;
    - 渲染在格式约定**之后**的受限区块里 —— 它是用户/agent 可写的自由文本,
      放在格式说明之前的话,一句「只用一句话回答」「用 JSON 输出」就能让
      ``规则N:`` 那几行消失,而 fail-closed 会把这变成"该相机所有规则静默失效",
      没有任何报错。放在后面并明确它不改变输出格式,风险小得多。
    """
    ask = scene_ask.strip() or DEFAULT_SCENE_ASK
    note = (camera_note or "").strip()
    if not rules:
        return f"{ask}\n\n{_note_block(note)}" if note else ask

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
    if note:
        lines.append("")
        lines.append(_note_block(note))
    return "\n".join(lines)


# 机位须知的最大长度。用户/agent 可写,过长会淹没任务提问本身。
_NOTE_MAXLEN = 400


def _note_block(note: str) -> str:
    """把机位须知包成一个明确不改变输出格式的区块。"""
    return (
        "以下是这台摄像头的机位说明,仅供你理解画面时参考,"
        "**不改变上面要求的回答格式**:\n"
        f"「{note[:_NOTE_MAXLEN]}」"
    )


NO_VERDICT_REASON = "模型未给出判定"


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
    # 判定必须**成词出现在判定头的开头**。整句扫描会让散文里的「但是」变成命中。
    m = _VERDICT_RE.match(head)
    if m is None or m.group("question"):
        # fail-closed。此处**不能**把 head 当依据回填 —— 模型有时干脆复述条件原文
        # (实测:「规则1: 有人在客厅沙发上」),那样会得到一条 hit=False 却写着
        # 「有人在客厅沙发上」的记录,读的人会正好读反。统一落未判定标记。
        return False, NO_VERDICT_REASON
    hit = m.group("neg") is None
    # 依据:优先分隔符之后的部分;没有分隔符时取判定词之后的余下文字。
    tail = reason.strip() or head[m.end():].strip(" ,,。、:：-—")
    return hit, tail or head.strip()


def _verdict_from_following_line(lines: list[str], idx: int) -> tuple[bool, str]:
    """规则行本身没给判定时,往下找紧邻的一行判定(跳过空行)。

    只看到下一条规则行为止 —— 越过它就会把别人的判定安到自己头上。
    """
    for nxt in lines[idx + 1:]:
        if not nxt.strip():
            continue
        if _RULE_LINE_RE.match(nxt):
            break
        return _verdict(nxt)
    return False, NO_VERDICT_REASON


def parse_response(raw: str, rules: list[dict]) -> tuple[str, list[dict]]:
    """把自由文本拆成 (caption, rule_hits)。

    解析失败不抛异常:caption 兜底为规则行之前的全部散文,规则兜底为未命中。
    """
    verdicts: dict[int, tuple[bool, str]] = {}
    prose: list[str] = []
    trailing: list[str] = []  # 规则区之后、显式带「描述:」标记的行
    seen_rule = False
    lines = raw.splitlines()

    for idx, line in enumerate(lines):
        m = _RULE_LINE_RE.match(line)
        if m:
            seen_rule = True
            hit, reason = _verdict(m.group(2))
            if reason == NO_VERDICT_REASON:
                # 实测的第四种变体:模型在「规则N:」行**复述条件原文**,把判定
                # 放到紧跟的下一行(「规则1: 有人在客厅沙发上」+「否 - 依据: …」)。
                # 不往下看一行就会把一次真判定读成未判定 —— 说"是"时那就是漏报。
                hit, reason = _verdict_from_following_line(lines, idx)
            verdicts[int(m.group(1))] = (hit, reason)
            continue
        s = _LEADING_NUM_RE.sub("", line).strip()
        tagged = _CAPTION_TAG_RE.match(s) is not None
        s = _CAPTION_TAG_RE.sub("", s, count=1).strip()
        if not s:
            continue
        if seen_rule:
            # 规则区之后的行默认是补充说明,不进 caption —— 除非它显式带了
            # 「描述:」标记。有些回答把描述写在判定后面,一律丢弃会让 caption 空掉
            # 且无从恢复。
            if tagged:
                trailing.append(s)
            continue
        prose.append(s)

    # 只有在**完全没识别出规则行**时才把整段原文当描述。若已经解析到规则行,
    # 说明模型只回了判定、没写描述 —— 此时兜底回原文会把「规则1: 否 - …」这种
    # 机器格式一路带进事件文案与 agent 上下文,宁可留空。
    caption = " ".join(prose).strip() or " ".join(trailing).strip()
    if not caption and not seen_rule:
        caption = raw.strip()

    hits = []
    for i, r in enumerate(rules, 1):
        hit, reason = verdicts.get(i, (False, NO_VERDICT_REASON))
        hits.append({"name": r.get("name", ""), "hit": hit, "reason": reason})
    return caption, hits
