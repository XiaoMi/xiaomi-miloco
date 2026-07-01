"""pet 命令组：宠物花名册 CRUD（调 ``/api/identity/pets``）。

宠物作为非人家庭成员管理。``observe``（上传媒体自动生成外观描述）走 multipart，
主要在 web / Agent 侧使用，CLI 暂不含。
"""

import json
import sys

import click

from miloco_cli.output import print_result


@click.group("pet")
def pet_group():
    """宠物：列表 / 添加 / 更新 / 删除（作为非人家庭成员管理）。"""


@pet_group.command("list")
@click.option("--pretty", is_flag=True)
def pet_list(pretty):
    """列出所有宠物。"""
    from miloco_cli.client import api_get

    data = api_get("/api/identity/pets")
    print_result(data, pretty)


@pet_group.command("add")
@click.option("--name", required=True, help="宠物名（唯一）")
@click.option("--species", default="", help="物种：猫 / 狗 / 其他")
@click.option("--pretty", is_flag=True)
def pet_add(name, species, pretty):
    """添加一只宠物。"""
    from miloco_cli.client import api_post

    data = api_post("/api/identity/pets", {"name": name, "species": species})
    print_result(data, pretty)


@pet_group.command("update")
@click.argument("pet_id")
@click.option("--name", default=None, help="新名字")
@click.option("--species", default=None, help="新物种")
@click.option("--pretty", is_flag=True)
def pet_update(pet_id, name, species, pretty):
    """更新宠物信息（部分更新）。"""
    from miloco_cli.client import api_patch

    payload: dict = {}
    if name is not None:
        payload["name"] = name
    if species is not None:
        payload["species"] = species
    if not payload:
        print(json.dumps({"error": "no fields to update"}), file=sys.stderr)
        sys.exit(1)

    data = api_patch(f"/api/identity/pets/{pet_id}", payload)
    print_result(data, pretty)


@pet_group.command("delete")
@click.argument("pet_id")
@click.option("--pretty", is_flag=True)
def pet_delete(pet_id, pretty):
    """删除宠物（后端联动清理家庭档案中绑定该宠物的条目 + 头像）。"""
    from miloco_cli.client import api_delete

    data = api_delete(f"/api/identity/pets/{pet_id}")
    print_result(data, pretty)
