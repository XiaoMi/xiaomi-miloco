from __future__ import annotations

import json

import click


@click.group()
def observe_group() -> None:
    """家庭知识 Observe 的 IoT 事实旁路。"""


@observe_group.command("batch")
@click.option("--pretty", is_flag=True)
def observe_batch(pretty: bool) -> None:
    """读取一批有界事实；不推进观察水位。"""
    from miloco_cli.client import api_post

    result = api_post("/api/iot-observation/batch")
    click.echo(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
    )


@observe_group.command("submit")
@click.option("--data", default=None, help="共性、差异与证据 JSON；空 observations 表示无发现")
@click.option("--data-file", type=click.File("r", encoding="utf-8"), default=None)
@click.option("--pretty", is_flag=True)
def observe_submit(data, data_file, pretty: bool) -> None:
    """原子保存观察结果并确认批次；支持空结果。"""
    from miloco_cli.client import api_post
    from miloco_cli.output import print_result

    if (data is None) == (data_file is None):
        raise click.UsageError("provide exactly one of --data or --data-file")
    try:
        payload = json.loads(data_file.read(65537) if data_file else data)
    except (ValueError, OSError) as exc:
        raise click.BadParameter("invalid observation JSON") from exc
    if not isinstance(payload, dict) or len(json.dumps(payload).encode()) > 65536:
        raise click.BadParameter("observation payload must be an object within 64 KiB")
    print_result(api_post("/api/iot-observation/submit", payload), pretty)
