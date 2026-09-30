#!/usr/bin/env python3
"""AirConnect 飞牛应用 —— 常驻管理服务入口。

职责：加载/校验设置 → 生成 AirConnect 配置文件 → 拉起 airupnp/aircast →
在 TCP 端口与飞牛统一网关套接字上提供管理界面与 REST API → 监管子进程并轮转日志。

只使用 Python 标准库：manifest 里声明 ``install_dep_apps = python312``，
安装期与运行期都不需要 pip、不需要联网。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import acconf          # noqa: E402
import aclog           # noqa: E402
import acpath          # noqa: E402
import acproc          # noqa: E402
import acweb           # noqa: E402

log = logging.getLogger("airconnect")


def _prepare_environment() -> None:
    """规则 #6 / #16：飞牛不注入 DATA_LIBRARY_PATH，HOME 会被注入成 /root。

    ``HOME`` 必须**无条件**覆盖成应用家目录（写成 ``${HOME:-...}`` 不会生效，
    因为 fnOS 已经注入了 ``HOME=/root``）；``DATA_LIBRARY_PATH`` 用
    ``TRIM_PKGVAR`` 兜底。
    """
    os.environ["HOME"] = acpath.PKG_HOME
    os.environ["DATA_LIBRARY_PATH"] = (
        os.environ.get("DATA_LIBRARY_PATH") or os.environ.get("TRIM_PKGVAR") or acpath.PKG_VAR
    )
    os.environ.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")


def _bootstrap_settings() -> dict:
    """首次运行生成 settings.json；已有配置则加载并合并 XML 里发现的设备。"""
    os.makedirs(acpath.PKG_ETC, exist_ok=True)
    if not os.path.exists(acpath.SETTINGS_FILE):
        clean = acconf.save(dict(acconf.DEFAULTS))
        log.info("已生成默认配置：%s", acpath.SETTINGS_FILE)
        return clean
    settings = acconf.load()
    settings, changed = acconf.sync_devices(settings, settings.get("mode", "upnp"))
    if changed:
        settings = acconf.save(settings)
        log.info("已从 AirConnect 配置合并 %d 个设备", len(settings.get("devices", [])))
    return settings


def main() -> int:
    parser = argparse.ArgumentParser(description="AirConnect fnOS 管理服务")
    parser.add_argument("--ui-dir", default=acpath.UI_DIR)
    parser.add_argument("--version", default=acpath.APP_VER)
    parser.add_argument("--port", type=int, default=acpath.SERVICE_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--socket", default=acpath.GATEWAY_SOCKET)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    level = getattr(logging, str(args.log_level).upper(), logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO

    _prepare_environment()
    acpath.ensure_dirs()
    aclog.setup_logging(acpath.SERVER_LOG, level)
    log.info("AirConnect 管理服务启动：version=%s port=%s home=%s",
             args.version, args.port, acpath.PKG_HOME)

    settings = _bootstrap_settings()
    try:
        acconf.write_xml_files(settings)
    except Exception:  # noqa: BLE001 - 配置写失败也要把服务起起来，便于看日志
        log.exception("写入 AirConnect 配置失败")

    # Supervisor 需要「读取最新设置」的回调，AppState 又需要 Supervisor，
    # 因此先建 AppState（supervisor=None），再把两者接起来。
    state = acweb.AppState(version=args.version, ui_dir=args.ui_dir)
    state.settings = settings

    def live_settings() -> dict:
        with state.lock:
            return state.settings

    supervisor = acproc.Supervisor(live_settings)
    state.supervisor = supervisor

    result = supervisor.start_mode(state.settings)
    log.info("桥接进程启动结果：%s", result)

    rotation = aclog.RotationThread(acpath.log_paths())
    rotation.start()

    server = acweb.WebServer(args.host, args.port, state, args.socket)
    try:
        server.start()
    except OSError as exc:
        log.exception("监听 %s:%s 失败", args.host, args.port)
        print(f"无法监听 {args.host}:{args.port}：{exc}", file=sys.stderr)
        supervisor.stop_all()
        return 1

    supervisor.start_monitor()

    stopping = threading.Event()

    def handle_signal(signum, _frame):  # noqa: ANN001
        log.info("收到信号 %s，开始退出", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        while not stopping.wait(1.0):
            pass
    finally:
        log.info("正在停止桥接进程与 HTTP 服务")
        supervisor.stop_monitor()
        supervisor.stop_all()
        server.stop()
        rotation.stop()
        aclog.trim_inplace(acpath.SERVER_LOG)
        log.info("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
