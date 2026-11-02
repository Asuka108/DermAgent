# -*- coding: utf-8 -*-
"""本机部署环境变量文件：默认读取 server/data/local.env（KEY=VALUE，已有环境变量优先）。

凭据只落在本机 600 权限文件里，不写入代码/仓库；内容与值均不落日志。
仓库根目录 .env 是唯一凭据文件，data/local.env 为指向它的软链接。
"""
import os


def path() -> str:
    return os.environ.get("DERM_ENV_FILE") or os.path.join(
        os.environ.get("DERM_DATA_DIR") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data"),
        "local.env")


def load(file_path: str = "") -> bool:
    target = file_path or path()
    try:
        with open(target, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val
    return True
