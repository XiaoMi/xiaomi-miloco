"""pet 命令组测试：CliRunner + mock 底层 API 调用。"""

import base64
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from miloco_cli.main import cli


def _write(name, data=b"\xff\xd8\xffX"):
    Path(name).write_bytes(data)


def test_pet_list():
    with patch(
        "miloco_cli.client.api_get", return_value={"code": 0, "data": {"pets": []}}
    ) as m:
        r = CliRunner().invoke(cli, ["pet", "list"])
    assert r.exit_code == 0
    m.assert_called_once_with("/api/identity/pets")


def test_pet_add():
    with patch(
        "miloco_cli.client.api_post",
        return_value={"code": 0, "data": {"id": "pet_abc", "name": "小黑"}},
    ) as m:
        r = CliRunner().invoke(cli, ["pet", "add", "--name", "小黑", "--species", "猫"])
    assert r.exit_code == 0
    m.assert_called_once_with("/api/identity/pets", {"name": "小黑", "species": "猫"})


def test_pet_add_requires_name():
    r = CliRunner().invoke(cli, ["pet", "add", "--species", "猫"])
    assert r.exit_code != 0  # --name required


def test_pet_update():
    with patch(
        "miloco_cli.client.api_patch", return_value={"code": 0, "data": {}}
    ) as m:
        r = CliRunner().invoke(cli, ["pet", "update", "pet_abc", "--name", "小白"])
    assert r.exit_code == 0
    m.assert_called_once_with("/api/identity/pets/pet_abc", {"name": "小白"})


def test_pet_update_no_fields_errors():
    r = CliRunner().invoke(cli, ["pet", "update", "pet_abc"])
    assert r.exit_code != 0


def test_pet_delete():
    with patch(
        "miloco_cli.client.api_delete", return_value={"code": 0, "data": {}}
    ) as m:
        r = CliRunner().invoke(cli, ["pet", "delete", "pet_abc"])
    assert r.exit_code == 0
    m.assert_called_once_with("/api/identity/pets/pet_abc")


# ── observe ─────────────────────────────────────────────────────────────────

def _observe_resp():
    return {
        "code": 0,
        "data": {
            "detected": True,
            "description": {"summary": "一只黑猫"},
            "primary_crop_b64": base64.b64encode(b"PRIMARY").decode(),
            "avatar_b64": base64.b64encode(b"AVATAR").decode(),
            "montage_b64": base64.b64encode(b"MONTAGE").decode(),
            "candidates": [
                {"crop_b64": base64.b64encode(b"C0").decode(), "conf": 0.9,
                 "sharpness": 0.8, "area_ratio": 0.5, "species_guess": "猫"},
                {"crop_b64": base64.b64encode(b"C1").decode(), "conf": 0.6,
                 "sharpness": 0.5, "area_ratio": 0.4, "species_guess": "猫"},
            ],
            "warnings": [],
        },
    }


def test_pet_observe_saves_crops_and_strips_b64():
    runner = CliRunner()
    with runner.isolated_filesystem():
        _write("a.jpg")
        _write("b.jpg")
        with patch(
            "miloco_cli.client.api_post_multipart", return_value=_observe_resp()
        ) as m:
            r = runner.invoke(
                cli,
                ["pet", "observe", "--images", "a.jpg", "--images", "b.jpg",
                 "--grounding", "--save-crops", "obs"],
            )
        assert r.exit_code == 0, r.output
        path, files, data = m.call_args.args
        assert path == "/api/identity/pets:observe"
        assert [f[0] for f in files] == ["medias", "medias"]  # 两张图都走 medias
        assert data["grounding"] == "true"
        # 候选 crop 落地 + primary 落地
        assert Path("obs_0.jpg").read_bytes() == b"C0"
        assert Path("obs_1.jpg").read_bytes() == b"C1"
        assert Path("obs_primary.jpg").read_bytes() == b"PRIMARY"
        # 默认头像（头部裁剪）也落盘
        assert Path("obs_avatar.jpg").read_bytes() == b"AVATAR"
        # 多姿态参考图横向拼图落盘（发用户看这一张）
        assert Path("obs_montage.jpg").read_bytes() == b"MONTAGE"
        # stdout 清掉冗长 base64，保留 crops_saved（含绝对分）+ avatar/montage_saved_to
        assert "crop_b64" not in r.output
        assert "avatar_b64" not in r.output
        assert "montage_b64" not in r.output
        assert "crops_saved" in r.output
        assert "avatar_saved_to" in r.output
        assert "montage_saved_to" in r.output


def test_pet_observe_video_single():
    runner = CliRunner()
    with runner.isolated_filesystem():
        _write("v.mp4", b"\x00\x00v")
        with patch(
            "miloco_cli.client.api_post_multipart", return_value=_observe_resp()
        ) as m:
            r = runner.invoke(cli, ["pet", "observe", "--video", "v.mp4"])
        assert r.exit_code == 0, r.output
        _path, files, _data = m.call_args.args
        assert [f[0] for f in files] == ["medias"]


def test_pet_observe_requires_exactly_one_source():
    runner = CliRunner()
    with runner.isolated_filesystem():
        _write("a.jpg")
        # 无源
        assert runner.invoke(cli, ["pet", "observe"]).exit_code != 0
        # 两源互斥
        _write("b.jpg")
        r = runner.invoke(
            cli, ["pet", "observe", "--image", "a.jpg", "--images", "b.jpg"]
        )
        assert r.exit_code != 0


def test_pet_observe_max_3_images():
    runner = CliRunner()
    with runner.isolated_filesystem():
        for n in ("a.jpg", "b.jpg", "c.jpg", "d.jpg"):
            _write(n)
        r = runner.invoke(
            cli,
            ["pet", "observe", "--images", "a.jpg", "--images", "b.jpg",
             "--images", "c.jpg", "--images", "d.jpg"],
        )
        assert r.exit_code != 0


# ── reference-crops ──────────────────────────────────────────────────────────

def test_pet_reference_crops_replace():
    runner = CliRunner()
    with runner.isolated_filesystem():
        _write("c0.jpg")
        _write("c1.jpg")
        with patch(
            "miloco_cli.client.api_post_multipart",
            return_value={"code": 0, "data": {"id": "pet_x", "reference_crop_count": 2}},
        ) as m:
            r = runner.invoke(
                cli,
                ["pet", "reference-crops", "pet_x", "--crops", "c0.jpg",
                 "--crops", "c1.jpg", "--scores", "0.5,0.3", "--mode", "replace"],
            )
        assert r.exit_code == 0, r.output
        path, files, data = m.call_args.args
        assert path == "/api/identity/pets/pet_x/reference-crops"
        assert [f[0] for f in files] == ["crops", "crops"]
        assert data["mode"] == "replace"
        assert data["scores"] == ["0.5", "0.3"]


def test_pet_reference_crops_append_space_scores():
    runner = CliRunner()
    with runner.isolated_filesystem():
        _write("c0.jpg")
        with patch(
            "miloco_cli.client.api_post_multipart",
            return_value={"code": 0, "data": {}},
        ) as m:
            r = runner.invoke(
                cli,
                ["pet", "reference-crops", "pet_x", "--crops", "c0.jpg",
                 "--scores", "0.42", "--mode", "append"],
            )
        assert r.exit_code == 0, r.output
        _path, _files, data = m.call_args.args
        assert data["mode"] == "append"
        assert data["scores"] == ["0.42"]


def test_pet_reference_crops_requires_crops():
    r = CliRunner().invoke(cli, ["pet", "reference-crops", "pet_x"])
    assert r.exit_code != 0  # --crops required


# ── avatar ───────────────────────────────────────────────────────────────────

def test_pet_avatar():
    runner = CliRunner()
    with runner.isolated_filesystem():
        _write("av.png")
        with patch(
            "miloco_cli.client.api_post_multipart",
            return_value={"code": 0, "data": {"id": "pet_x", "avatar_ext": "png"}},
        ) as m:
            r = runner.invoke(cli, ["pet", "avatar", "pet_x", "--image", "av.png"])
        assert r.exit_code == 0, r.output
        path, files = m.call_args.args
        assert path == "/api/identity/pets/pet_x/avatar"
        assert files[0][0] == "image"


def test_pet_avatar_requires_image():
    r = CliRunner().invoke(cli, ["pet", "avatar", "pet_x"])
    assert r.exit_code != 0  # --image required


def test_pet_reference_crops_replace_over_3_errors():
    runner = CliRunner()
    with runner.isolated_filesystem():
        for n in ("c0.jpg", "c1.jpg", "c2.jpg", "c3.jpg"):
            _write(n)
        r = runner.invoke(
            cli,
            ["pet", "reference-crops", "pet_x", "--crops", "c0.jpg", "--crops",
             "c1.jpg", "--crops", "c2.jpg", "--crops", "c3.jpg", "--mode", "replace"],
        )
        assert r.exit_code != 0
