"""DLNA 渲染器状态监视 + 空闲会话释放。

**为什么需要它**：iOS 暂停（以及某些"切回本机播放"的情况）**不发任何 RTSP 命令** ——
没有 FLUSH、没有 TEARDOWN。AirConnect 收不到任何通知，于是那个 DLNA 会话就一直挂着：
音箱停在 ``PLAYING``、位置冻结、HTTP 连接不断，米家/小爱同学 App 里一直显示"播放中"，
音箱也一直不进入待机。

本模块用**独立**的 UPnP 轮询把这件事看出来（AirConnect 自己不暴露这个状态），然后：

* 把状态给界面显示（播放中 / 已暂停 / 已停止 + 位置 + 冻结了多久）；
* 支持**手动**「释放会话」；
* 可选地**自动**释放：状态是播放中/已暂停、且位置冻结超过 N 秒时发一个 ``AVTStop``。
  默认关闭（``idle_release_seconds = 0``）。

**刻意不清空 CurrentURI**：实测该机型在外部 ``AVTStop`` 之后，单独一个 ``Play``
就会重新去拉同一个 URI（服务端能看到新的 GET）。保留 URI 才能让后续恢复更省事。

已知取舍：AirConnect 只在收到 RAOP 的 PLAY 事件时才发 ``AVTPlay``。如果 iOS 的
"暂停 → 恢复"不重新 RECORD（它有时确实什么都不发），那么被释放掉的会话不会自动
恢复，需要在 iPhone 上重新选一次音箱。所以自动释放默认关闭、由用户自己决定。
"""

from __future__ import annotations

import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional, Tuple

import acpath

log = logging.getLogger("airconnect.dlna")

SSDP_ADDR = ("239.255.255.250", 1900)
DEV_NS = "urn:schemas-upnp-org:device-1-0"
AVT_SERVICE = "urn:schemas-upnp-org:service:AVTransport:1"

# 会被判定为「占着会话」的状态
BUSY_STATES = ("PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING", "RECORDING")

# 位置冻结多久才算「流已经死了」。RelTime 粒度是 1 秒，给足余量避免误判。
MIN_FREEZE_SECONDS = 8.0


def should_release(state: str, frozen_seconds: float, threshold: float) -> bool:
    """纯逻辑，便于单测：是否该把这个会话释放掉。"""
    if threshold <= 0:
        return False
    if state not in BUSY_STATES:
        return False
    if frozen_seconds < max(threshold, MIN_FREEZE_SECONDS):
        return False
    return True


def _parse_reltime(value: str) -> Optional[int]:
    """``01:23:45`` / ``01:23:45.678`` → 秒。取不到返回 None。"""
    match = re.match(r"^(\d+):(\d{2}):(\d{2})", (value or "").strip())
    if not match:
        return None
    hours, minutes, seconds = (int(x) for x in match.groups())
    return hours * 3600 + minutes * 60 + seconds


# ---------------------------------------------------------------- UPnP 基础
def _ssdp_search(timeout: float = 4.0) -> List[str]:
    msg = "\r\n".join([
        "M-SEARCH * HTTP/1.1",
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}",
        'MAN: "ssdp:discover"',
        "MX: 2",
        "ST: urn:schemas-upnp-org:device:MediaRenderer:1",
        "", "",
    ]).encode()
    locations: List[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        sock.sendto(msg, SSDP_ADDR)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                break
            match = re.search(rb"LOCATION:\s*(\S+)", data, re.I)
            if match:
                value = match.group(1).decode("utf-8", "replace")
                if value not in locations:
                    locations.append(value)
    except OSError:
        pass
    finally:
        sock.close()
    return locations


def _device_info(location: str) -> Dict[str, str]:
    """抓设备描述，取出 UDN / 名称 / 型号 / AVTransport 控制地址。"""
    with urllib.request.urlopen(location, timeout=5) as resp:
        root = ET.fromstring(resp.read())
    base = re.match(r"(https?://[^/]+)", location)
    base_url = base.group(1) if base else ""
    info = {
        "udn": (root.findtext(f".//{{{DEV_NS}}}UDN", default="") or "").strip(),
        "name": (root.findtext(f".//{{{DEV_NS}}}friendlyName", default="") or "").strip(),
        "model": (root.findtext(f".//{{{DEV_NS}}}modelName", default="") or "").strip(),
        "control_url": "",
    }
    for service in root.iter(f"{{{DEV_NS}}}service"):
        stype = service.findtext(f"{{{DEV_NS}}}serviceType") or ""
        if "AVTransport" in stype:
            ctrl = (service.findtext(f"{{{DEV_NS}}}controlURL") or "").strip()
            if ctrl.startswith("http"):
                info["control_url"] = ctrl
            else:
                info["control_url"] = base_url + ("" if ctrl.startswith("/") else "/") + ctrl
            break
    return info


class Renderer:
    """单个渲染器的 UPnP 客户端（只用到 AVTransport 的三个动作）。"""

    def __init__(self, udn: str, control_url: str, name: str = "", model: str = "") -> None:
        self.udn = udn
        self.control_url = control_url
        self.name = name
        self.model = model
        self.state = "UNKNOWN"
        self.reltime: Optional[int] = None
        self.track_uri = ""
        self.last_change = time.time()     # 位置最后一次变化
        self.last_ok = 0.0
        self.last_error = ""

    # ---------------------------------------------------------- SOAP
    def _soap(self, action: str, extra: str = "", timeout: float = 4.0) -> str:
        body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{AVT_SERVICE}"><InstanceID>0</InstanceID>{extra}'
            f'</u:{action}></s:Body></s:Envelope>'
        ).encode()
        request = urllib.request.Request(self.control_url, data=body, headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{AVT_SERVICE}#{action}"',
        })
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    def poll(self) -> None:
        """刷新状态与位置。任何异常都只记录，绝不让监管线程死掉。"""
        try:
            info = self._soap("GetTransportInfo")
            state = _pick(info, "CurrentTransportState") or "UNKNOWN"
            pos = self._soap("GetPositionInfo")
            reltime = _parse_reltime(_pick(pos, "RelTime"))
            uri = _pick(pos, "TrackURI")
        except (urllib.error.URLError, OSError, ValueError, ET.ParseError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.state = "UNREACHABLE"
            return
        self.last_error = ""
        self.last_ok = time.time()
        if reltime != self.reltime:
            self.last_change = time.time()
        self.state = state
        self.reltime = reltime
        self.track_uri = uri

    def stop(self) -> bool:
        """发 AVTStop 释放会话。**不动 CurrentURI**，这样后续一个 Play 还能拉起来。"""
        try:
            self._soap("Stop")
            return True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.last_error = f"Stop 失败 {type(exc).__name__}: {exc}"
            log.warning("释放 DLNA 会话失败（%s）：%s", self.name or self.udn, exc)
            return False

    # ---------------------------------------------------------- 查询
    @property
    def frozen_seconds(self) -> float:
        if self.state not in BUSY_STATES:
            return 0.0
        return max(0.0, time.time() - self.last_change)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "udn": self.udn,
            "name": self.name,
            "model": self.model,
            "state": self.state,
            "reltime": self.reltime,
            "frozen_seconds": round(self.frozen_seconds, 1),
            "track_uri": self.track_uri,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "control_url": self.control_url,
        }


def _pick(xml_text: str, tag: str) -> str:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", xml_text, re.S)
    return match.group(1).strip() if match else ""


# ---------------------------------------------------------------- 监视器
class DlnaMonitor(threading.Thread):
    """周期性刷新各渲染器状态，并按设置决定是否释放空闲会话。"""

    def __init__(self, settings_provider: Callable[[], Dict[str, Any]],
                 interval: float = 3.0) -> None:
        super().__init__(name="dlna-monitor", daemon=True)
        self.settings_provider = settings_provider
        self.interval = interval
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._renderers: Dict[str, Renderer] = {}
        self._last_discover = 0.0
        self.releases: List[Dict[str, Any]] = []      # 最近的释放记录（给界面看）

    # ---------------------------------------------------------- 发现
    def discover(self) -> None:
        locations = _ssdp_search()
        found: Dict[str, Dict[str, str]] = {}
        for location in locations:
            try:
                info = _device_info(location)
            except (urllib.error.URLError, OSError, ET.ParseError, ValueError):
                continue
            if info.get("udn") and info.get("control_url"):
                found[info["udn"]] = info
        with self._lock:
            for udn, info in found.items():
                renderer = self._renderers.get(udn)
                if renderer is None:
                    self._renderers[udn] = Renderer(udn, info["control_url"],
                                                    info["name"], info["model"])
                else:
                    renderer.control_url = info["control_url"]
                    renderer.name = info["name"] or renderer.name
                    renderer.model = info["model"] or renderer.model
            self._last_discover = time.time()

    # ---------------------------------------------------------- 主循环
    def run(self) -> None:  # noqa: D102
        try:
            self.discover()
        except Exception:  # noqa: BLE001 - 发现失败不该让线程死掉
            log.exception("DLNA 设备发现失败")
        while not self._stop_event.wait(self.interval):
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                log.exception("DLNA 监视循环异常")

    @staticmethod
    def _wanted_from_settings(settings: Dict[str, Any]) -> set:
        return {d["udn"] for d in settings.get("devices", [])
                if d.get("bridge", "upnp") == "upnp" and int(d.get("enabled", 1))}

    @staticmethod
    def _wanted_from_airconnect_xml() -> set:
        """兜底：设备还没被同步进 settings 时（全新安装、用户又没开过界面），
        直接读 AirConnect 自己维护的配置。否则监视器永远不轮询、自动释放形同虚设。"""
        import acconf  # 延迟导入，避免 acdlna 在无 TRIM 环境下不可测
        try:
            _, devices = acconf.parse_xml(acpath.AIRUPNP_XML)
        except Exception:  # noqa: BLE001
            return set()
        return {d["udn"] for d in devices if int(d.get("enabled", 1))}

    def _tick(self) -> None:
        settings = self.settings_provider()
        # 只有 UPnP 模式才有 DLNA 设备要管
        wanted = self._wanted_from_settings(settings)
        if not wanted:
            wanted = self._wanted_from_airconnect_xml()
        with self._lock:
            known = set(self._renderers)
        if wanted - known and time.time() - self._last_discover > 30:
            self.discover()
        with self._lock:
            targets = [self._renderers[u] for u in wanted if u in self._renderers]
        for renderer in targets:
            renderer.poll()
        threshold = float(settings.get("idle_release_seconds", 0) or 0)
        if threshold <= 0:
            return
        for renderer in targets:
            if should_release(renderer.state, renderer.frozen_seconds, threshold):
                log.info("DLNA 会话空闲 %.0f 秒（状态 %s），按设置释放：%s",
                         renderer.frozen_seconds, renderer.state, renderer.name or renderer.udn)
                if renderer.stop():
                    self.note_release(renderer, "auto")

    # ---------------------------------------------------------- 对外
    def note_release(self, renderer: Renderer, reason: str) -> None:
        with self._lock:
            self.releases.append({
                "udn": renderer.udn,
                "name": renderer.name,
                "reason": reason,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            del self.releases[:-20]
        # 释放后位置必然归零，重置冻结计时，避免下一轮又判一次
        renderer.last_change = time.time()

    def release(self, udn: str) -> Dict[str, Any]:
        """手动释放（界面上的「释放会话」按钮）。"""
        with self._lock:
            renderer = self._renderers.get(udn)
        if renderer is None:
            return {"ok": False, "error": "还没探测到这台设备，请先「重新扫描」"}
        if renderer.state not in BUSY_STATES:
            return {"ok": True, "message": f"设备状态已是 {renderer.state}，无需释放"}
        if renderer.stop():
            self.note_release(renderer, "manual")
            return {"ok": True, "message": "已释放 DLNA 会话"}
        return {"ok": False, "error": renderer.last_error or "释放失败"}

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "renderers": [r.snapshot() for r in self._renderers.values()],
                "releases": list(self.releases),
                "last_discover": self._last_discover,
            }

    def state_for(self, udn: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            renderer = self._renderers.get(udn)
        return renderer.snapshot() if renderer else None

    def stop_monitor(self) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=3.0)
