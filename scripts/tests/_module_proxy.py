"""测试用的双模块代理。

卸载逻辑拆到 ``installer_uninstall`` 后，测试既需要 ``scripts.install`` 的
安装侧符号，也需要卸载模块的 cleaners/helpers。这个代理解析两边属性，
并把 ``mock.patch.object`` 的写入同时落到两个模块，保证卸载函数读到的
全局变量也被替换。
"""

from __future__ import annotations


class MergedModule:
    def __init__(self, *modules):
        object.__setattr__(self, "_modules", modules)

    def __getattr__(self, name):
        for module in self._modules:
            try:
                return getattr(module, name)
            except AttributeError:
                continue
        raise AttributeError(name)

    def __setattr__(self, name, value):
        for module in self._modules:
            setattr(module, name, value)

    def __delattr__(self, name):
        for module in self._modules:
            try:
                delattr(module, name)
            except AttributeError:
                continue
