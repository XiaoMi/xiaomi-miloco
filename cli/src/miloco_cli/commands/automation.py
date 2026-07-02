"""automation 命令组：米家事件自动化映射管理。"""

import json
import sys

import click

from miloco_cli.output import print_result

API_PREFIX = "/api/automation"


@click.group("automation")
def automation_group():
    """米家事件自动化：映射列表 / 新增 / 删除 / 测试触发 / 源目录。"""


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------


@automation_group.command("catalog")
@click.option("--pretty", is_flag=True)
def automation_catalog(pretty):
    """列出自动化源目录（设备 + 摄像头）。"""
    from miloco_cli.client import api_get

    data = api_get(f"{API_PREFIX}/catalog")
    print_result(data, pretty)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@automation_group.command("list")
@click.option("--pretty", is_flag=True)
def automation_list(pretty):
    """列出所有米家事件映射。"""
    from miloco_cli.client import api_get

    data = api_get(f"{API_PREFIX}/mappings")
    print_result(data, pretty)


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


@automation_group.command("add")
@click.option("--source-id", "source_id", required=True, help="触发设备 did")
@click.option("--source-name", "source_name", default="", help="触发设备名称快照")
@click.option(
    "--camera",
    "camera_dids",
    multiple=True,
    required=True,
    help="关联摄像头 did（可重复，至少一个）",
)
@click.option(
    "--event-kind",
    "event_kinds",
    multiple=True,
    help="事件 kind（可重复，如 event.xxx 或 device_prop）。不填默认 device_prop",
)
@click.option(
    "--property-filter",
    "property_filters_raw",
    multiple=True,
    help='属性过滤 JSON（可重复，如 \'{"on":true}\'）',
)
@click.option("--query-template", "query_template", default="", help="感知提示模板")
@click.option(
    "--cooldown-seconds",
    "cooldown_seconds",
    type=int,
    default=30,
    show_default=True,
    help="冷却秒数",
)
@click.option("--notes", "notes", default="", help="备注")
@click.option("--disabled", is_flag=True, help="创建为禁用状态")
@click.option("--pretty", is_flag=True)
def automation_add(
    source_id,
    source_name,
    camera_dids,
    event_kinds,
    property_filters_raw,
    query_template,
    cooldown_seconds,
    notes,
    disabled,
    pretty,
):
    """新增米家事件映射。"""
    from miloco_cli.client import api_post

    property_filters: dict = {}
    for raw in property_filters_raw:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            _exit_error(f"invalid --property-filter JSON: {e}")
        if not isinstance(obj, dict):
            _exit_error(f"--property-filter must be a JSON object, got: {raw}")
        property_filters.update(obj)

    payload = {
        "source_type": "device",
        "source_id": source_id,
        "source_name_snapshot": source_name,
        "camera_dids": list(camera_dids),
        "enabled": not disabled,
        "query_template": query_template,
        "event_kinds": list(event_kinds) if event_kinds else ["device_prop"],
        "property_filters": property_filters,
        "cooldown_seconds": cooldown_seconds,
        "notes": notes,
    }
    data = api_post(f"{API_PREFIX}/mappings", payload)
    print_result(data, pretty)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@automation_group.command("delete")
@click.argument("mapping_id")
@click.option("--pretty", is_flag=True)
def automation_delete(mapping_id, pretty):
    """删除米家事件映射。"""
    from miloco_cli.client import api_delete

    data = api_delete(f"{API_PREFIX}/mappings/{mapping_id}")
    print_result(data, pretty)


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


@automation_group.command("test")
@click.option("--source-id", "source_id", required=True, help="触发设备 did")
@click.option("--source-name", "source_name", default="", help="触发设备名称")
@click.option(
    "--event-name",
    "event_name",
    default="",
    help="事件名（如 event.xxx 或 device_prop）",
)
@click.option(
    "--prop",
    "changed_properties_raw",
    multiple=True,
    help='变更属性 JSON（可重复，如 \'{"on":true}\'）',
)
@click.option("--pretty", is_flag=True)
def automation_test(source_id, source_name, event_name, changed_properties_raw, pretty):
    """手动测试触发米家事件自动化。"""
    from miloco_cli.client import api_post

    changed_properties: dict = {}
    for raw in changed_properties_raw:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            _exit_error(f"invalid --prop JSON: {e}")
        if not isinstance(obj, dict):
            _exit_error(f"--prop must be a JSON object, got: {raw}")
        changed_properties.update(obj)

    payload = {
        "source_type": "device",
        "source_id": source_id,
        "source_name": source_name,
        "event_name": event_name,
        "changed_properties": changed_properties,
    }
    data = api_post(f"{API_PREFIX}/test-trigger", payload)
    print_result(data, pretty)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _exit_error(msg: str):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(1)
