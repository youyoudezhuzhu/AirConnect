#!/usr/bin/env python3
"""设置读写 CLI —— 供生命周期脚本（安装 / 升级 / 应用设置向导）调用。

用法::

    settings_cli.py init                     # 不存在则生成默认设置
    settings_cli.py migrate                  # 补齐缺失字段（只增不删）
    settings_cli.py wizard                   # 读取 wizard_* 环境变量并写入
    settings_cli.py set mode=cast name_suffix=+
    settings_cli.py get [key]

校验失败时打印原因到 stderr 并返回非 0；调用方必须保留原配置。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import acconf  # noqa: E402

# 向导日志级别 → 各子日志级别。debug 时把子模块也提到 info，否则光有 main_log
# 根本看不到设备发现过程，用户会以为「打开了调试还是什么都没有」。
LEVEL_MAP = {
    "error": {"main_log": "error", "upnp_log": "error", "cast_log": "error",
              "raop_log": "error", "util_log": "error"},
    "warn": {"main_log": "warn", "upnp_log": "warn", "cast_log": "warn",
             "raop_log": "warn", "util_log": "error"},
    "info": {"main_log": "info", "upnp_log": "info", "cast_log": "info",
             "raop_log": "warn", "util_log": "error"},
    "debug": {"main_log": "debug", "upnp_log": "info", "cast_log": "info",
              "raop_log": "info", "util_log": "info"},
    "sdebug": {"main_log": "debug", "upnp_log": "debug", "cast_log": "debug",
               "raop_log": "debug", "util_log": "debug"},
}

WIZARD_KEYS = ("wizard_mode", "wizard_name_suffix", "wizard_log_level",
               "wizard_latency")


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def cmd_init() -> int:
    if os.path.exists(acconf.acpath.SETTINGS_FILE):
        print("配置已存在，保持不变")
        return 0
    acconf.save(dict(acconf.DEFAULTS))
    print(f"已生成默认配置：{acconf.acpath.SETTINGS_FILE}")
    return 0


def cmd_migrate() -> int:
    """补齐缺失字段；已有值一律保留（升级时不覆盖用户设置）。"""
    current = acconf.load()
    merged = dict(acconf.DEFAULTS)
    merged.update({k: v for k, v in current.items() if k != "devices"})
    merged["devices"] = current.get("devices", [])
    try:
        clean = acconf.save(merged)
    except acconf.ConfigError as exc:
        return _fail(f"配置迁移失败：{exc}")
    print(f"配置已迁移，共 {len(clean.get('devices', []))} 个设备")
    return 0


def cmd_set(pairs) -> int:
    if not pairs:
        return _fail("用法：settings_cli.py set key=value [key=value ...]")
    current = acconf.load()
    updates = {}
    for pair in pairs:
        if "=" not in pair:
            return _fail(f"参数格式应为 key=value：{pair}")
        key, value = pair.split("=", 1)
        key = key.strip()
        if key not in acconf.DEFAULTS:
            return _fail(f"未知配置项：{key}")
        updates[key] = value
    current.update(updates)
    try:
        clean = acconf.save(current)
    except acconf.ConfigError as exc:
        return _fail(f"配置校验失败，已保留原配置：{exc}")
    print("配置已更新：" + ", ".join(f"{k}={clean[k]}" for k in updates))
    return 0


def cmd_wizard() -> int:
    """应用 install / config 向导里填写的值。"""
    current = acconf.load()
    updates = {}

    mode = os.environ.get("wizard_mode", "").strip()
    if mode:
        updates["mode"] = mode

    if "wizard_name_suffix" in os.environ:
        updates["name_suffix"] = os.environ["wizard_name_suffix"].strip()

    latency = os.environ.get("wizard_latency", "").strip()
    if latency:
        updates["latency"] = latency

    level = os.environ.get("wizard_log_level", "").strip().lower()
    if level:
        updates.update(LEVEL_MAP.get(level, {}))

    if not updates:
        print("向导未提供任何可写字段，保持不变")
        return 0

    current.update(updates)
    try:
        clean = acconf.save(current)
    except acconf.ConfigError as exc:
        return _fail(f"配置校验失败，已保留原配置：{exc}")
    print("向导配置已写入：" + json.dumps({k: clean[k] for k in updates}, ensure_ascii=False))
    return 0


def cmd_get(args) -> int:
    current = acconf.load()
    if args:
        payload = {key: current.get(key) for key in args}
    else:
        payload = current
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main(argv) -> int:
    if len(argv) < 2:
        return _fail(__doc__ or "缺少子命令")
    command = argv[1]
    if command == "init":
        return cmd_init()
    if command == "migrate":
        return cmd_migrate()
    if command == "wizard":
        return cmd_wizard()
    if command == "set":
        return cmd_set(argv[2:])
    if command == "get":
        return cmd_get(argv[2:])
    return _fail(f"未知子命令：{command}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
