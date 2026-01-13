from setuptools import setup, find_packages
import tomllib
import os
from collections import defaultdict


# 从给定路径的pyproject.toml文件中读取依赖
def load_requirement(part_name: str):
    with open("./" + part_name + "/pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    project = data.get("project", {})
    dependencies = project.get("dependencies", [])
    optional_dependencies = project.get("optional-dependencies", {})
    
    return dependencies, optional_dependencies

dep = {}
opt_dep = {}
dep["server"], opt_dep["server"] = load_requirement("miloco_server")
dep["miot_kit"], opt_dep["miot_kit"] = load_requirement("miot_kit")

defaults = dep["server"] + dep["miot_kit"]

extras = {
    "dev": opt_dep["server"]["dev"],
}

packages = find_packages(
    where=".",
    include=[
        "miloco_server",
        "miloco_server.*",  # 递归包含所有子包
        "scripts",
        "config",
    ],
    exclude=["tests", "tests.*", "*.egg-info"],
)
# 单独处理 miot_kit，因为它有特殊的 package_dir 映射
miot_packages = find_packages(
    where="miot_kit",
    include=["miot", "miot.*"],
)
# 重命名为 miot_kit
miot_packages = ["miot_kit" if p == "miot" else p.replace("miot.", "miot_kit.") for p in miot_packages]

all_packages = packages + miot_packages


setup(
    name="miloco",
    version="0.1.2",
    python_requires='>=3.11',
    packages=all_packages,
    package_dir={"miot_kit": "miot_kit/miot"},
    install_requires= defaults,
    extras_require= extras,
    entry_points={
        "console_scripts": [
            "miloco=scripts.start_server:main",
        ]
    },
    include_package_data=True,
)
