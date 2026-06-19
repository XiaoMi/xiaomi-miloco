from hermes import schemas


# --------------------------------------------------------------- miloco_im_push

def test_im_push_name():
    assert schemas.MILOCO_IM_PUSH["name"] == "miloco_im_push"


def test_im_push_parameters():
    params = schemas.MILOCO_IM_PUSH["parameters"]["properties"]
    assert params["message"]["type"] == "string"
    assert params["bindHint"]["type"] == "string"


def test_im_push_required():
    assert schemas.MILOCO_IM_PUSH["parameters"]["required"] == ["message"]


def test_im_push_has_type_function():
    assert schemas.MILOCO_IM_PUSH["type"] == "function"
    assert isinstance(schemas.MILOCO_IM_PUSH["description"], str)
    assert schemas.MILOCO_IM_PUSH["description"]  # non-empty


# ----------------------------------------------------------- miloco_notify_bind

def test_notify_bind_name():
    assert schemas.MILOCO_NOTIFY_BIND["name"] == "miloco_notify_bind"


def test_notify_bind_parameters():
    params = schemas.MILOCO_NOTIFY_BIND["parameters"]["properties"]
    assert params["sessionKey"]["type"] == "string"


def test_notify_bind_optional():
    assert schemas.MILOCO_NOTIFY_BIND["parameters"]["required"] == []


def test_notify_bind_description():
    assert schemas.MILOCO_NOTIFY_BIND["type"] == "function"
    assert schemas.MILOCO_NOTIFY_BIND["description"]


# --------------------------------------------------------- miloco_habit_suggest

def test_habit_suggest_name():
    assert schemas.MILOCO_HABIT_SUGGEST["name"] == "miloco_habit_suggest"


def test_habit_suggest_action_required_and_enum():
    params = schemas.MILOCO_HABIT_SUGGEST["parameters"]
    action = params["properties"]["action"]
    assert action["type"] == "string"
    assert action["enum"] == ["list", "record", "mark_asked", "resolve"]
    assert params["required"] == ["action"]


def test_habit_suggest_optional_fields():
    props = schemas.MILOCO_HABIT_SUGGEST["parameters"]["properties"]
    for field in (
        "key", "subject", "habit", "suggestion", "title", "evidence",
        "item_id", "task_id", "reason",
    ):
        assert field in props
    outcome = props["outcome"]
    assert outcome["enum"] == ["accepted", "rejected", "created"]


def test_habit_suggest_optional_none_required_except_action():
    required = schemas.MILOCO_HABIT_SUGGEST["parameters"]["required"]
    assert required == ["action"]


def test_habit_suggest_type_and_description():
    assert schemas.MILOCO_HABIT_SUGGEST["type"] == "function"
    assert schemas.MILOCO_HABIT_SUGGEST["description"]
