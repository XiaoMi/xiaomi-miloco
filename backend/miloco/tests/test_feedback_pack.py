"""feedback_pack 核心逻辑单测."""

from miloco.admin.feedback_pack import _sanitize_pii


def test_sanitize_pii_masks_phone():
    assert _sanitize_pii("手机号13800138000结束") == "手机号***结束"


def test_sanitize_pii_masks_ip():
    assert _sanitize_pii("地址192.168.1.1端口") == "地址***端口"


def test_sanitize_pii_masks_idcard():
    assert _sanitize_pii("身份证11010519491231002X号") == "身份证***号"


def test_sanitize_pii_masks_all_three():
    text = "13800138000 192.168.1.1 11010519491231002X"
    assert _sanitize_pii(text) == "*** *** ***"


def test_sanitize_pii_preserves_short_numbers():
    assert _sanitize_pii("token数1350万") == "token数1350万"


def test_sanitize_pii_preserves_model_version():
    assert _sanitize_pii("mimo-v2.5.1.0模型") == "mimo-v2.5.1.0模型"
