"""pet 命令组：宠物花名册 CRUD + 富注册（observe / reference-crops）。

CRUD 调 ``/api/identity/pets``；``observe``（上传图/视频→门控候选 crop + 共性外观描述，
无副作用）与 ``reference-crops``（存多姿态参考图，喂 ③ 识别）走 multipart，供 Agent
宠物注册 skill（miloco-miot-pet-register）端到端使用。
"""

import base64
import json
import mimetypes
import re
import sys
from pathlib import Path

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


@pet_group.command("observe")
@click.option("--image", "image_path", type=click.Path(exists=True), default=None,
              help="单张图（--image / --video / --images 三选一）")
@click.option("--video", "video_path", type=click.Path(exists=True), default=None,
              help="单段视频（后端 SORT 选帧，禁客户端抽帧）")
@click.option("--images", "image_paths_multi", type=click.Path(exists=True), multiple=True,
              help="多张图 1~3（可重复：--images a.jpg --images b.jpg）")
@click.option("--grounding/--no-grounding", "grounding", default=None,
              help="是否头部 grounding；缺省取后端 features.pet_head_grounding")
@click.option("--save-crops", "save_crops", type=click.Path(), default=None,
              help="把候选参考 crop 存到 <prefix>_<i>.jpg（+ _primary.jpg），"
                   "供确认后喂 pet reference-crops；给路径后响应里的 base64 省略")
@click.option("--pretty", is_flag=True)
def pet_observe(image_path, video_path, image_paths_multi, grounding, save_crops, pretty):
    """上传图（1~3 张）/视频（1 个）→ 后端门控出 ≤3 张同一只候选 crop + omni 共性外观描述。

    \b
    无副作用、不落库；用于宠物富注册第 1 步（观察出候选）：
      1) pet observe --images a.jpg b.jpg --save-crops /tmp/petobs → 拿候选 crop 文件 + 描述 + warnings
      2) 把候选图 + 描述发用户确认
      3) 确认后：pet add（若新）+ pet reference-crops <id> --crops /tmp/petobs_0.jpg ... --scores ...
    """
    from miloco_cli.client import api_post_multipart

    src_count = sum([bool(image_path), bool(video_path), bool(image_paths_multi)])
    if src_count != 1:
        print(json.dumps({"error": "--image / --video / --images 三选一,有且仅传一个"}),
              file=sys.stderr)
        sys.exit(1)
    if image_paths_multi and len(image_paths_multi) > 3:
        print(json.dumps({"error": "最多 3 张图片"}), file=sys.stderr)
        sys.exit(1)

    paths = ([image_path] if image_path else
             [video_path] if video_path else list(image_paths_multi))
    files = []
    for p in paths:
        ct = mimetypes.guess_type(p)[0] or "application/octet-stream"
        files.append(("medias", (Path(p).name, Path(p).read_bytes(), ct)))
    data: dict = {}
    if grounding is not None:
        data["grounding"] = "true" if grounding else "false"

    resp = api_post_multipart("/api/identity/pets:observe", files, data)

    # 存候选 crop 到本地 + 从 stdout 清掉冗长 base64（同 identity register preview 范式）。
    if save_crops:
        d = (resp.get("data") or {}).copy()
        cands = d.get("candidates") or []
        saved = []
        for i, c in enumerate(cands):
            b64 = c.get("crop_b64")
            if not b64:
                continue
            outp = f"{save_crops}_{i}.jpg"
            Path(outp).write_bytes(base64.b64decode(b64))
            # 绝对质量分 = conf × sharpness × area_ratio（喂 reference-crops --scores）
            score = (c.get("conf") or 0) * (c.get("sharpness") or 0) * (c.get("area_ratio") or 0)
            saved.append({
                "index": i, "path": outp, "score": round(float(score), 6),
                "species_guess": c.get("species_guess"),
            })
        d["crops_saved"] = saved
        if d.get("primary_crop_b64"):
            pp = f"{save_crops}_primary.jpg"
            Path(pp).write_bytes(base64.b64decode(d["primary_crop_b64"]))
            d["primary_saved_to"] = pp
        d.pop("primary_crop_b64", None)
        d["candidates"] = [
            {k: v for k, v in c.items() if k != "crop_b64"} for c in cands
        ]
        resp = {**resp, "data": d}
    print_result(resp, pretty)


@pet_group.command("reference-crops")
@click.argument("pet_id")
@click.option("--crops", "crop_paths", type=click.Path(exists=True), multiple=True,
              required=True, help="参考 crop 文件（可重复 --crops，≤3；通常来自 observe --save-crops）")
@click.option("--scores", default=None,
              help="与 --crops 对齐的绝对质量分，逗号/空格分隔（缺省补 0）")
@click.option("--mode", type=click.Choice(["replace", "append"]), default="replace",
              help="replace=整组替换（注册）/ append=追加（按绝对分留 top-3，补充素材用）")
@click.option("--pretty", is_flag=True)
def pet_reference_crops(pet_id, crop_paths, scores, mode, pretty):
    """存客户端已裁好的参考 crop（③ 多姿态参照图）。服务端只存不裁。

    replace 整组替换（注册时一次性写 ≤3）；append 追加，与现有合并后按绝对分留 top-3。
    """
    from miloco_cli.client import api_post_multipart

    if mode == "replace" and len(crop_paths) > 3:
        print(json.dumps({"error": "replace 最多 3 张参考 crop"}), file=sys.stderr)
        sys.exit(1)
    files = [
        ("crops", (Path(p).name, Path(p).read_bytes(), "image/jpeg")) for p in crop_paths
    ]
    data: dict = {"mode": mode}
    if scores:
        parts = [x for x in re.split(r"[,\s]+", scores.strip()) if x]
        try:
            data["scores"] = [str(float(x)) for x in parts]  # 校验数字 + 规范化字符串
        except ValueError:
            print(json.dumps({"error": f"--scores 必须是数字（逗号/空格分隔）: {scores!r}"}),
                  file=sys.stderr)
            sys.exit(1)

    resp = api_post_multipart(f"/api/identity/pets/{pet_id}/reference-crops", files, data)
    print_result(resp, pretty)
