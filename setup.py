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

# 处理二进制文件, 整合所有的MANIFEST.in
def load_manifest(part_name: str):
    """
    解析 MANIFEST.in 文件，转换为 package_data 格式
    
    支持的指令:
    - include path/pattern
    - recursive-include dir pattern
    - graft dir
    
    返回: dict, 如 {'miot': ['libs/**/*', 'specs/*', ...]}
    """
    manifest_path = "./" + part_name + "/MANIFEST.in"
    package_data = defaultdict(list)
    
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"文件不存在: {manifest_path}")
    
    with open(manifest_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            
            # 跳过空行和注释
            if not line or line.startswith('#'):
                continue
            
            parts = line.split()
            directive = parts[0].lower()
            
            if directive == 'include' and len(parts) >= 2:
                # include miot/specs/*
                for pattern in parts[1:]:
                    package, rel_path = _split_package_path(pattern)
                    if package:
                        package_data[package].append(f"{part_name}/"+rel_path)
                        
            elif directive == 'recursive-include' and len(parts) >= 3:
                # recursive-include miot/libs *
                dir_path = parts[1]
                patterns = parts[2:]
                package, rel_dir = _split_package_path(dir_path)
                if package:
                    for pattern in patterns:
                        if rel_dir:
                            full_pattern = f"{rel_dir}/**/{pattern}"
                        else:
                            full_pattern = f"**/{pattern}"
                        package_data[package].append(f"{part_name}/"+full_pattern)
                        
            # elif directive == 'graft' and len(parts) >= 2:
            #     # graft miot/data
            #     dir_path = parts[1]
            #     package, rel_dir = _split_package_path(dir_path)
            #     if package:
            #         if rel_dir:
            #             package_data[package].append(f"{rel_dir}/**/*")
            #         else:
            #             package_data[package].append("**/*")
                        
            # elif directive == 'global-include' and len(parts) >= 2:
            #     # global-include *.txt
            #     # 这个比较特殊，暂时跳过或需要手动处理
            #     pass
    
    return dict(package_data)

def _split_package_path(path):
    """
    将路径拆分为包名和相对路径
    'miot/libs/data' -> ('miot', 'libs/data')
    'miot/*.py' -> ('miot', '*.py')
    """
    path = path.replace('\\', '/')
    parts = path.split('/')
    
    if len(parts) >= 1:
        package = parts[0]
        rel_path = '/'.join(parts[1:]) if len(parts) > 1 else ''
        return package, rel_path
    
    return None, path

# 整合所有MANIFEST.in
package_data = {}
for part_name in ["miot_kit"]:
    package_data.update(load_manifest(part_name))

package_data.setdefault("miloco_server", []).append("miloco_server/static/**/*")

package_data.setdefault("config", []).append("**/*")  # config 是独立的包
package_data.setdefault("scripts", []).append("**/*")  # scripts 是独立的包

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

setup(
    name="miloco",
    version="0.1.2",
    python_requires='>=3.11',
    packages=packages,
    package_dir={"miloco_server": "miloco_server",
                    "miot_kit": "miot_kit/miot",
                    "config": "config",
                    "scripts": "scripts"},
    install_requires= defaults,
    extras_require= extras,
    package_data=package_data,
    entry_points={
        "console_scripts": [
            "miloco=scripts.start_server:main",
        ]
    }
)
