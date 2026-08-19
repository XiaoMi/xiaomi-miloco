from miloco.miot.result_codes import is_known_code


def test_known_code_is_recognized():
    assert is_known_code(-704220043) is True


def test_unknown_code_is_not():
    assert is_known_code(-704010000) is False


def test_success_code_is_not_a_failure_code():
    """0 不是失败码，也就不该被当成「认识的失败码」。"""
    assert is_known_code(0) is False
