"""MIoT spec 云端错误码 → 中文释义,及一个 results/result 归一判定 helper。

后端在 control_device / trigger_scene 落 action_ledger 时,用 ``summarize_results``
把 proxy 返回体(复数 ``results`` 或单数 ``result``)归一成 (success, worst_code, msg)。

判定规则(镜像 PR #394 语义):**只有负码算失败** —— 外层 ``code=0`` 只表示 RPC 送达,
逐条结果的 ``code`` 才是设备侧执行结果;``isinstance(code, int) and code < 0 and code not
in _MIOT_OK_CODES`` 才判失败,其余(0 / 正码 / 非 int / 缺失)一律视作成功。

注意:CLI 侧 ``cli/src/miloco_cli/commands/device.py`` 另有一份 ``_MIOT_SPEC_CODES`` 副本用于
stdout 渲染(独立轮子,CLI 不能 import backend)。两份需手动保持同步(tests/test_result_codes.py 的跨包一致性测试会在两表漂移时变红)。
"""
from __future__ import annotations

# 设备侧云端错误码 → 中文释义。来源:米家 spec 错误码表。
# (与 CLI device.py::_MIOT_SPEC_CODES 逐条对齐,改一处两处都要改。)
_MIOT_SPEC_CODES: dict[int, str] = {
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

# 设备侧"成功"码:0(后端归一) + MIoT 原始 OK/accept,不当失败处理。
_MIOT_OK_CODES = frozenset({0, -702000000, -702010000})

_UNKNOWN_FAIL_MSG = "设备侧执行失败（未知错误码，详见米家 spec 错误码表）"


def code_message(code: int) -> str:
    """单个失败码 → 中文释义(未知码给通用文案)。"""
    return _MIOT_SPEC_CODES.get(code, _UNKNOWN_FAIL_MSG)


def _is_failure(code: object) -> bool:
    """负码即失败,其余(0 / 正码 / 非 int / None)一律成功。镜像 PR #394。"""
    return isinstance(code, int) and code < 0 and code not in _MIOT_OK_CODES


def summarize_results(
    results_or_result: object,
) -> tuple[bool, int | None, str | None]:
    """把 proxy 返回体归一成 ``(success, worst_code, msg)``。

    接受三种形态:
      - ``list[dict]``   —— set_property / set_properties 的 ``results``
      - ``dict``         —— call_action 的单数 ``result``
      - 其他/None        —— 无从判定,按成功处理(success=True, code/msg=None)

    ``worst_code``:第一个失败项的 code(逐条扫描,取首个负码);全成功则 None。
    ``msg``:worst_code 的中文释义;全成功则 None。
    """
    if isinstance(results_or_result, dict):
        items = [results_or_result]
    elif isinstance(results_or_result, list):
        items = [it for it in results_or_result if isinstance(it, dict)]
    else:
        return True, None, None

    for it in items:
        code = it.get("code")
        if _is_failure(code):
            return False, code, code_message(code)  # type: ignore[arg-type]

    return True, None, None
