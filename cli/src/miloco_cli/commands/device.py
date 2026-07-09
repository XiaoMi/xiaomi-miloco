"""device 命令组：list / spec / catalog / control / props / action / refresh。"""

import json
import sys

import click

from miloco_cli.output import print_result

# MIoT spec 云端错误码 → 中文释义（device control / action / props 返回的
# data.results[].code / data.properties[].code 是设备侧执行码，负值即失败；
# 外层 code=0 只代表 RPC 送达，不代表设备执行成功）。来源：米家 spec 错误码表。
_MIOT_SPEC_CODES = {
    -704042011: "设备离线",
    -704042001: "未找到设备",
    -704090001: "未找到设备",
    -704040003: "属性不存在",
    -704040004: "事件不存在",
    -704040005: "方法不存在",
    -704040999: "功能未上线",
    -704044006: "未找到功能定义",
    -704030013: "属性不可读",
    -704030023: "属性不可写",
    -704030033: "属性不可上报",
    -704030992: "请求过于频繁，本次被拒绝",
    -704220043: "属性值不正确",
    -704220035: "方法输入参数错误",
    -704220025: "方法输入参数数量不匹配",
    -704222035: "方法输出参数数量不匹配或参数错误",
    -704222034: "事件参数数量不匹配",
    -704220008: "非法的 ID（SIID/PIID/EIID/AIID）",
    -704053100: "无法执行此操作",
    -704053101: "摄像机休眠中",
    -704013101: "红外设备不支持此操作",
    -704083036: "操作超时",
    -704012904: "设备未授权控制能力给小爱",
    -704012905: "设备未绑定",
    -704012906: "认证失败",
    -702022036: "操作正在处理中",
    -705201013: "读属性失败",
    -706012013: "读属性失败",
    -706012014: "读属性失败",
    -705201023: "写属性失败",
    -706012023: "写属性失败",
    -705201015: "方法执行失败",
    -706012015: "方法执行失败",
    -704002000: "设备错误（通用）",
}

# 设备侧"成功"码：0（后端归一）+ MIoT 原始 OK/accept，不当失败处理
_MIOT_OK_CODES = frozenset({0, -702000000, -702010000})


def _annotate_result_codes(data: dict) -> None:
    """按设备侧执行结果改写返回体，消除"外层成功、内层失败"的自相矛盾。

    外层 NormalResponse.code=0 只表示 RPC 送达；逐条结果的 code 才是设备执行结果，
    负值=失败。本函数：
    1. 给 data.results[] / data.properties[] / data.result 中失败项补 code_msg（中文释义）；
    2. 只要有失败，就把**外层 code/message** 一并改成反映真实结果——
       不再出现 code=0 + "executed successfully" 却设备离线的怪状态。

    control/props（set_property/set_properties）返回复数 ``results``；call_action 返回
    单数 ``result``（service.py:740）——两种 shape 都要覆盖，否则 action 失败时外层信封
    不会被改写（音箱 TTS 静默失败）。
    """
    inner = data.get("data")
    if not isinstance(inner, dict):
        return

    items: list[dict] = []
    for arr_key in ("results", "properties"):
        arr = inner.get(arr_key)
        if isinstance(arr, list):
            items.extend(it for it in arr if isinstance(it, dict))
    # call_action 返回单数 result（非数组），单独纳入
    single = inner.get("result")
    if isinstance(single, dict):
        items.append(single)

    total = 0
    failures: list[tuple[int, str]] = []  # (code, code_msg)
    for it in items:
        total += 1
        code = it.get("code")
        # 负值才是失败：米家 spec 错误码全为大负数（见 _MIOT_SPEC_CODES）。正数 / 0
        # 代表指令已下发并执行（实测部分开关返回 code:1 仍生效），绝不能误判为失败——
        # 否则上层 agent 见"失败"会盲目重试（甚至换 toggle 反复翻转开关）、并谎报失败。
        if isinstance(code, int) and code < 0 and code not in _MIOT_OK_CODES:
            msg = _MIOT_SPEC_CODES.get(
                code, "设备侧执行失败（未知错误码，详见米家 spec 错误码表）"
            )
            it["code_msg"] = msg
            failures.append((code, msg))

    if not failures:
        return

    # 外层信封对齐真实结果
    reasons = "；".join(dict.fromkeys(m for _, m in failures))  # 去重保序
    data["code"] = failures[0][0]
    if len(failures) == total:
        data["message"] = f"失败：{reasons}"
    else:
        data["message"] = f"部分失败（{len(failures)}/{total}）：{reasons}"


@click.group("device")
def device_group():
    """设备操作：列表 / 规格 / 目录 / 控制 / 状态 / 动作 / 刷新缓存。"""


# ─── device list ─────────────────────────────────────────────────────────────


@device_group.command("list")
@click.option("--room", default=None, help="按房间过滤")
@click.option("--category", default=None, help="按设备类型过滤")
@click.option("--online", is_flag=True, default=False, help="只返回在线设备")
def device_list(room, category, online):
    """列出设备（每次从后端拉取最新数据）。

    顶部单行 # home=xxx 声明家庭名，后跟 TSV：did|device_name|room|category|online
    """
    from miloco_cli.home_info import get_home_info

    info = get_home_info()
    devices = info.get("devices", [])
    if room:
        devices = [d for d in devices if d.get("room") == room]
    if category:
        devices = [d for d in devices if d.get("category") == category]
    if online:
        devices = [d for d in devices if d.get("online")]

    home = info.get("home_name") or ""
    if home:
        click.echo(f"# home={home}")
    click.echo("# did|device_name|room|category|online")
    for d in devices:
        click.echo(_render_device_row(d))


def _render_device_row(d: dict) -> str:
    """同 catalog 的设备行格式：did|device_name|room|category|online。

    每个字段都需转义 ``|``——与 catalog._escape 统一。米家 app 房间名 / 别名
    用户可改，含 ``|`` 不是不可能（如 "客厅|主卧"）。
    """
    from miloco_cli.catalog import _escape

    return "|".join([
        _escape(d.get("did")),
        _escape(d.get("name")),
        _escape(d.get("room")),
        _escape(d.get("category")),
        "online" if d.get("online") else "offline",
    ])


# ─── device spec ─────────────────────────────────────────────────────────────


@device_group.command("spec")
@click.argument("dids", nargs=-1, required=True)
def device_spec(dids):
    """查看一台或多台设备的属性和动作规格。

    \b
    单台：device spec <did>
    多台：device spec <did1> <did2> …   （各设备规格依次输出，设备之间空两行分隔）

    对齐表格：每行一个 prop / action，列宽自适应。
    """
    from miloco_cli.client import api_get

    blocks: list[str] = []
    for did in dids:
        resp = api_get(f"/api/miot/devices/{did}/spec")
        data = resp.get("data", {})
        if not data or not data.get("spec"):
            # 单条错误打到 stderr，不中断其余 did（批量时部分失败仍返回成功的）
            print(json.dumps({"error": f"did '{did}' not found or spec empty"}), file=sys.stderr)
            continue
        blocks.append(_render_device_spec(data))

    if blocks:
        click.echo("\n\n\n".join(blocks))
    else:
        sys.exit(1)


def _render_device_spec(dev: dict) -> str:
    """DEVICE 头 + PROPERTIES + ACTIONS 两段，spec 行与 catalog 同款 pipe 形式。"""
    from miloco_cli.catalog import (
        _build_spec_line,
        _resolve_keys_for_device,
    )

    spec = dev.get("spec", {}) or {}
    head_parts = [f"did={dev.get('did')}", f"device_name={dev.get('name') or '-'}"]
    if dev.get("home"):
        head_parts.append(f"home={dev['home']}")
    if dev.get("room"):
        head_parts.append(f"room={dev['room']}")
    if dev.get("category"):
        head_parts.append(f"category={dev['category']}")
    head_parts.append("online" if dev.get("online") else "offline")

    out: list[str] = ["  ".join(head_parts)]

    if not spec:
        out.append("  (spec 为空，请运行 `device refresh` 刷新缓存)")
        return "\n".join(out)

    out.append("")
    out.append("# 每行：iid  spec_name|access|format|constraint|unit（action 行：iid  spec_name|x|in_params）；字段含义同 catalog「# 数据格式」")
    out.append("# access：wr=读写 / w=只写 / r=只读 / x=动作")

    iid_to_key = _resolve_keys_for_device(spec)
    prop_rows: list[tuple[str, str]] = []
    action_rows: list[tuple[str, str]] = []
    for iid, entry in spec.items():
        if not isinstance(entry, dict):
            continue
        key = iid_to_key.get(iid) or iid
        line = _build_spec_line(iid, entry, key)
        (action_rows if line.is_action else prop_rows).append((iid, line.render()))

    def _section(title: str, rows: list[tuple[str, str]]) -> None:
        if not rows:
            return
        out.append("")
        out.append(f"{title} ({len(rows)}):")
        width = max(len(iid) for iid, _ in rows)
        for iid, rendered in rows:
            out.append(f"  {iid:<{width}}  {rendered}")

    _section("properties", prop_rows)
    _section("actions", action_rows)

    return "\n".join(out)


# ─── device catalog ──────────────────────────────────────────────────────────


@device_group.command("catalog")
@click.option(
    "--cap", default=50, show_default=True, type=int, help="设备数上限"
)
@click.option(
    "--capacity", default=7, show_default=True, type=int, help="每设备 LRU buffer 容量"
)
@click.option(
    "--no-whitelist", is_flag=True, default=False, help="禁用白名单过滤（输出全量 spec）"
)
@click.option(
    "--token-budget", default=5000, show_default=True, type=int, help="目录 token 预算"
)
def device_catalog(cap, capacity, no_whitelist, token_budget):
    """生成 TSV 设备目录，供 plugin 注入到 system prompt。"""
    from miloco_cli.catalog import build_catalog, load_whitelist
    from miloco_cli.home_info import get_home_info

    info = get_home_info()
    whitelist = set() if no_whitelist else load_whitelist()
    result = build_catalog(
        info,
        whitelist=whitelist,
        cap=cap,
        capacity=capacity,
        token_budget=token_budget,
    )
    sys.stdout.write(result.text)


# ─── device refresh ───────────────────────────────────────────────────────────


@device_group.command("refresh")
@click.option("--pretty", is_flag=True)
def device_refresh(pretty):
    """从后端拉取最新 home_info（触发后端刷新设备/摄像头/场景）。"""
    from miloco_cli.home_info import get_home_info

    info = get_home_info(refresh=True)
    print_result({
        "code": 0,
        "message": "home_info refreshed",
        "devices": len(info.get("devices", [])),
    }, pretty)


# ─── device control ───────────────────────────────────────────────────────────


@device_group.command("control")
@click.argument("did")
@click.argument("iid", required=False, default=None)
@click.argument("value", required=False, default=None)
@click.option(
    "--set",
    "sets",
    nargs=2,
    multiple=True,
    metavar="IID VALUE",
    help="批量设置属性，可重复。例：--set brightness 50 --set color-temperature 4000",
)
@click.option("--pretty", is_flag=True)
def device_control(did, iid, value, sets, pretty):
    """控制设备属性。

    \b
    单属性简写：miloco-cli device control <did> <spec_name> <value>
    批量设置：  miloco-cli device control <did> --set brightness 50 --set color-temperature 4000

    \b
    第二参数 ``spec_name`` 直接复制 ``device catalog`` / ``device spec`` 输出的 spec_name 即可。
    常见两类：
      - ``spec_name``：如 ``on`` / ``brightness`` / ``play-text``。多键 / 多组件
        设备同名冲突时 catalog 会自动带 ``@<service_description>`` 后缀消歧
        （如 ``on@左键`` / ``on@Switch_2``）。
      - ``prop.{siid}.{piid}`` / ``action.{siid}.{piid}``：iid 形态，向后兼容。

    value 自动推断类型：true/false/0/1/on/off/yes/no → bool；纯数字 → number；其余 → string。
    校验失败时自动刷新缓存并重试一次。
    """
    from miloco_cli.home_info import infer_value

    if sets and (iid or value is not None):
        print(
            json.dumps({"error": "cannot mix positional iid/value with --set"}),
            file=sys.stderr,
        )
        sys.exit(1)

    if sets:
        properties = [{"iid": k, "value": infer_value(v)} for k, v in sets]
    elif iid and value is not None:
        properties = [{"iid": iid, "value": infer_value(value)}]
    else:
        print(
            json.dumps({"error": "provide <iid> <value> or --set IID VALUE"}),
            file=sys.stderr,
        )
        sys.exit(1)

    _do_control(did, properties, pretty)


def _do_control(did: str, properties: list[dict], pretty: bool) -> None:
    from miloco_cli.client import api_post
    from miloco_cli.home_info import (
        ValidationError,
        get_home_info,
        lookup_iid_by_key,
        normalize_bool,
        validate_did,
        validate_iid,
        validate_value,
    )

    try:
        info = get_home_info()
        validate_did(did, info)
        # 第二参数 → 真实 iid（type_name / @desc / @s.p / 原 iid 都支持）。
        # 用新 list 而非就地修改 properties，否则 retry 时已被覆写的 prop["iid"]
        # 会形成不一致状态。
        resolved_properties: list[dict] = []
        for prop in properties:
            resolved_iid = lookup_iid_by_key(did, prop["iid"], info)
            # action 不能用 control 调用：明确导向 device action，别让后端报晦涩错误
            if resolved_iid.startswith("action."):
                raise ValidationError(
                    f"'{prop['iid']}' is an action, not a controllable property; "
                    f"control 不能调用动作。请改用："
                    f"miloco-cli device action {did} {prop['iid']} [值...]"
                )
            iid_spec = validate_iid(did, resolved_iid, info)
            value = prop["value"]
            # bool 兼容
            if iid_spec.get("format") == "bool":
                value = normalize_bool(value)
            validate_value(iid_spec, value)
            resolved_properties.append({"iid": resolved_iid, "value": value})
    except ValidationError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)

    if len(resolved_properties) == 1:
        body = {
            "type": "set_property",
            "iid": resolved_properties[0]["iid"],
            "value": resolved_properties[0]["value"],
        }
    else:
        body = {"type": "set_properties", "properties": resolved_properties}

    data = api_post(f"/api/miot/devices/{did}/control", body)
    # 后端返回体不含 did；并发批量控制（&+wait）时多行输出交错，补 did 让结果可归属
    if isinstance(data.get("data"), dict):
        data["data"]["did"] = did
    _annotate_result_codes(data)
    print_result(data, pretty)


# ─── device props ─────────────────────────────────────────────────────────────


@device_group.command("props")
@click.argument("did")
@click.argument("iids", nargs=-1)
@click.option("--pretty", is_flag=True)
def device_status(did, iids, pretty):
    """查询设备属性值。

    第二参数 ``spec_name`` 形态见 ``device control --help``。不传则返回全部可读属性。
    """
    from miloco_cli.catalog import _resolve_keys_for_device
    from miloco_cli.client import api_get
    from miloco_cli.home_info import (
        ValidationError,
        get_home_info,
        lookup_iid_by_key,
        validate_did,
    )

    params = {}
    info = None
    if iids:
        try:
            info = get_home_info()
            resolved_iids = [lookup_iid_by_key(did, k, info) for k in iids]
        except ValidationError as e:
            print(json.dumps({"error": str(e)}), file=sys.stderr)
            sys.exit(1)
        params["iid"] = ",".join(resolved_iids)

    data = api_get(f"/api/miot/devices/{did}/status", params or None)
    if isinstance(data.get("data"), dict):
        # 后端返回体不含 did；并发批量查询（&+wait）时输出交错，补 did 让结果可归属
        data["data"]["did"] = did
        # 后端按 iid（prop.s.p）返回，外部无法把值关联到属性 → 补 spec_name（= control/props 用的第2参数）
        props = data["data"].get("properties")
        if isinstance(props, list) and props:
            try:
                spec = validate_did(did, info or get_home_info()).get("spec") or {}
                iid_to_key = _resolve_keys_for_device(spec)
            except Exception:
                iid_to_key = {}
            for p in props:
                if isinstance(p, dict) and p.get("iid"):
                    p["spec_name"] = iid_to_key.get(p["iid"], p["iid"])
    _annotate_result_codes(data)
    print_result(data, pretty)


# ─── device action ────────────────────────────────────────────────────────────


@device_group.command("action")
@click.argument("did")
@click.argument("iid")
@click.argument("params", nargs=-1)
@click.option("--pretty", is_flag=True)
def device_action(did, iid, params, pretty):
    """调用设备动作（call_action），主要用于音箱 TTS 播报。

    第二参数 ``spec_name`` 形态见 ``device control --help``。
    """
    from miloco_cli.client import api_post
    from miloco_cli.home_info import (
        ValidationError,
        get_home_info,
        infer_value,
        lookup_iid_by_key,
    )

    try:
        info = get_home_info()
        resolved_iid = lookup_iid_by_key(did, iid, info)
    except ValidationError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)

    data = api_post(
        f"/api/miot/devices/{did}/control",
        {
            "type": "call_action",
            "iid": resolved_iid,
            "params": [infer_value(p) for p in params],
        },
    )
    # 后端返回体不含 did；并发批量 action（&+wait）时多行输出交错，补 did 让结果可归属
    if isinstance(data.get("data"), dict):
        data["data"]["did"] = did
    _annotate_result_codes(data)
    print_result(data, pretty)
