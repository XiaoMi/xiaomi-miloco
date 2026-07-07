"""miot.result_codes.summarize_results:负码即失败(镜像 PR #394)。"""

from miloco.miot.result_codes import summarize_results


def test_all_zero_codes_is_success():
    assert summarize_results([{"code": 0}, {"code": 0}]) == (True, None, None)


def test_single_result_dict_success():
    assert summarize_results({"code": 0}) == (True, None, None)


def test_negative_code_is_failure_decoded():
    ok, code, msg = summarize_results([{"code": -704042011}])
    assert ok is False
    assert code == -704042011
    assert msg == "设备离线"


def test_positive_code_not_failure():
    # 正码不判失败(只有负码算失败)
    assert summarize_results([{"code": 12345}]) == (True, None, None)


def test_miot_ok_negative_codes_not_failure():
    # -702000000 / -702010000 在 OK 集里,不判失败
    assert summarize_results([{"code": -702000000}]) == (True, None, None)


def test_missing_code_is_success():
    assert summarize_results([{"siid": 2, "piid": 1}]) == (True, None, None)


def test_first_failure_wins():
    ok, code, msg = summarize_results(
        [{"code": 0}, {"code": -704030023}, {"code": -704042011}]
    )
    assert ok is False
    assert code == -704030023  # 第一个失败项
    assert msg == "属性不可写"


def test_unknown_negative_code_gets_generic_msg():
    ok, code, msg = summarize_results([{"code": -799999999}])
    assert ok is False
    assert code == -799999999
    assert "未知错误码" in msg


def test_none_input_is_success():
    assert summarize_results(None) == (True, None, None)
