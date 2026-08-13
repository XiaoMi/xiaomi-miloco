"""identity 命令组:身份库 + 陌生人池 + 注册流程 + 算法 CLI。

v1.2 设计:与 `person` 子组分开——`person` 管 DB 行(轻量 CRUD);本 `identity`
子组管样本 + 抽取 / 筛选算法 + 合并拆分 + 注册流程。详见 plan §11.1。

子组划分:
    identity member ...   身份层级(合并 / 拆分 / list / show)
    identity sample ...   单张样本(增删 / backfill-emb)
    identity pool ...     陌生人池(PR 7 接入,本期仅 stub)
    identity register ... 注册流程(preview / commit / sessions / rollback / from-* )
    identity extract      抽取算法独立入口
    identity select       筛选算法独立入口
"""

import base64
import json
import sys
from pathlib import Path

import click

# 视频后缀白名单 — `--image` 收到这些扩展名时直接报错指向 `--video`,纠正 LLM
# agent "视频抽帧 → 当图喂 --image" 的旧惯性走法。覆盖常见家用录制格式。
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v", ".3gp"}


# ISO BMFF(``ftyp`` 盒)里属于**静态图片**的品牌。HEIF/AVIF 与 mp4/mov 共用同一套容器结构,
# 只看「字节 4..8 == ftyp」会把 iPhone 的 HEIC 判成视频 —— 而这个误判会连锁成灾:报错文案
# 把 agent 引到 `--video`,服务端不嗅内容直接按视频抽帧,ffmpeg 又只暴露 HEIC 的 512x512
# 瓦片而不拼接 grid,最终静默拿一块瓦片当整帧、返回"没识别到人"。故必须按 brand 区分。
# 与后端 identity/_image_utils._HEIF_BRANDS 同口径(两侧各自独立进程,无法共享常量)。
_STILL_IMAGE_BRANDS = {
    b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs",
    b"mif1", b"msf1", b"miaf", b"mia1",
    b"avif", b"avis",
}


def _looks_like_video_bytes(head: bytes) -> str | None:
    """看文件头 magic number 判断是否视频(防 agent 把 .mp4 改成 .jpg 后缀伪装绕过)。

    返回识别到的格式名,或 None(不是视频)。检测:
    - ISO BMFF (mp4/mov/m4v/3gp):字节 4..8 == "ftyp"**且 brand 不是静态图片家族**
    - Matroska / WebM:开头 \\x1A\\x45\\xDF\\xA3 (EBML)
    - AVI:RIFF....AVI
    """
    if not head or len(head) < 8:
        return None
    if head[4:8] == b"ftyp":
        if len(head) >= 12 and head[8:12] in _STILL_IMAGE_BRANDS:
            return None  # HEIC/HEIF/AVIF 是**图片**,该走 --image
        return "mp4/mov"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska/webm"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "avi"
    return None


def _reject_video_misuse(image_path: str | None) -> None:
    """如果 --image 收到视频或视频衍生帧,报错引导改用 --video <原视频>。

    给 agent 用的 self-correct 钩子。三层检测覆盖 LLM 三种典型走错方式:

    1. **直接喂 .mp4 / .mov 等视频文件** → 后缀 catch
    2. **把视频改图片后缀(.jpg / .png)伪装** → magic number catch
    3. **ffmpeg 抽帧 → 喂 'xxx.mp4.png' 等带视频中间扩展的衍生帧** → 文件名子串 catch
       这是观察到的实际 agent 惯性走法(原 SKILL 没接通 --video 时的 workaround)

    第 3 种 catch 的 trade-off:误杀概率 — 普通用户 jpg/png 文件名极少含 ".mp4."
    这种子串;agent ffmpeg 默认输出却几乎必然以 "<原视频名>.<png|jpg>" 命名。
    """
    if not image_path:
        return
    path = Path(image_path)
    ext = path.suffix.lower()

    # 1) 后缀直接是视频(最常见,无 IO 最快)
    if ext in _VIDEO_EXTS:
        reason = f"文件后缀 '{ext}' 是视频格式"
        hint_path = image_path
    else:
        # 2) 文件名内层扩展含视频后缀(ffmpeg 抽帧的命名模式 "xxx.mp4.png")
        #    `Path.stem` 把最外层扩展去掉,如 "xxx.mp4.png" → stem="xxx.mp4"
        #    再 split,取最后一段 → "mp4"
        stem_parts = path.stem.split(".")
        inner = stem_parts[-1].lower() if len(stem_parts) >= 2 else ""
        if inner and ("." + inner) in _VIDEO_EXTS:
            reason = (
                f"文件名 '{path.name}' 含视频扩展子串 '.{inner}.',"
                f"看起来是从视频抽帧的衍生帧"
            )
            # hint 路径取去掉外层 .png/.jpg 的视频原路径(ffmpeg 抽帧约定)
            hint_path = str(path.with_suffix(""))
        else:
            # 3) magic number(扩展名被改成图片后缀的伪装)
            try:
                head = path.open("rb").read(16)
            except OSError:
                return
            fmt = _looks_like_video_bytes(head)
            if not fmt:
                return
            reason = f"文件头 magic number 显示是视频格式 ({fmt})"
            hint_path = image_path
    print(json.dumps({
        "error": (
            f"{reason}。本流程**禁止视频抽帧再用 --image**(只会拿到 1 张样本,"
            f"丢失 DeepSORT 多 track 关联 + 多帧差异化采样)。请改用 preview"
            f"(两步走,用户确认后再 commit):\n"
            f"  miloco-cli identity register preview --video {hint_path} --topk 5 --pretty\n"
            f"⚠️ 不要用 from-media(agent 必须走 preview → 用户确认 → commit)"
        ),
    }, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)


@click.group("identity")
def identity_group():
    """身份库 + 陌生人池 + 注册流程 + 算法 CLI。"""


# =============================================================================
# member 子组
# =============================================================================


@identity_group.group("member")
def member_group():
    """成员管理(身份层级)。"""


@member_group.command("list")
@click.option("--pretty", is_flag=True)
def member_list(pretty):
    """列所有成员(走 /identity/persons)。"""
    from miloco_cli.client import api_get
    from miloco_cli.output import print_result
    data = api_get("/api/identity/persons")
    print_result(data, pretty)


@member_group.command("merge")
@click.option("--target", "target_id", required=True, help="保留的目标 person_id")
@click.option("--source", "sources", multiple=True, required=True,
                help="合并并删除的 source person_id,可多次给")
@click.option("--pretty", is_flag=True)
def member_merge(target_id, sources, pretty):
    """合并 sources → target;source 样本并入,DB 行 + 目录删除。"""
    from miloco_cli.client import api_post
    from miloco_cli.output import print_result
    data = api_post("/api/identity/persons/merge",
                     {"target_id": target_id, "source_ids": list(sources)})
    print_result(data, pretty)


@member_group.command("split")
@click.argument("source_id")
@click.option("--new-name", required=True, help="拆出来的新人物真名(必填唯一)")
@click.option("--new-role", default=None, help="新人物的家庭角色(可选,如 爸爸/妈妈)")
@click.option("--by-session", "by_session", multiple=True,
                help="按 register_session_id 拆,可多次")
@click.option("--by-cluster", "by_cluster", multiple=True,
                help="按 cluster_id 拆")
@click.option("--by-cam", "by_cam", multiple=True, help="按 camera_id 拆")
@click.option("--pretty", is_flag=True)
def member_split(source_id, new_name, new_role, by_session, by_cluster, by_cam, pretty):
    """从指定 person 拆出部分样本到新 person。"""
    from miloco_cli.client import api_post
    from miloco_cli.output import print_result
    payload = {
        "new_name": new_name,
        "new_role": new_role,
        "selector_session_ids": list(by_session) or None,
        "selector_cluster_ids": list(by_cluster) or None,
        "selector_cam_ids": list(by_cam) or None,
    }
    data = api_post(f"/api/identity/persons/{source_id}/split", payload)
    print_result(data, pretty)


@member_group.command("delete")
@click.argument("person_id")
@click.option("--pretty", is_flag=True)
def member_delete(person_id, pretty):
    """删成员(级联清 identity_lib 目录)。"""
    from miloco_cli.client import api_delete
    from miloco_cli.output import print_result
    data = api_delete(f"/api/identity/persons/{person_id}")
    print_result(data, pretty)


# =============================================================================
# sample 子组(身份库样本管理)
# =============================================================================


@identity_group.group("sample")
def sample_group():
    """身份库样本管理(增删 / backfill-emb / show)。"""


@sample_group.command("show")
@click.option("--person", "person_id", required=True, help="person_id (UUID)")
@click.option("--with-face", is_flag=True, default=False,
                help="是否在 body 下方追加 face 横排")
@click.option("--tier", type=click.Choice(["a", "c"]), default="a",
                help="tier_a(用户登记,默认)或 tier_c(omni 累积)")
@click.option("--save", "save_path", type=click.Path(), default=None,
                help="保存合并图到本地 jpg 路径;不给则返 base64 在 stdout (data 字段)")
@click.option("--pretty", is_flag=True)
def sample_show(person_id, with_face, tier, save_path, pretty):
    """**一次性**取该 person 的样本合并图(body 横排 + 可选 face 横排在下方)。

    \b
    Agent 给用户展示某人样本时,**用本命令一句话拿合并图**,
    不要反复调单图端点 + 多次飞书发图。
    \b
    返回 data 字段含:
      - image_jpeg_b64    : 合并 jpg 的 base64(--save 给定时省略)
      - body_count        : 含 body 数
      - face_count        : 该身份磁盘上 face 样本总数(与 with_face 无关,只
                            是信息字段;with_face=False 时仅不绘制 face 行)
      - width, height     : 合并图尺寸
    """
    from miloco_cli.client import api_get
    from miloco_cli.output import print_result
    data = api_get(
        f"/api/identity/persons/{person_id}/samples/montage",
        params={"with_face": str(with_face).lower(), "tier": tier},
    )
    if save_path:
        b64 = (data.get("data") or {}).get("image_jpeg_b64", "")
        if not b64:
            print(json.dumps({"error": "no montage available (no samples?)"},
                              ensure_ascii=False), file=sys.stderr)
            sys.exit(1)
        Path(save_path).write_bytes(base64.b64decode(b64))
        # 保存后从输出里删掉冗长的 base64,只留 metadata + 文件路径
        d = (data.get("data") or {}).copy()
        d.pop("image_jpeg_b64", None)
        d["saved_to"] = save_path
        data = {**data, "data": d}
    print_result(data, pretty)


@sample_group.command("backfill-emb")
@click.option("--force", is_flag=True, help="即便已有 .npy 也重新生成")
@click.option("--person", "person_ids", multiple=True, help="只扫指定 person_id")
@click.option("--pretty", is_flag=True)
def sample_backfill_emb(force, person_ids, pretty):
    """老身份库适配:给缺 .npy 的 body crop 现场抽 ReID emb 落盘。

    需要在 backend 进程内执行(直接读 identity_lib 目录 + 调 HumanReID),不走 HTTP。
    """
    # 本命令是离线工具,不通过 backend HTTP;CLI 直接 load library + HumanReID
    # 避免给 backend 加重型端点。需要 miloco backend 包可 import。
    try:
        from miloco.perception.engine.identity.config_loader import resolve_library_root
        from miloco.perception.engine.identity.library import IdentityLibrary
        from miloco.perception.engine.identity.tracker.human_reid import HumanReID
    except ImportError as e:
        print(json.dumps({"error": f"需在 miloco backend venv 内运行: {e}"}),
                file=sys.stderr)
        sys.exit(1)
    from miloco_cli.output import print_result

    lib = IdentityLibrary(resolve_library_root())
    reid = HumanReID()  # 默认 path = models/human_body_reid_v2.onnx
    result = lib.backfill_reid_embeddings(
        reid, force=force,
        person_ids=list(person_ids) or None,
    )
    print_result({"code": 0, "message": "OK", "data": result}, pretty)


# =============================================================================
# pool 子组(陌生人池)
# =============================================================================


@identity_group.group("pool")
def pool_group():
    """陌生人池:status / fetch / cluster-split。"""


@pool_group.command("status")
@click.option("--pretty", is_flag=True)
def pool_status(pretty):
    """池子状态(entry / cluster / 内存)。"""
    from miloco_cli.client import api_get
    from miloco_cli.output import print_result
    data = api_get("/api/identity/pool/status")
    print_result(data, pretty)


@pool_group.command("fetch")
@click.option("--cam", default=None, help="锁定 cam_id(配合 --track)")
@click.option("--track", type=int, default=None, help="锁定 track_id")
@click.option("--window", type=float, default=None, help="时间窗口秒数,默认 300")
@click.option("--offset", type=int, default=0,
                help="翻页起始位置(0-based);用户回'更多'时用上次响应的 "
                     "next_offset 重发即可")
@click.option("--with-crops", is_flag=True, default=False,
                help="返回 cluster 内每张 representative + per_cam 的 base64 jpg "
                     "(默认关,避免 stdout 撑大;号码图通过 --save-montage 拿)")
@click.option("--save-montage", "save_montage", type=click.Path(), default=None,
                help="把号码图(每 cluster 一张代表样本,前 6 个,标 [1] [2]...)"
                     "存到本地 jpg。agent 飞书场景必带——一次发图给用户选号,"
                     "不要拆开发多张")
@click.option("--pretty", is_flag=True)
def pool_fetch(cam, track, window, offset, with_crops, save_montage, pretty):
    """取注册候选 cluster 列表(SKILL workflow B/C 用)。

    \b
    Agent 飞书流程:
      1) miloco-cli identity pool fetch --window 300 --save-montage /tmp/p.jpg --pretty
      2) 发 /tmp/p.jpg 给用户 + 文字"近 X 分钟看到 N 组陌生人(图中编号),
         给「<name>」选哪一组?回数字 1/2/..."
      3) 用户回 "1" → tracks[0].cluster_id → identity register from-cluster
      4) 用户回 "更多" → 用上次响应的 next_offset 重发:
         miloco-cli identity pool fetch --window 300 --offset <next_offset> \\
             --save-montage /tmp/p.jpg --pretty
    """
    from miloco_cli.client import api_get
    from miloco_cli.output import print_result
    params: dict = {"with_crops": str(with_crops).lower()}
    if cam:
        params["cam"] = cam
    if track is not None:
        params["track"] = track
    if window is not None:
        params["window"] = window
    if offset > 0:
        params["offset"] = offset
    data = api_get("/api/identity/pool/fetch", params=params)
    if save_montage:
        d = (data.get("data") or {}).copy()
        b64 = d.get("numbered_montage_jpeg_b64", "")
        if b64:
            Path(save_montage).write_bytes(base64.b64decode(b64))
            d["montage_saved_to"] = save_montage
        else:
            d["montage_saved_to"] = None
            d["montage_warning"] = "no clusters or no body crop → no montage"
        # 不污染 stdout:把 base64 字段 + clusters 内嵌的 base64 都删干净
        d.pop("numbered_montage_jpeg_b64", None)
        if isinstance(d.get("clusters"), list):
            for cl in d["clusters"]:
                rep = cl.get("representative") or {}
                rep.pop("image_jpeg_b64", None)
                for cam_v in (cl.get("per_cam_representative") or {}).values():
                    if isinstance(cam_v, dict):
                        cam_v.pop("image_jpeg_b64", None)
        data = {**data, "data": d}
    print_result(data, pretty)


@pool_group.command("cluster-split")
@click.option("--cluster-id", required=True)
@click.option("--remove-cam", "remove_cams", multiple=True,
              help="按 cam_id 剥离该相机下全部成员")
@click.option("--remove-member", "remove_members", multiple=True,
              help="精确剥 'cam_id:track_id' 格式,可多次")
@click.option("--pretty", is_flag=True)
def pool_cluster_split(cluster_id, remove_cams, remove_members, pretty):
    """拆分误合并 cluster(commit 前的修正)。"""
    from miloco_cli.client import api_post
    from miloco_cli.output import print_result
    members_list: list[list] = []
    for m in remove_members:
        if ":" not in m:
            print(json.dumps({"error": f"--remove-member 格式应为 cam:track,得到 {m}"}),
                  file=sys.stderr)
            sys.exit(1)
        cam, tid = m.split(":", 1)
        members_list.append([cam, int(tid)])
    body = {
        "cluster_id": cluster_id,
        "remove_cams": list(remove_cams) or None,
        "remove_members": members_list or None,
    }
    data = api_post("/api/identity/pool/cluster-split", body)
    print_result(data, pretty)


# =============================================================================
# register 子组(注册流程)
# =============================================================================


@identity_group.group("register")
def register_group():
    """注册流程:preview / commit / sessions / rollback / from-*。"""


@register_group.command("preview")
@click.option("--image", "image_path", type=click.Path(exists=True), default=None,
                help="单张图片(--image / --video / --images 三选一)")
@click.option("--video", "video_path", type=click.Path(exists=True), default=None,
                help="单段视频(走 DeepSORT 多 track)")
@click.option("--images", "image_paths_multi", type=click.Path(exists=True),
                multiple=True,
                help="多张图(可重复传 --images a.jpg --images b.jpg ...);"
                     "服务端循环 extract_from_image 平铺,select_topk 跨图去重")
@click.option("--member-id", default=None, help="已有成员注册;不给走新建")
@click.option("--topk", type=int, default=5,
              help="服务端 select_topk 目标样本数 (默认 5, 跟 SKILL.md 文档示例对齐)")
@click.option("--pretty", is_flag=True)
@click.option("--save-montage", "save_montage", type=click.Path(), default=None,
                help="把 auto_selected 候选的拼图存到本地 jpg(agent 飞书场景必用,"
                     "用这张图发给用户看确认)。给路径后 response 里 base64 字段省略。")
def register_preview(image_path, video_path, image_paths_multi, member_id,
                     topk, save_montage, pretty):
    """两步走第 1 步:抽取 + 筛选,返回 pending_id + candidates + 一张拼图。

    \b
    Agent 飞书 / Web 流程:
      1) 调本命令(必带 --save-montage),拿到拼图文件 + pending_id
      2) **发拼图给用户** + 回话术"找到 N body + M face,确认入库到 XXX?回复'确认'"
      3) **本轮 turn 终止 (NO_REPLY)**,不要在同一 turn 调 commit
      4) 用户回复"确认"才触发新 turn → 调 register commit

    \b
    单图 vs 多图 vs 视频:
      --image      单张图 → single ScoredCandidate(图片里多人合影也会都列出)
      --video      单段视频 → DeepSORT 关联多 track + 跨帧 select_topk
      --images     多张图(≥2,重复 --images)→ 跨图平铺 + select_topk 跨图去重
    """
    from miloco_cli.client import api_post
    from miloco_cli.output import print_result
    # 三种输入互斥(三选一)
    src_count = sum([bool(image_path), bool(video_path), bool(image_paths_multi)])
    if src_count != 1:
        print(json.dumps({
            "error": "--image / --video / --images 三选一,有且仅传一个"
        }), file=sys.stderr)
        sys.exit(1)
    if image_path:
        _reject_video_misuse(image_path)
    if image_paths_multi:
        # 多图路径:base64 list + 调用 batch endpoint
        for p in image_paths_multi:
            _reject_video_misuse(p)
        b64_list = [base64.b64encode(Path(p).read_bytes()).decode()
                    for p in image_paths_multi]
        body = {
            "media_b64_list": b64_list,
            "member_id": member_id,
            "topk": topk,
        }
    else:
        media_path = image_path or video_path
        media_kind = "image" if image_path else "video"
        raw = Path(media_path).read_bytes()
        body = {
            "media_b64": base64.b64encode(raw).decode(),
            "media_kind": media_kind,
            "member_id": member_id,
            "topk": topk,
        }
    data = api_post("/api/identity/register/preview", body)
    # 保存拼图到本地,把响应里的 base64 删掉(避免污染 stdout / 节省 token)。
    # 视频多 track 时优先存 numbered_montage(号码图),单 track / 图片走 auto_selected_montage。
    # ⚠️ 同时清理 candidates list 里每个 entry 的 image_jpeg_b64 字段——视频 100+ 帧
    # 时 base64 总和能到 30+ MB,OpenClaw process tool 默认截断 stdout 会把后面的
    # multi_track / tracks 关键字段全部裁掉,agent 看不到导致走错路径。
    if save_montage:
        d = (data.get("data") or {}).copy()
        numbered_b64 = d.get("numbered_montage_jpeg_b64", "")
        auto_b64 = d.get("auto_selected_montage_jpeg_b64", "")
        chosen_b64 = numbered_b64 if numbered_b64 else auto_b64
        if chosen_b64:
            Path(save_montage).write_bytes(base64.b64decode(chosen_b64))
            d["montage_saved_to"] = save_montage
            d["montage_kind"] = "numbered" if numbered_b64 else "auto_selected"
        else:
            d["montage_saved_to"] = None
            d["montage_warning"] = "no candidates → no montage produced"
        d.pop("numbered_montage_jpeg_b64", None)
        d.pop("auto_selected_montage_jpeg_b64", None)
        # candidates 里的 image_jpeg_b64 给 web v2 用,agent 完全用不到——清掉防截断
        if isinstance(d.get("candidates"), list):
            d["candidates"] = [
                {k: v for k, v in c.items() if k != "image_jpeg_b64"}
                for c in d["candidates"]
            ]
        data = {**data, "data": d}
    print_result(data, pretty)


@register_group.command("commit")
@click.option("--pending-id", required=True)
@click.option("--indices", required=True,
                help='逗号分隔的 candidate 索引,如 "0,2,5"')
@click.option("--member-name", default=None)
@click.option("--member-role", default=None, help="家庭角色(可选,如 爸爸/妈妈)")
@click.option("--pretty", is_flag=True)
def register_commit(pending_id, indices, member_name, member_role, pretty):
    """两步走第 2 步:按 indices 真正入库。"""
    from miloco_cli.client import api_post
    from miloco_cli.output import print_result
    idx_list = [int(x) for x in indices.split(",") if x.strip()]
    body = {
        "register_session_id_pending": pending_id,
        "indices": idx_list,
        "member_name": member_name,
        "member_role": member_role,
    }
    data = api_post("/api/identity/register/commit", body)
    print_result(data, pretty)


@register_group.command("from-media")
@click.option("--name", "member_name", required=True)
@click.option("--role", "member_role", default=None, help="家庭角色(可选,如 爸爸/妈妈)")
@click.option("--image", "image_path", type=click.Path(exists=True), default=None,
                help="本地图片路径(image / video 二选一)")
@click.option("--video", "video_path", type=click.Path(exists=True), default=None,
                help="本地视频路径(image / video 二选一)")
@click.option("--topk", type=int, default=5,
              help="服务端 select_topk 目标样本数 (默认 5, 跟 SKILL.md 文档示例对齐)")
@click.option("--yes", "skip_confirm", is_flag=True, default=False,
                help="确认跳过'用户挑选/确认'步骤,直接 commit。**仅 CLI 脚本场景使用;"
                     "agent 走飞书/Web 时必须经过用户确认,走 preview + commit 两步走**")
@click.option("--pretty", is_flag=True)
def register_from_media(member_name, member_role, image_path, video_path,
                         topk, skip_confirm, pretty):
    """一气呵成:从图/视频直接注册(**仅 CLI 脚本场景**,需 --yes 显式确认)。

    内部 = preview + commit auto_selected_indices。

    ⚠️ **agent 走飞书 / Web 时不该用本命令** —— 因为它跳过了"用户挑选/确认"步骤。
    必须走两步走 `register preview` → 回话术让用户确认 → `register commit`。
    没传 --yes 时本命令直接退出并提示走 preview。
    """
    from miloco_cli.client import api_post
    from miloco_cli.output import print_result
    if not skip_confirm:
        print(json.dumps({
            "error": (
                "from-media 跳过用户确认,**禁止 agent 直接用**(用户预期是看到候选数 + "
                "确认才入库)。请改走两步:\n"
                "  1) miloco-cli identity register preview --video/--image <path> --topk 5 --pretty\n"
                "  2) 回话术让用户确认(找到 N body+M face,要登记到 XXX?)\n"
                "  3) 用户确认后 → miloco-cli identity register commit --pending-id <...> --indices <...> --member-name <name>\n"
                "如果你确实是 CLI 脚本场景需要一气呵成,加 --yes 显式跳过用户确认。"
            ),
        }, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    if bool(image_path) == bool(video_path):
        print(json.dumps({"error": "--image 与 --video 必须二选一"}), file=sys.stderr)
        sys.exit(1)
    _reject_video_misuse(image_path)
    media_path = image_path or video_path
    media_kind = "image" if image_path else "video"
    raw = Path(media_path).read_bytes()
    # 先 preview
    preview = api_post("/api/identity/register/preview", {
        "media_b64": base64.b64encode(raw).decode(),
        "media_kind": media_kind,
        "topk": topk,
    })
    pending_id = preview["data"]["register_session_id_pending"]
    auto_idx = preview["data"]["auto_selected_indices"]
    # 再 commit auto_selected_indices
    data = api_post("/api/identity/register/commit", {
        "register_session_id_pending": pending_id,
        "indices": auto_idx,
        "member_name": member_name,
        "member_role": member_role,
    })
    print_result(data, pretty)


@register_group.command("from-cluster")
@click.option("--cluster-id", required=True)
@click.option("--name", "member_name", default=None,
              help="member_id 缺省时按 name 新建成员")
@click.option("--role", "member_role", default=None, help="家庭角色(可选,如 爸爸/妈妈)")
@click.option("--member-id", default=None, help="给已有成员追加;不给走新建")
@click.option("--topk", type=int, default=5,
              help="服务端 select_topk 目标样本数 (默认 5, 跟 SKILL.md 文档示例对齐)")
@click.option("--pretty", is_flag=True)
def register_from_cluster(cluster_id, member_name, member_role, member_id,
                          topk, pretty):
    """从陌生人池 cluster_id 注册(SKILL workflow B/C 终态)。"""
    from miloco_cli.client import api_post
    from miloco_cli.output import print_result
    # 真名(member_name)与家庭角色(member_role)各归各位:role 不再用 name 兜底。
    # name 是身份主键、role 可空——服务端 commit_pending 也已拆掉同款 fallback。
    body = {
        "cluster_id": cluster_id,
        "member_name": member_name,
        "member_role": member_role,
        "member_id": member_id,
        "topk": topk,
    }
    data = api_post("/api/identity/register/from-cluster", body)
    print_result(data, pretty)


@register_group.command("sessions")
@click.option("--member", default=None, help="只看该成员")
@click.option("--limit", type=int, default=20)
@click.option("--pretty", is_flag=True)
def register_sessions(member, limit, pretty):
    """列历史注册批次。"""
    from miloco_cli.client import api_get
    from miloco_cli.output import print_result
    params = {"limit": limit}
    if member:
        params["member_id"] = member
    data = api_get("/api/identity/register/sessions", params=params)
    print_result(data, pretty)


@register_group.command("rollback")
@click.option("--person-id", required=True)
@click.option("--session-id", required=True)
@click.option("--pretty", is_flag=True)
def register_rollback(person_id, session_id, pretty):
    """撤销该 session 写入的所有样本。"""
    from miloco_cli.client import api_post
    from miloco_cli.output import print_result
    data = api_post("/api/identity/register/rollback",
                     {"person_id": person_id,
                      "register_session_id": session_id})
    print_result(data, pretty)


# =============================================================================
# extract / select 算法独立入口
# =============================================================================


@identity_group.command("extract")
@click.option("--image", "image_path", type=click.Path(exists=True), required=True)
@click.option("--pretty", is_flag=True)
def identity_extract(image_path, pretty):
    """抽取算法(M4):从图像抽 ScoredCandidate。"""
    from miloco_cli.client import api_post
    from miloco_cli.output import print_result
    raw = Path(image_path).read_bytes()
    data = api_post("/api/identity/extract", {
        "media_b64": base64.b64encode(raw).decode(),
        "media_kind": "image",
    })
    print_result(data, pretty)


@identity_group.command("select")
@click.option("--candidates-file", type=click.Path(exists=True), required=True,
                help="extract 输出 JSON 文件;通常是 'identity extract ... > out.json' 的内容")
@click.option("--topk", type=int, default=3)
@click.option("--min-k", type=int, default=1)
@click.option("--pretty", is_flag=True)
def identity_select(candidates_file, topk, min_k, pretty):
    """筛选算法(M5):从 candidates 数组挑 topk。"""
    from miloco_cli.client import api_post
    from miloco_cli.output import print_result
    raw_json = json.loads(Path(candidates_file).read_text(encoding="utf-8"))
    # extract CLI 输出格式 = api_post 返回的 dict;data.candidates 是数组
    cands = raw_json.get("data", {}).get("candidates", raw_json.get("candidates", []))
    data = api_post("/api/identity/select", {
        "candidates": cands, "topk": topk, "min_k": min_k,
    })
    print_result(data, pretty)
