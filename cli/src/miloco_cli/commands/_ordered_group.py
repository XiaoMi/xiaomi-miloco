"""按命令定义顺序（而非字母序）展示子命令的 click.Group。

Click 默认 ``Group.list_commands`` 返回 ``sorted``（字母序），导致 ``--help`` 的
Commands 区顺序与命令组 docstring 里人工编排的顺序对不上。本子类改为按注册
（即源码中 ``@group.command()`` 的定义）顺序展示，使两者一致——把命令按希望展示
的顺序定义即可，无需再维护一份独立的顺序列表。
"""

import click


class OrderedGroup(click.Group):
    def list_commands(self, ctx: click.Context) -> list[str]:
        # self.commands 是 dict，Python 3.7+ 保留插入顺序 = 定义顺序
        return list(self.commands)
