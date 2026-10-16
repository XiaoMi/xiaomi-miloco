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


# 「成因」那一列跟着写入侧的判据走，判据已经前移过一次：`event_rejected` 里的「参数名
# 当不了路径段」在入表校验前移后不可达了，该支成因并进了 `event_arg_unknown`。改判据时同步这里。
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

    判「事件推送通没通」（另一条独立通道，订阅与闸门都是分开的两套）：

    \b
    - push.event_written > 0                事件到达、参数名从 spec 翻出来了、真进了树
    - event_arg_unknown 有数                帧到了，但 (siid, eiid, piid) 在事件 spec 里
                                            翻不出参数名 —— 事件 spec 没取到，这台设备的
                                            spec 里这个事件没有这个参数，或参数名非法
                                            （空 / 含 '/' 或 '*' / 与 siid、eiid 拼成的段
                                            超 128）在建表时已被滤掉
    - event_no_args 有数                    无参事件（spec 允许），或整帧参数都残缺
    - event_rejected 有数                   参数名翻出来了，值却没落进树：值是对象
                                            （事件参数只承载标量），或设备号当不了路径段
    - event_not_aligned / event_out_of_home 两道闸各自拦下的
    - 全部为 0 且事件确实发生过             订阅那条通道没通（事件没有回拉，叶只能是空的）
    """
    from miloco_cli.client import api_get

    print_result(api_get(_STATS_PATH), pretty=pretty)


@state_group.command("dump")
@click.option(
    "--pattern",
    default="**",
    show_default=True,
    help="路径 pattern，例如 iot/device/*/prop/* 或 iot/device/*/event/*",
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
