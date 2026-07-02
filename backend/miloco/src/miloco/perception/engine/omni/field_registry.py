"""Omni 输出字段的单一来源 registry。

每个输出字段（identities / caption / speeches / env_sounds / matched_rules /
suggestions）的 **schema 字面量**、**字段说明文字**、**适用场景**集中定义在 ``FieldSpec``。
system prompt 的「# 输出格式」schema 与「# 字段说明」全部由本模块按场景派生
（``render_schema`` / ``render_field_spec``），杜绝散落多处的漂移。

``SceneDescriptor`` 描述一次调用的场景维度（video/audio × 有无身份候选 × stream），
``selected_fields`` 据此挑选并排序字段：audio 场景剥掉 ``requires_video`` 字段
（caption/identities）；有身份候选时把 identities 置于最前（与「先识别」一致）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class FieldSpec:
    """单个输出字段的完整定义。

    schema_literal —— 进「# 输出格式」JSON 的字面量片段。
    spec_md        —— 进「# 字段说明」的 ``## 字段名`` Markdown 块（含填写/判定细则）。
    requires_video —— 仅 video 场景输出（audio-only 场景剥离）。
    requires_audio —— 仅本轮实际把音频喂给模型时输出（音频未过 gate、未合成进 mp4 的
        window 剥离）。否则模型会就着画面脑补人声/环境音（实测 audio_tokens=0 仍幻觉出指令）。
    requires_speech —— 仅本轮 VAD 判出有真人声时输出（speeches 专用）。音频过了能量 gate
        但 VAD 判无人声（键鼠 / 底噪）时剥离本字段，根除模型在低信息音频上脑补"像指令的话"
        （实测：留着 speeches 字段就幻觉，剥掉即 0）。env_sounds 不挂此项，无人声仍保留。
    requires_identity —— 仅本轮有身份候选时输出。
    spec_md_audio —— audio-only 路由专用「字段说明」变体（纯音频无画面，视觉语言全部剔除）；
        为 None 时 audio 路由沿用 video 版（与视觉无关的字段，如 env_sounds）。
    """

    name: str
    schema_literal: str
    spec_md: str
    requires_video: bool = False
    requires_audio: bool = False
    requires_speech: bool = False
    requires_identity: bool = False
    spec_md_audio: str | None = None

    def spec_for(self, route: str) -> str:
        if route == "audio" and self.spec_md_audio is not None:
            return self.spec_md_audio
        return self.spec_md


IDENTITY = FieldSpec(
    name="identities",
    schema_literal='"identities":[{"track_id":<int>,"name":"<姓名|unknown|no_person>","confidence":0-1,"reason":"≤20字"}]',
    spec_md="""## identities
- 覆盖"待识别 track"所有 track_id，不遗漏不新增。name 三选一：对上 <gallery> 成员→填其 name；确有人但认不出→填 "unknown"；框内根本不是人（非人物体被误检成人）→填 "no_person"
- 先定 name 与 confidence，再用 reason（≤20字）简述所靠特征（面部/发型/体型）；reason 是对已定结论的事后交代，不得先编"吻合"叙事把自己说服进匹配

- 待识别 track（无论首次出现还是系统重核）一律只凭 <gallery> 独立判断、不沿用任何旧结论
- 已识别人物/陌生人（带 bbox）是先验位置信息（这些 track 不在待识别列表、不必你重判）
- 判据优先级 面部 > 发型/体型 > 衣着
- 认定是「本人正向吻合」不是「相对最像 / 排除法」：必须该成员本人面部确实吻合才填其名；某成员即便是库中唯一同性别、唯一候选，也不能因「排除了其他人」或「最接近」就认定——对不上本人就填 unknown。库中某性别只有一名成员时尤其警惕：不要因「同性别只剩他一个」就认成他

- 面部判定要『全项吻合』、非『部分吻合』：逐项核对眼型/眼距、鼻型、嘴型、脸型轮廓、眉、有无胡须；任一项明显不同 → 判不同人、填 unknown；不得因『某几项相似』就认成该成员（宁因一项不符漏认，不因几项相似错认）
- 脸部分可见/侧脸 ≠ 必然不可用，但门槛要硬：仅当该角度下眼/鼻/嘴/脸型等关键特征仍逐项清晰、足以完成上一条的全项核对时，才按面部判据走；只要有关键特征被遮挡或模糊到无法逐项核对，就当面部不可用、退到发型/体型，不得凭侧脸的笼统印象认定（拿不准宁可不认）
- face_visible=false（无清晰人脸）时需有其他强区分证据才可高置信：库中只有一个成员明确符合该外观→可较高置信；『多个成员外观相近且看不到脸 → 中置信或 unknown』，不得仅凭性别+发型粗粒度相似就认定

- 性别不一致不认：与候选成员体型/衣着相似但性别不同 → unknown；性别看不清时此条不生效
- 衣着只作辅助参考、不作决定性判据，尤其不得仅因衣着不同就拒认或大幅降低置信；换衣/换发是同一人常态，面部对得上即同一人

- 按「区分度」判置信，不按「有无脸」硬归类——决定 confidence 的不是「看不看得到脸」，而是「能否在 <gallery> 其他成员里把这人独一无二地认出来」：高(≥0.85)=清晰人脸明显吻合，或独特外观组合(身型+发型)吻合『且不与库中其他成员混淆』；中(0.65–0.85)=多项外观线索倾向该成员，但人脸不清晰、『无法完全排除其他相似成员』；低(<0.65)=仅泛化相似（同性别/同发色/相近体型等，库中其他成员也可能符合）→ 倾向 unknown
- confidence = 对本次判断的把握，判成员或判 unknown 都按"有多确定"打分，与 name 取值无关
- no_person vs unknown：框内没有真实人体、只是家中非人物体被误框（如 3D 打印机、落地扇、衣帽架或搭挂/晾晒的衣物、落地绿植、纸箱行李堆 等外形易被误当成人的物体）→ "no_person"，纵使轮廓像人；确有人体（哪怕背影/侧身/局部/遮挡/模糊）→ "unknown"；分不清是人还是物时才倾向 unknown（别把真人误判成没人）""",
    requires_video=True,
    requires_identity=True,
)

CAPTION = FieldSpec(
    name="caption",
    schema_literal='"caption":"详细描述"',
    spec_md="""## caption
- 如实描述本轮画面所见，优先动态部分：①人、宠物的状态和正在做的事（含手持物）②物品移动（从哪移到哪）③设备运行（电视播放、风扇运转等）④环境异常（冒烟、起火、漏水、液体外溢等）
- ≤100 字；不用规则措辞、不因规则夸大
- 待识别 track 若 identities 判为 no_person（框内确无人）→ caption 不据该框描述任何人物或动作，当作此处没人；仅 identities 判 unknown 才写"陌生人 / 某人"；名册里的"陌生人"若本轮画面没真正看到，也别写进 caption（仅限陌生人，成员不受限）
- 涉及人物只用本轮 identities 判出的姓名；identities 没识别出该人（判 unknown / 没给出）→ 写"陌生人 / 某人"，不写"一人"这类泛称，不要从 gallery 或家庭档案里取成员名安到没被识别出的人身上
- 物体类别拿不准时退到上位概念（"细长物体" / "手中持有物体" / "桌面有物品"），不对不确定物体硬落具体类别（"水杯" / "手机" / "食物"）——宁可粗不可错""",
    requires_video=True,
)

SPEECHES = FieldSpec(
    name="speeches",
    schema_literal='"speeches":[{"speaker":"人","content":"原文","is_complete":true|false,"needs_response":true|false}]',
    spec_md="""## speeches
- 仅人声。needs_response：仅当本句是对智能助手说的【信息查询】（问时间/天气/日程）、【设备控制】（开关/调节某设备）或【任务请求】（要助手去做/记某件事）才=true；家人间对话、自言自语、情绪感叹、纯陈述/闲聊、以及疑问句但显然在问家人而非助手的，一律=false
- speaker：画面里看到某人正在说话（嘴动/说话姿态）、且其已在 identities 识别出 → 填其姓名；否则填"未知"。无声纹，不凭声音猜、不从 <gallery> 或家庭档案取名
- content：仅转录清晰可辨的人声；嘈杂、含糊、听不清在说什么 → 不输出该条（宁可不转也不硬凑、不把同一句反复输出）。本轮输入若带 last_speech（上一窗没说完的半句），是否与本轮拼接成完整句见该处说明
- is_complete：语义完整=true；不完整（只有动词没宾语，如"打开""帮我"）=false""",
    spec_md_audio="""## speeches
- 仅人声。needs_response：仅当本句是对智能助手说的【信息查询】（问时间/天气/日程）、【设备控制】（开关/调节某设备）或【任务请求】（要助手去做/记某件事）才=true；家人间对话、自言自语、情绪感叹、纯陈述/闲聊、以及疑问句但显然在问家人而非助手的，一律=false
- speaker：本轮无画面、无身份信息，无法判断是谁在说话 → speaker 一律填"未知"（除非本人在话里自报姓名）。不凭声音猜、不臆测姓名，也不从家庭档案取成员名
- content：仅转录清晰可辨的人声；嘈杂、含糊、微弱、听不清在说什么 → 不输出该条（宁可不转也不硬凑、不把同一句反复输出）。本轮输入若带 last_speech（上一窗没说完的半句），是否与本轮拼接成完整句见该处说明
- is_complete：语义完整=true；不完整（只有动词没宾语，如"打开""帮我"）=false""",
    requires_audio=True,
    requires_speech=True,
)

ENV_SOUNDS = FieldSpec(
    name="env_sounds",
    schema_literal='"env_sounds":"环境音描述"',
    spec_md="""## env_sounds
- 只报「突发、短时、有明确事件含义」的非人声事件（玻璃破碎、警报、犬吠、敲门、婴儿哭、重物倒地…为例）；否则省略该字段
- 持续稳定的运行声 / 嗡鸣 / 底噪一律是背景，不报——空调、抽油烟机、冰箱、各类电器运行、锅具沸腾、键盘敲击、待机音、正常脚步等都属此类
- 微弱、模糊、听不清在响什么 → 不硬识别、不输出（宁缺毋滥）
- 人说话归 speeches""",
    requires_audio=True,
)

MATCHED_RULES = FieldSpec(
    name="matched_rules",
    schema_literal='"matched_rules":[{"rule_name":"规则名","reason":"判断依据","hit":true|false}]',
    spec_md="""## matched_rules
- 基于本轮观察判断"# 待判断规则"是否满足；与本轮明显无关的可不列（系统只对 hit=true 触发）
- reason 先写证据、再定 hit：hit=true 必须 reason 给出"规则每个要素都满足"的本轮证据——规则点名的人以本轮 identities 为准（没被 identities 识别在场的人 → 该规则 hit=false，不从 gallery / 家庭档案推断是谁），活动 / 状态只据本轮画面判断（听到的话 / 声音不作规则命中依据，音频不稳；见总原则）且不得与 caption 相矛盾；证据不全、靠推测、或与 caption / identities 抵触 → hit=false
- rule_name 只能从"# 待判断规则"段原样照抄某一条完整名称（方括号开头那串，如 [pet_safety] 宠物破坏家具），严禁自创；reason 引用本轮具体观察、别复述规则原文；该段为空则 matched_rules 输出 []""",
    # 规则判断本质需视觉证据（现有规则全是"见到人/姿势/在场"这类）；纯音频无画面，
    # 做 matched_rules 只会脑补或恒空、零正当价值——故 audio-only 轮直接剥离本字段
    # （见 selected_fields）。可听见的危险（求救/玻璃碎/报警）改由 audio 版 suggestions 兜底。
    requires_video=True,
)

SUGGESTIONS = FieldSpec(
    name="suggestions",
    schema_literal='"suggestions":[{"event":"事件","action":"建议","urgency":"high|medium|low"}]',
    spec_md="""## suggestions
- 独立于规则的「隐患巡检」：主动找本轮值得提醒的事。标准是下面三类——标准不是关键词清单，凡符合标准的都要报，别因为不在例子里就放过：
  ① 安全/健康危险：判据是人/宠物/财产是否真的在受伤害、或已处于失控——只认本轮画面或音频里真实可辨的危险/失控征兆，与现场有没有人无关（已起火/已溢锅，有人在也报）；人/宠物身体急性异常（摔倒/抽搐/呼救/说“动不了”等）同属此类。
  ② 家务隐患：家电/设施没在被正常使用、处于“本该用完收尾却被放任”的状态（门窗/柜门/抽屉敞开、水龙头流着、垃圾满溢、外露堆积等），放任会浪费·损坏·脏乱。判据是设备「有没有在正常发挥功能/处于正常使用态」，不是「有没有人」——(a) 正在履行本职功能的不报：灶具加热做饭、电视播放、灯照明、空调运转，哪怕此刻无人也是正常；(b) 开冰箱门/柜门/水龙头这类访问态本该用完即恢复：有人正在取放/使用=正常不报，无人在用却仍敞开/仍流=被放任的隐患、报。「有没有人」只是判断“在不在正常使用”的参考线索，不是硬规则。
  ③ 违反「# 家庭档案」中明确写明的禁止事项或偏好约定（须档案显式记录、本轮可直接观察到，不臆测、不自行推断习惯）。
- 不报陌生人/未识别人员的出现：身份是否可信交给 identity 字段与规则，suggestion 不因「画面里有不认识的人」报警。
- event/action 指称画面里的人只用本轮 identities 识别出的姓名；没识别出的人写"某人 / 陌生人"，不要从 gallery / 家庭档案取成员名安上（第③类「违反某成员约定」也须 identities 已确认该成员在场才成立）。
- 去重：对照「# 待判断规则」——某类事已有规则在管就归规则、不在此重复（如坐姿有坐姿规则）。
- event/action：event 只陈述本轮观察到的事实（主体+动作/物品/位置），不写大类、不臆测原因（见总原则）；action 给下一步处置
- urgency：high＝画面确认、正在发生的危险事件（画面直接看到：起火、溢锅、冒烟、摔倒、抽搐、持械伤人等）；medium＝声音里的危险信号（求救、惨叫、玻璃破碎、火警 / 燃气报警、重物倒地等；音频识别不够准，危险声止于 medium、提醒确认）；low＝家务隐患（冰箱门没关、水龙头长流、垃圾满溢等）/ 违反或不符合家庭档案约定""",
    spec_md_audio="""## suggestions
- 独立于规则的「隐患巡检」：本轮只有音频，只报**能从声音听出来**的、值得提醒的事。标准是下面三类——凡符合标准的都要报，别因为不在例子里就放过：
  ① 安全/健康危险（可听见的）：求救/惨叫、痛苦呻吟、剧烈咳嗽、重物倒地声、玻璃破碎、火警/燃气报警声等，指示人/宠物/财物正在受伤害或身体急性异常。
  ② 家务隐患（有声的）：持续不停的异常声响指示某事被放任——如水龙头/水流长时间不停、警报/定时器长鸣无人处理。看不见的隐患（门窗/冰箱敞开、堆积）无声、无从判断，不报。
  ③ 违反「# 家庭档案」中明确写明、且本轮能从声音直接听出的禁止事项。
- event/action 不要出现没被识别确认的成员名；无法确认是谁就写"某人 / 未知"，不从家庭档案取名。
- 听不清、微弱、拿不准是什么声音 → 不硬识别成隐患、不报。
- 不臆测画面：不得据声音脑补"看到谁/在做什么"再据以报警。
- event/action：event 只陈述本轮听到的事实（什么声音+含义），不写大类、不臆测原因（见总原则）；action 给下一步处置
- urgency：medium＝声音里的危险信号（求救、惨叫、玻璃破碎、火警 / 燃气报警、重物倒地等；本轮无画面、音频识别不够准，危险信号止于 medium）；low＝有声的家务隐患（水龙头长流、定时器 / 警报长鸣等）/ 违反家庭档案约定。本轮无画面，不产生 high""",
)


# 常规字段顺序（identities 不在内，由 has_identity 时单独 prepend）。
_ORDER_NORMAL = ["caption", "speeches", "env_sounds", "matched_rules", "suggestions"]
_ORDER_STREAM = ["speeches", "env_sounds", "matched_rules", "suggestions", "caption"]

_REGISTRY = {f.name: f for f in (IDENTITY, CAPTION, SPEECHES, ENV_SOUNDS, MATCHED_RULES, SUGGESTIONS)}


@dataclass(frozen=True)
class SceneDescriptor:
    """一次 omni 调用的场景维度，驱动 system prompt 的按需装配。

    route        —— ``_resolve_route(packets)`` 的结果（audio 场景无视觉）。
    has_identity —— 本轮是否有待识别身份候选（``bool(candidates)``，仅 fused 路径非空）。
    stream       —— 是否流式（非 fused 路径；stream 与 has_identity 不会同时为真）。
    has_audio    —— 本轮是否真把音频喂给模型（video 路由下音频未过 gate 时为 False，
        剥掉 speeches / env_sounds，避免模型就着画面脑补人声/环境音）。audio 路由恒 True。
    has_speech   —— 本轮 VAD 是否判出有真人声。音频过 gate（has_audio=True）但 VAD 判无
        人声（键鼠 / 底噪）时为 False → 只剥 speeches、保留 env_sounds。has_audio=False 时
        speeches 已被 requires_audio 剥掉，本标志无额外作用。
    """

    route: Literal["video", "audio"]
    has_identity: bool = False
    stream: bool = False
    has_audio: bool = True
    has_speech: bool = True

    def selected_fields(self) -> list[FieldSpec]:
        order = _ORDER_STREAM if self.stream else _ORDER_NORMAL
        fields = [_REGISTRY[n] for n in order]
        if self.route == "audio":
            fields = [f for f in fields if not f.requires_video]
        if not self.has_audio:
            fields = [f for f in fields if not f.requires_audio]
        if not self.has_speech:
            fields = [f for f in fields if not f.requires_speech]
        if self.has_identity:
            fields = [_REGISTRY["identities"], *fields]
        return fields


def render_schema(scene: SceneDescriptor) -> str:
    """按场景拼出「# 输出格式」的 JSON schema 字面量。"""
    return "{" + ",".join(f.schema_literal for f in scene.selected_fields()) + "}"


def render_field_spec(scene: SceneDescriptor) -> str:
    """按场景拼出「# 字段说明」正文（各字段 ``## 字段名`` 块；audio 路由取 audio 变体）。"""
    return "\n\n".join(f.spec_for(scene.route) for f in scene.selected_fields())
