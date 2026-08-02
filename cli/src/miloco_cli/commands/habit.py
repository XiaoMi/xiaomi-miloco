"""habit 命令组：习惯建议候选库读写（防骚扰状态机）。

配合 miloco-habit-suggest skill 使用。状态流转：
pending → asked →（accepted → created）| rejected | expired。

读写本地 ``$MILOCO_HOME/home-profile/task-suggestions.json``（fcntl 文件锁 + 原子写），
输出与旧 openclaw ``miloco_habit_suggest`` tool 完全一致的 JSON。
"""

import sys

import click

from miloco_cli.habit_store import apply_habit_action
from miloco_cli.output import print_result


def _emit(result: dict, pretty: bool) -> None:
    """输出结果 JSON 到 stdout；业务失败（ok:false）按 CLI 约定退出码 3。

    与 client.py 的 HTTP 路径一致（后端 code!=0 → exit 3）：脚本/cron 包装器可靠
    exit code 判成败，agent 仍照常读 stdout 的 ``ok`` 字段。
    """
    print_result(result, pretty)
    if not result.get("ok"):
        sys.exit(3)


@click.group("habit")
def habit_group():
    """习惯建议候选库：list / record / asked / resolve。"""


@habit_group.command("list")
@click.option("--pretty", is_flag=True)
def habit_list(pretty):
    """读候选库现状。

    返回 can_ask_now（此刻能否发起新询问）、open_questions（正在等回应的条目）、
    askable_pending（可挑去询问的候选）、entries（全量条目，含已拒绝/已建/已作废——
    据此判断是不是同一个习惯、复用既有 key、跳过终态）。
    """
    _emit(apply_habit_action("list"), pretty)


@habit_group.command("record")
@click.option("--key", required=True, help="稳定语义 key（如 wanglei_sleep_dim_light）；同一习惯务必复用 list 里已有的 key")
@click.option("--subject", default="shared", help="习惯主体：成员名；全家公共填 shared")
@click.option("--habit", required=True, help="观察到的习惯（规范短句）")
@click.option("--suggestion", required=True, help="要推荐的任务点子（自然语言，认可后据此建任务）")
@click.option("--title", default=None, help="一句话标题（可选，缺省截取 habit）")
@click.option("--evidence", default=None, help="依据（档案条目/出现频率，可选）")
@click.option("--item-id", default=None, help="该习惯所依据的家庭档案条目 id（追踪来源 + 建成任务后从档案渲染剔除）")
@click.option("--pretty", is_flag=True)
def habit_record(key, subject, habit, suggestion, title, evidence, item_id, pretty):
    """把识别到的一条习惯登记为候选（status=pending）。

    同一 key 幂等：已 rejected/created 的只返回既有、永久不再推；过期（expired）的会复活为
    pending 重新推荐，但累计问满 3 次仍无果即永久放弃；在途（pending/asked/accepted）原样返回。
    """
    _emit(
        apply_habit_action(
            "record",
            {
                "key": key,
                "subject": subject,
                "habit": habit,
                "suggestion": suggestion,
                "title": title,
                "evidence": evidence,
                "item_id": item_id,
            },
        ),
        pretty,
    )


@habit_group.command("asked")
@click.option("--key", required=True, help="要标记为已询问的建议 key")
@click.option("--pretty", is_flag=True)
def habit_mark_asked(key, pretty):
    """把某条 pending 翻成 asked。

    **必须在 IM 推送确认送达（ok:true）之后才调**；本命令会再次校验防骚扰闸门，
    越界（已有待回应 / 今天已问过 / 状态不对）直接返回 ok:false。
    """
    _emit(apply_habit_action("mark_asked", {"key": key}), pretty)


@habit_group.command("resolve")
@click.option("--key", required=True, help="要落地的建议 key")
@click.option(
    "--outcome",
    required=True,
    type=click.Choice(["created", "rejected", "accepted"]),
    help="created（任务建成、回填 task_id，终态）/ rejected（拒绝，终态）/ accepted（中间态：需跨轮分步建任务时先标已同意）",
)
@click.option("--task-id", default=None, help="outcome=created 时回填的任务 id")
@click.option("--reason", default=None, help="outcome=rejected 时的简短原因（可选）")
@click.option("--pretty", is_flag=True)
def habit_resolve(key, outcome, task_id, reason, pretty):
    """用户回应后落地：created / rejected / accepted。"""
    _emit(
        apply_habit_action(
            "resolve",
            {"key": key, "outcome": outcome, "task_id": task_id, "reason": reason},
        ),
        pretty,
    )
