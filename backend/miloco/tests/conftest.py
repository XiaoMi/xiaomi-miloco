# 【test/pet-prompt-force 分支专用】
# 本分支把 prompt_builder._has_pets_for_scene() 默认覆盖为强制注入 pet prompt（供感知回归测试）。
# 单元测试应校验**生产 gating 逻辑**，故这里给测试会话默认关掉强制（等价生产行为）；
# 运行时的 backend 进程没有这个 conftest，仍是「默认强制注入」。setdefault 不覆盖显式设置的值。
import os

os.environ.setdefault("MILOCO_FORCE_PET_PROMPT", "0")
