"""state 命令组：读状态容器与属性推送的计数、转储容器内容。

**每个诊断出口都要有一个只读的命令行入口。** 判据是「这个功能正常运行时，能不能在
不改代码的前提下证明它在跑」——答不上来就等于没上线，出问题时分不出是坏了还是根本
没接。一个没有读取方的 stats() 等于没写。
"""

import click

from miloco_cli.output import print_result

_STATS_PATH = "/api/miot/state/stats"
_DUMP_PATH = "/api/miot/state/dump"


@click.group("state")
def state_group():
    """状态容器：计数 / 转储。"""


@state_group.command("stats")
@click.option("--pretty", is_flag=True)
def state_stats(pretty):
    """容器与推送写入器的计数。

    判「属性推送通没通」：

    \b
    - push.prop_written > 0                 推送到达写入器、过了两道闸、真进了树
    - prop_written == 0 而 prop_not_aligned / prop_out_of_home / prop_rejected 有数
                                            推送到了，被闸挡掉
    - 全部为 0，而订阅对账报 failed=0       SUBACK 成功但 broker 没投递
    """
    from miloco_cli.client import api_get

    print_result(api_get(_STATS_PATH), pretty=pretty)


@state_group.command("dump")
@click.option(
    "--pattern",
    default="**",
    show_default=True,
    help="路径 pattern，例如 iot/device/*/prop/*",
)
@click.option("--limit", type=int, default=500, show_default=True, help="最多返回几行")
@click.option("--pretty", is_flag=True)
def state_dump(pattern, limit, pretty):
    """按 pattern 转储容器。每条叶子带 src=，能分辨这个值是对齐写的（iot_align）
    还是推送写的（iot_push）——判「某台设备到底推没推」不用再看计数总量。"""
    from miloco_cli.client import api_get

    print_result(
        api_get(_DUMP_PATH, params={"pattern": pattern, "limit": limit}), pretty=pretty
    )
