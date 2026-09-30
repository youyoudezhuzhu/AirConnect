"""AirConnect 飞牛应用 —— 路径与环境。

硬规则：脚本与代码里**一律**通过飞牛注入的 ``TRIM_*`` 环境变量引用路径，
绝不硬编码 ``/var/apps/airconnect`` 之类的绝对路径。

分区语义（见 fnos-fpk 方法总纲 §2）：

* ``TRIM_APPDEST`` 代码目录 —— 升级会被整体替换
* ``TRIM_PKGVAR``  运行数据 / 日志 / PID
* ``TRIM_PKGETC``  配置
* ``TRIM_PKGHOME`` 用户数据（这里的 AirConnect config.xml 属于用户数据）
* ``TRIM_PKGTMP``  临时文件
"""

from __future__ import annotations

import os
from typing import List

APPNAME = "airconnect"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


# ---------------------------------------------------------------- 分区路径
APP_DEST = _env("TRIM_APPDEST", f"/var/apps/{APPNAME}/target")
PKG_VAR = _env("TRIM_PKGVAR", f"/vol1/@appdata/{APPNAME}")
PKG_ETC = _env("TRIM_PKGETC", f"/vol1/@appconf/{APPNAME}")
PKG_HOME = _env("TRIM_PKGHOME", f"/vol1/@apphome/{APPNAME}")
PKG_TMP = _env("TRIM_PKGTMP", "/tmp")

RUN_USER = _env("TRIM_USERNAME", APPNAME)
APP_VER = _env("TRIM_APPVER", "0.0.0")

# 服务端口：manifest 的 service_port，飞牛通过 TRIM_SERVICE_PORT 注入
SERVICE_PORT = int(_env("TRIM_SERVICE_PORT", "18888") or "18888")

# 飞牛统一网关
GATEWAY_PREFIX = _env("GATEWAY_PREFIX", f"/app/{APPNAME}")
GATEWAY_SOCKET = _env("GATEWAY_SOCKET", os.path.join(APP_DEST, f"{APPNAME}.sock"))

# ---------------------------------------------------------------- 应用内文件
BIN_DIR = os.path.join(APP_DEST, "bin")
SERVER_DIR = os.path.join(APP_DEST, "server")
UI_DIR = os.path.join(APP_DEST, "ui")

SETTINGS_FILE = os.path.join(PKG_ETC, "settings.json")
AIRUPNP_XML = os.path.join(PKG_HOME, "airupnp.xml")
AIRCAST_XML = os.path.join(PKG_HOME, "aircast.xml")

SERVER_LOG = os.path.join(PKG_VAR, "server.log")
MAIN_LOG = os.path.join(PKG_VAR, "main.log")

# python312 是 manifest 里声明的依赖运行时
PYTHON_CANDIDATES = (
    "/var/apps/python312/target/bin/python3",
    "/usr/bin/python3",
    "/bin/python3",
)

# AirConnect 二进制端口区间：避开 5666/5667（Web UI）、22、445/2049、5005/5006、
# 8000（影视）以及本机 18778/18801/18980/19092/19093/19798/19991 等已占用端口。
DEFAULT_PORT_BASE = 18300
DEFAULT_PORT_RANGE = 128
DEFAULT_UPNP_PORT = 49152


def python_executable() -> str:
    for candidate in PYTHON_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return "python3"


def binary_path(name: str) -> str:
    """airupnp / aircast 在包内的绝对路径。"""
    return os.path.join(BIN_DIR, name)


def ensure_dirs() -> None:
    for path in (PKG_VAR, PKG_ETC, PKG_HOME):
        os.makedirs(path, exist_ok=True)


def log_paths() -> List[str]:
    """本应用会写入的全部日志文件（供轮转与清理使用）。"""
    return [
        MAIN_LOG,
        SERVER_LOG,
        os.path.join(PKG_VAR, "airupnp.log"),
        os.path.join(PKG_VAR, "aircast.log"),
        os.path.join(PKG_VAR, "supervisor.log"),
    ]
