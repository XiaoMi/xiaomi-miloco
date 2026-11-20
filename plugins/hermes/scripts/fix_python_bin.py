"""install-hermes.sh helper: 把 python_bin 合并写回 config.json（fail-closed）。

与 read_python_bin.py 配对：写路径必须区分「文件不存在/空白 = 首启初始化」与
「存在但读不懂 = 拒绝写盘」。读失败当空基底会让深合并退化成整体覆盖，一次
auto-fix 就清掉 server.token / model.omni.api_key 等段落（miloco issue #569），
且这个修复跑在安装器备份之前——损坏字节被覆盖后，连人工恢复的现场都没有。

退出码：0 = 已落盘；1 = 拒绝写盘（原因打到 stderr）；2 = 用法错误。
"""

import json
import os
import sys
import tempfile
from pathlib import Path


def merge_python_bin(config_path: Path, py_bin: str) -> bool:
    """把 ``server.python_bin`` 合并进 config.json，返回是否真正落盘。"""
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""  # 文件不存在：首启初始化，没有可保留的内容
    except OSError as e:
        print(f"config.json 无法读取，拒绝写入 python_bin: {e}", file=sys.stderr)
        return False
    if not text.strip():
        cfg: dict = {}
    else:
        try:
            cfg = json.loads(text)
        except json.JSONDecodeError as e:
            print(
                f"config.json 无法解析，拒绝写入 python_bin 以免清空其他段落: {e}",
                file=sys.stderr,
            )
            return False
        if not isinstance(cfg, dict):
            print(
                "config.json 顶层不是 JSON 对象，拒绝写入 python_bin 以免清空其他段落",
                file=sys.stderr,
            )
            return False
    cfg.setdefault("server", {})["python_bin"] = py_bin
    # 原子写：tmp + fsync 数据页 + rename，避免写一半留下截断文件（下一个损坏源）。
    fd, tmp = tempfile.mkstemp(dir=str(config_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, config_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: fix_python_bin.py <miloco_home> <python_bin>", file=sys.stderr)
        sys.exit(2)
    home, py_bin = sys.argv[1], sys.argv[2]
    sys.exit(0 if merge_python_bin(Path(home) / "config.json", py_bin) else 1)
