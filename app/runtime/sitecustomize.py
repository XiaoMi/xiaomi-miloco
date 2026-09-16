"""内嵌解释器的运行期约束（随 App 打包进 site-packages，每次启动自动加载）。

**只做一件事：禁止写字节码。**

原因：App 是 ad-hoc 签名（没有 Developer ID），签名会把 ``Contents`` 下所有文件
做成封条（CodeResources）。内嵌解释器一旦在运行期补写 ``__pycache__/*.pyc``，封条
立刻失效（``codesign --verify`` 报 "a sealed resource is missing or invalid"），
而且是在**签名之后**发生，构建期看不见。

字节码已在构建期用 ``compileall`` 预热好，运行期只读即可，所以禁写不影响启动速度；
``PYTHONDONTWRITEBYTECODE=1`` 是同一件事的环境变量版本（启动器已设），这里再加一道
兜底，保证无论谁怎么调这个解释器，App 包都不会被自己改坏。
"""

import sys

sys.dont_write_bytecode = True
