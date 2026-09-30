"""管理界面后端：HTTP 服务 + REST API + 飞牛统一网关接入。

设计要点（都是踩过的坑）：

* 统一网关**不剥前缀**，``/app/airconnect/...`` 会原样转发进来，应用自己剥；
  裸前缀 ``/app/airconnect`` 必须 307 跳到带斜杠的地址，否则页面里的相对路径
  （``css/app.css``）会被解析到 ``/app/css/...`` → 静态资源全 404 → 白屏。
* 网关走 Unix 域套接字（``$TRIM_APPDEST/airconnect.sock``，权限 0666），
  TCP 端口只作为直连调试入口。
* 静态资源一律 ``Cache-Control: no-cache``：升级后「界面没变」十有八九是缓存，
  不要靠让用户强刷来解决。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import socket
import subprocess
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import acconf
import acpath
from aclog import tail_text

log = logging.getLogger("airconnect.web")

MAX_BODY_BYTES = 256 * 1024
# 只支持 POST 的接口：用来区分「方法不对」（405）与「路径不存在」（404）
_POST_ONLY_PATHS = {"/api/settings", "/api/devices", "/api/action"}
ALLOWED_STATIC_EXT = {
    ".html", ".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".svg",
    ".ico", ".webmanifest", ".json", ".woff2", ".map", ".txt",
}


class AppState:
    """HTTP 层与业务层之间的共享状态。"""

    def __init__(self, supervisor: Any = None, version: str = "0.0.0",
                 ui_dir: str = "") -> None:
        self.supervisor = supervisor
        self.version = version
        self.ui_dir = ui_dir
        self.started_at = time.time()
        self.lock = threading.RLock()
        self.gateway_prefix = acpath.GATEWAY_PREFIX.rstrip("/")
        self.settings: Dict[str, Any] = acconf.load()
        self._binary_versions: Dict[str, str] = {}
        self._binary_cache_at = 0.0

    # ---------------------------------------------------------- 设置
    def reload_settings(self) -> Dict[str, Any]:
        with self.lock:
            settings, changed = acconf.sync_devices(self.settings, self.settings.get("mode", "upnp"))
            self.supervisor.enrich_devices(settings)
            settings, _ = acconf.sync_devices(settings, settings.get("mode", "upnp"))
            if changed:
                try:
                    self.settings = acconf.save(settings)
                except acconf.ConfigError:
                    self.settings = settings
            return self.settings

    def apply_settings(self, payload: Dict[str, Any], restart: bool = True) -> Dict[str, Any]:
        """校验 → 落盘 → 写 AirConnect XML → 重启桥接。"""
        with self.lock:
            # 先与 AirConnect 自动保存的 XML 合并：否则「GET 设置 → 期间发现新设备
            # → POST 保存」会把刚发现的设备条目从 XML 里抹掉。
            base, _ = acconf.sync_devices(self.settings, self.settings.get("mode", "upnp"))
            merged = dict(base)
            for key, value in payload.items():
                if key in acconf.DEFAULTS:
                    merged[key] = value
            clean = acconf.validate(merged)
            acconf.write_xml_files(clean)
            self.settings = acconf.save(clean)
            if restart:
                self.supervisor.restart(self.settings)
            return self.settings

    def apply_device(self, payload: Dict[str, Any], restart: bool = True) -> Dict[str, Any]:
        with self.lock:
            udn = str(payload.get("udn", "")).strip()
            if not udn:
                raise acconf.ConfigError("缺少设备 UDN")
            base, _ = acconf.sync_devices(self.settings, self.settings.get("mode", "upnp"))
            devices = [dict(d) for d in base.get("devices", [])]
            target = None
            for device in devices:
                if device["udn"] == udn:
                    target = device
                    break
            if target is None:
                raise acconf.ConfigError("设备不在列表中，请先刷新设备列表")
            if "enabled" in payload:
                target["enabled"] = 1 if payload["enabled"] else 0
            if "name" in payload:
                name = str(payload["name"]).strip()
                if len(name) > 60:
                    raise acconf.ConfigError("设备名称最多 60 个字符")
                if '"' in name or "\\" in name:
                    raise acconf.ConfigError("设备名称不能包含引号或反斜杠")
                target["name"] = name
            merged = dict(base)
            merged["devices"] = devices
            clean = acconf.validate(merged)
            acconf.write_xml_files(clean)
            self.settings = acconf.save(clean)
            if restart:
                self.supervisor.restart(self.settings)
            return self.settings

    def forget_device(self, udn: str) -> Dict[str, Any]:
        with self.lock:
            base, _ = acconf.sync_devices(self.settings, self.settings.get("mode", "upnp"))
            devices = [d for d in base.get("devices", []) if d["udn"] != udn]
            merged = dict(base)
            merged["devices"] = devices
            clean = acconf.validate(merged)
            acconf.write_xml_files(clean)
            self.settings = acconf.save(clean)
            self.supervisor.restart(self.settings)
            return self.settings

    # ---------------------------------------------------------- 二进制版本
    def binary_versions(self) -> Dict[str, str]:
        now = time.time()
        if now - self._binary_cache_at < 600 and self._binary_versions:
            return self._binary_versions
        versions: Dict[str, str] = {}
        for name in ("airupnp", "aircast"):
            path = acpath.binary_path(name)
            if not os.access(path, os.X_OK):
                versions[name] = "缺失"
                continue
            try:
                proc = subprocess.run(  # noqa: S603 - 常量路径
                    [path, "-h"], capture_output=True, timeout=5, check=False)
                text = (proc.stdout or b"").decode("utf-8", "replace")
                text += (proc.stderr or b"").decode("utf-8", "replace")
                first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "未知")
                versions[name] = first
            except (OSError, subprocess.SubprocessError):
                versions[name] = "未知"
        self._binary_versions = versions
        self._binary_cache_at = now
        return versions


# ---------------------------------------------------------------- 网络接口
def list_interfaces() -> List[Dict[str, str]]:
    """列出网卡（优先用 ``ip -j``，取出 IPv4 与接口名）。"""
    result: List[Dict[str, str]] = []
    try:
        proc = subprocess.run(["ip", "-j", "-4", "addr"],  # noqa: S603,S607
                              capture_output=True, timeout=5, check=False)
        if proc.returncode == 0 and proc.stdout:
            for entry in json.loads(proc.stdout.decode("utf-8", "replace")):
                name = entry.get("ifname", "")
                if not name or name == "lo":
                    continue
                for info in entry.get("addr_info", []):
                    if info.get("family") == "inet":
                        result.append({"name": name,
                                       "address": info.get("local", ""),
                                       "label": f"{name} ({info.get('local', '')})"})
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if not result:
        try:
            for index, name in socket.if_nameindex():
                if name == "lo":
                    continue
                result.append({"name": name, "address": "",
                               "label": f"{name} (#{index})"})
        except OSError:
            pass
    return result


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "AirConnect-fnOS"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> AppState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------ 输出辅助
    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_text(self, text: str, status: int = HTTPStatus.OK,
                   content_type: str = "text/plain; charset=utf-8",
                   download_name: str = "") -> None:
        body = text.encode("utf-8", "replace")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if download_name:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{download_name}"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send_json({"ok": False, "error": message}, status=status)

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from None
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _check_csrf(self) -> bool:
        if (self.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest":
            return True
        self._error(HTTPStatus.FORBIDDEN, "缺少 X-Requested-With 头（CSRF 防护）")
        return False

    # ------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        prefix = self.state.gateway_prefix
        if prefix:
            if path == prefix:
                self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
                self.send_header("Location", prefix + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path.startswith(prefix + "/"):
                path = path[len(prefix):]
        try:
            if path.startswith("/api/"):
                self._handle_api(method, path, query)
            elif method in ("GET", "HEAD"):
                self._handle_static(path)
            else:
                self._error(HTTPStatus.METHOD_NOT_ALLOWED, "不支持的方法")
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001 - 单个请求绝不能打挂线程
            log.exception("处理请求失败：%s %s", method, path)
            try:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误")
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------ API
    def _handle_api(self, method: str, path: str, query: Dict[str, List[str]]) -> None:
        state = self.state
        if path == "/api/health" and method in ("GET", "HEAD"):
            snapshot = state.supervisor.snapshot()
            self._send_json({
                "ok": True,
                "version": state.version,
                "mode": snapshot["mode"],
                "running": snapshot["running"],
                "home": acpath.PKG_HOME,
                "data_library_path": os.environ.get("DATA_LIBRARY_PATH", ""),
                "uptime": round(time.time() - state.started_at, 1),
            })
            return

        if path == "/api/status" and method in ("GET", "HEAD"):
            self._send_json(self._status_payload())
            return

        if path == "/api/settings" and method in ("GET", "HEAD"):
            settings = state.reload_settings()
            self._send_json({"ok": True, "settings": settings})
            return

        if path == "/api/devices" and method in ("GET", "HEAD"):
            settings = state.reload_settings()
            self._send_json({"ok": True,
                             "devices": settings.get("devices", []),
                             "mode": settings.get("mode", "upnp"),
                             "name_suffix": settings.get("name_suffix", "+")})
            return

        if path == "/api/interfaces" and method in ("GET", "HEAD"):
            self._send_json({"ok": True, "interfaces": list_interfaces()})
            return

        if path == "/api/logs" and method in ("GET", "HEAD"):
            source = (query.get("source") or ["airupnp"])[0]
            try:
                lines = int((query.get("lines") or ["300"])[0])
            except ValueError:
                lines = 300
            lines = max(10, min(lines, 3000))
            if source == "server":
                text = tail_text(acpath.SERVER_LOG, lines)
            elif source == "main":
                text = tail_text(acpath.MAIN_LOG, lines)
            elif source == "cast":
                text = state.supervisor.logs("cast", lines)
            else:
                source = "airupnp"
                text = state.supervisor.logs("upnp", lines)
            self._send_json({"ok": True, "source": source, "lines": lines, "text": text})
            return

        if path == "/api/logs/download" and method in ("GET", "HEAD"):
            source = (query.get("source") or ["airupnp"])[0]
            mapping = {
                "server": (acpath.SERVER_LOG, "server.log"),
                "main": (acpath.MAIN_LOG, "main.log"),
                "cast": (os.path.join(acpath.PKG_VAR, "aircast.log"), "aircast.log"),
                "airupnp": (os.path.join(acpath.PKG_VAR, "airupnp.log"), "airupnp.log"),
            }
            target, filename = mapping.get(source, mapping["airupnp"])
            text = tail_text(target, 3000, max_scan=4 * 1024 * 1024)
            self._send_text(text, download_name=f"airconnect-{filename}")
            return

        if method != "POST":
            if path in _POST_ONLY_PATHS:
                self._error(HTTPStatus.METHOD_NOT_ALLOWED, "该接口只支持 POST")
            else:
                self._error(HTTPStatus.NOT_FOUND, "接口不存在")
            return
        if not self._check_csrf():
            return

        if path == "/api/settings":
            try:
                payload = self._read_json()
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            try:
                settings = state.apply_settings(payload)
            except acconf.ConfigError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send_json({"ok": True, "settings": settings,
                             "message": "设置已保存，桥接程序已重启"})
            return

        if path == "/api/devices":
            try:
                payload = self._read_json()
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            try:
                if payload.get("action") == "forget":
                    settings = state.forget_device(str(payload.get("udn", "")))
                else:
                    settings = state.apply_device(payload)
            except acconf.ConfigError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send_json({"ok": True, "devices": settings.get("devices", []),
                             "message": "设备设置已保存，桥接程序已重启"})
            return

        if path == "/api/action":
            try:
                payload = self._read_json()
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            action = str(payload.get("action", ""))
            settings = state.reload_settings()
            if action == "start":
                result = state.supervisor.start_mode(settings)
                message = "已启动"
            elif action == "stop":
                state.supervisor.stop_all()
                result = {}
                message = "已停止"
            elif action in ("restart", "rescan"):
                result = state.supervisor.restart(settings)
                message = "已重启，正在重新发现设备（约 10 秒）" if action == "rescan" else "已重启"
            else:
                self._error(HTTPStatus.BAD_REQUEST, f"未知操作：{action}")
                return
            self._send_json({"ok": True, "result": result, "message": message,
                             "status": state.supervisor.snapshot()})
            return

        self._error(HTTPStatus.NOT_FOUND, "接口不存在")

    def _status_payload(self) -> Dict[str, Any]:
        state = self.state
        snapshot = state.supervisor.snapshot()
        settings = state.reload_settings()
        return {
            "ok": True,
            "app": {
                "name": "AirConnect",
                "version": state.version,
                "uptime": round(time.time() - state.started_at, 1),
                "port": acpath.SERVICE_PORT,
            },
            "bridge": snapshot,
            "binaries": state.binary_versions(),
            "settings": {k: v for k, v in settings.items() if k != "devices"},
            "paths": {
                "app": acpath.APP_DEST,
                "var": acpath.PKG_VAR,
                "etc": acpath.PKG_ETC,
                "home": acpath.PKG_HOME,
                "socket": acpath.GATEWAY_SOCKET,
            },
            "log_sizes": {name: _safe_size(path) for name, path in (
                ("server", acpath.SERVER_LOG),
                ("airupnp", os.path.join(acpath.PKG_VAR, "airupnp.log")),
                ("aircast", os.path.join(acpath.PKG_VAR, "aircast.log")),
            )},
        }

    # ------------------------------------------------------ 静态资源
    def _handle_static(self, path: str) -> None:
        ui_dir = os.path.realpath(self.state.ui_dir)
        if path in ("", "/"):
            path = "/index.html"
        relative = path.lstrip("/")
        target = os.path.realpath(os.path.join(ui_dir, relative))
        if not (target == ui_dir or target.startswith(ui_dir + os.sep)):
            self._error(HTTPStatus.FORBIDDEN, "越权访问")
            return
        if not os.path.isfile(target):
            # SPA 回退
            fallback = os.path.join(ui_dir, "index.html")
            if os.path.isfile(fallback) and "." not in os.path.basename(path):
                target = fallback
            else:
                self._error(HTTPStatus.NOT_FOUND, "文件不存在")
                return
        ext = os.path.splitext(target)[1].lower()
        if ext and ext not in ALLOWED_STATIC_EXT:
            self._error(HTTPStatus.FORBIDDEN, "不支持的文件类型")
            return
        try:
            with open(target, "rb") as handle:
                body = handle.read()
        except OSError:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "读取文件失败")
            return
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ext in (".js", ".mjs", ".json", ".webmanifest"):
            ctype += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 升级后「界面没变」多半是缓存：一律不缓存，别让用户强刷
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class UnixHTTPServer(ThreadingHTTPServer):
    """在 Unix 域套接字上提供 HTTP 服务（飞牛统一网关的接入点）。"""

    address_family = socket.AF_UNIX

    def server_bind(self) -> None:  # noqa: D102
        path = str(self.server_address)
        try:
            if os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass
        self.socket.bind(path)
        self.server_name = "unix"
        self.server_port = 0
        try:
            os.chmod(path, 0o666)
        except OSError:
            pass

    def server_activate(self) -> None:  # noqa: D102
        self.socket.listen(self.request_queue_size)

    def get_request(self):  # noqa: D102
        conn, _ = self.socket.accept()
        return conn, ("unix", 0)

    def server_close(self) -> None:  # noqa: D102
        super().server_close()
        try:
            os.unlink(str(self.server_address))
        except OSError:
            pass


class WebServer:
    """同时提供 TCP 端口（直连）与飞牛网关 Unix 套接字。"""

    def __init__(self, host: str, port: int, state: AppState,
                 socket_path: str = "") -> None:
        self.host = host
        self.port = port
        self.state = state
        self.socket_path = socket_path
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._unixd: Optional[UnixHTTPServer] = None
        self._threads: List[threading.Thread] = []

    def start(self) -> None:
        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self._httpd.state = self.state  # type: ignore[attr-defined]
        thread = threading.Thread(target=self._httpd.serve_forever,
                                  name="http-tcp", daemon=True)
        thread.start()
        self._threads.append(thread)
        log.info("管理界面已监听 %s:%s", self.host, self.port)

        if not self.socket_path:
            return
        try:
            parent = os.path.dirname(self.socket_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._unixd = UnixHTTPServer(self.socket_path, Handler)  # type: ignore[arg-type]
            self._unixd.daemon_threads = True
            self._unixd.state = self.state  # type: ignore[attr-defined]
            uthread = threading.Thread(target=self._unixd.serve_forever,
                                       name="http-unix", daemon=True)
            uthread.start()
            self._threads.append(uthread)
            log.info("飞牛统一网关套接字已就绪：%s（前缀 %s）",
                     self.socket_path, self.state.gateway_prefix or "-")
        except Exception:  # noqa: BLE001 - 套接字失败不应让应用起不来
            self._unixd = None
            log.exception("创建网关套接字失败（TCP 端口仍可用）：%s", self.socket_path)

    def stop(self) -> None:
        for server in (self._unixd, self._httpd):
            if server is None:
                continue
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=3.0)
