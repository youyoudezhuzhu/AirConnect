"""AirConnect 配置模型。

两个层次：

1. ``settings.json``（``$TRIM_PKGETC``）—— 本应用自己的设置，是 Web UI 的
   唯一数据源，同时缓存设备的友好名/型号等元数据。
2. ``airupnp.xml`` / ``aircast.xml``（``$TRIM_PKGHOME``）—— AirConnect 自己的
   配置文件。启动时由 settings 生成；运行期 AirConnect 会用 ``-I`` 自动保存
   （仅在新设备被发现时才写盘，见 airupnp.c 的 ``if (Updated && ...)``），
   因此被发现的设备会自己出现在文件里。UI 每次读取时把 XML 合并回 settings。

XML 结构必须与上游 ``SaveConfig()`` 输出保持一致（已用 ``airupnp -i`` 实测）::

    <?xml version="1.0"?>
    <airupnp>
    <common>
    <enabled>1</enabled>
    ...
    </common>
    <main_log>info</main_log>
    ...
    <device>
    <udn>uuid:...</udn>
    <name>音箱+</name>
    <mac>bb:bb:db:0e:c3:72</mac>
    <enabled>1</enabled>
    </device>
    </airupnp>

注意：XML 在**命令行参数之前**被加载（``main()`` 里先 ``LoadConfig`` 再
``ParseArgs``），所以命令行会覆盖 XML —— 只有 ``-N``（名称格式）没有对应的
XML 键，必须走命令行。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

import acpath

SCHEMA_VERSION = 1

VALID_MODES = ("upnp", "cast", "both")
VALID_LEVELS = ("error", "warn", "info", "debug")
VALID_STREAM_TYPES = ("broadcast", "track", "radio")

CODEC_RE = re.compile(
    r"^(?:pcm|wav|mp3(?::\d{2,3})?|aac(?::\d{2,3})?|flac(?::[0-9])?(?:/\d{3,5})?)$",
    re.IGNORECASE,
)
LATENCY_RE = re.compile(r"^-?\d{0,6}(?::-?\d{0,6})?(?::[fF01])?$")
LEVEL_RE = re.compile(r"^[a-z]+$")

# 默认值尽量贴近上游「无配置文件时的编译期默认」，但把对 NAS 更安全的项改掉：
# 端口固定在一段不冲突的区间、日志级别收敛到 info/warn。
DEFAULTS: Dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "mode": "upnp",
    "name_suffix": "+",
    "latency": "0:0",
    "codec": "flac",
    "http_length": -1,
    "stream_type": "broadcast",
    "upnp_max": 1,
    "enabled_by_default": 1,
    "max_volume": 100,
    "media_volume": 0.5,
    "stop_receiver": 0,
    "metadata": 1,
    "flush": 1,
    "drift": 0,
    "artwork": "",
    "max_players": 32,
    "log_limit": -1,
    "main_log": "info",
    "upnp_log": "warn",
    "cast_log": "warn",
    "util_log": "warn",
    "raop_log": "warn",
    "binding": "?",
    "upnp_port": 0,
    "port_base": acpath.DEFAULT_PORT_BASE,
    "port_range": acpath.DEFAULT_PORT_RANGE,
    "devices": [],
}

_INT_FIELDS = {
    "http_length": (-3, 0),
    "upnp_max": (0, 3),
    "enabled_by_default": (0, 1),
    "max_volume": (0, 100),
    "stop_receiver": (0, 1),
    "metadata": (0, 1),
    "flush": (0, 1),
    "drift": (0, 1),
    "max_players": (1, 64),
    "log_limit": (-1, 4096),
    "upnp_port": (0, 65535),
    "port_base": (1024, 65000),
    "port_range": (1, 512),
}


# --------------------------------------------------------------------- 工具
def _atomic_write(path: str, text: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".swp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _xml_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


# --------------------------------------------------------------------- 校验
class ConfigError(ValueError):
    """配置校验失败。"""


def validate(data: Dict[str, Any]) -> Dict[str, Any]:
    """校验并归一化设置，返回清洗后的副本；不合法时抛 ``ConfigError``。"""
    out = dict(DEFAULTS)
    out.update({k: v for k, v in data.items() if k in DEFAULTS})

    mode = str(out.get("mode", "upnp")).lower()
    if mode not in VALID_MODES:
        raise ConfigError(f"不支持的桥接模式：{mode}")
    out["mode"] = mode

    suffix = str(out.get("name_suffix", "+"))
    if "%" in suffix:
        raise ConfigError("AirPlay 名称后缀不能包含百分号 %")
    if len(suffix) > 20:
        raise ConfigError("AirPlay 名称后缀最多 20 个字符")
    out["name_suffix"] = suffix

    latency = str(out.get("latency", "0:0")).strip()
    if latency and not LATENCY_RE.match(latency):
        raise ConfigError("延迟格式应为 [rtp][:http][:f]，例如 0:0 或 1000:2000")
    out["latency"] = latency

    codec = str(out.get("codec", "flac")).strip().lower()
    if not CODEC_RE.match(codec):
        raise ConfigError("编码器应为 mp3[:码率] / aac[:码率] / flac[:级别][/块大小] / wav / pcm")
    out["codec"] = codec

    stream_type = str(out.get("stream_type", "broadcast")).strip().lower()
    if stream_type not in VALID_STREAM_TYPES:
        raise ConfigError("stream_type 只能是 broadcast / track / radio")
    out["stream_type"] = stream_type

    for field in ("main_log", "upnp_log", "cast_log", "util_log", "raop_log"):
        level = str(out.get(field, "warn")).strip().lower()
        if level == "sdebug":
            level = "debug"
        if level not in VALID_LEVELS:
            raise ConfigError(f"{field} 只能是 error / warn / info / debug")
        out[field] = level

    for field, (low, high) in _INT_FIELDS.items():
        try:
            value = int(out.get(field))
        except (TypeError, ValueError):
            raise ConfigError(f"{field} 必须是整数") from None
        if not (low <= value <= high):
            raise ConfigError(f"{field} 必须在 {low}..{high} 之间")
        out[field] = value

    try:
        media_volume = float(out.get("media_volume", 0.5))
    except (TypeError, ValueError):
        raise ConfigError("media_volume 必须是数字") from None
    if not (0.0 <= media_volume <= 1.0):
        raise ConfigError("media_volume 必须在 0..1 之间")
    out["media_volume"] = round(media_volume, 3)

    artwork = str(out.get("artwork", "")).strip()
    if len(artwork) > 512:
        raise ConfigError("artwork 地址过长")
    out["artwork"] = artwork

    binding = str(out.get("binding", "?")).strip()
    if binding and not re.match(r"^[A-Za-z0-9_.:\-?]{1,64}$", binding):
        raise ConfigError("绑定接口只能填接口名、IP 或 ?（自动）")
    out["binding"] = binding or "?"

    # 端口区间不能相互重叠到一个明显无效的段
    if out["port_base"] + out["port_range"] > 65535:
        raise ConfigError("端口区间超出 65535")

    out["devices"] = _sanitize_devices(out.get("devices") or [])
    out["schema_version"] = SCHEMA_VERSION
    return out


def _sanitize_devices(devices: Any) -> List[Dict[str, Any]]:
    if not isinstance(devices, list):
        return []
    result: List[Dict[str, Any]] = []
    seen = set()
    for item in devices:
        if not isinstance(item, dict):
            continue
        udn = str(item.get("udn", "")).strip()
        if not udn or udn in seen:
            continue
        if len(udn) > 200:
            continue
        seen.add(udn)
        name = str(item.get("name", "")).strip()[:80]
        mac = str(item.get("mac", "")).strip()[:32]
        if not re.match(r"^[0-9a-fA-F:]{0,32}$", mac):
            mac = ""
        result.append({
            "udn": udn,
            "name": name,
            "mac": mac,
            "enabled": 1 if int(item.get("enabled", 1) or 0) else 0,
            "friendly_name": str(item.get("friendly_name", "")).strip()[:120],
            "model": str(item.get("model", "")).strip()[:80],
            "bridge": "cast" if str(item.get("bridge", "upnp")).lower() == "cast" else "upnp",
            "last_seen": str(item.get("last_seen", "")).strip()[:32],
        })
    return result


# --------------------------------------------------------------------- 读写
def load(path: Optional[str] = None) -> Dict[str, Any]:
    """读取 settings.json；损坏或缺失时回落到默认值（绝不抛异常）。"""
    path = path or acpath.SETTINGS_FILE
    raw: Dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            raw = loaded
    except (OSError, ValueError):
        raw = {}
    try:
        return validate(raw)
    except ConfigError:
        merged = dict(DEFAULTS)
        merged["devices"] = raw.get("devices") if isinstance(raw.get("devices"), list) else []
        try:
            return validate(merged)
        except ConfigError:  # pragma: no cover - 默认值本身必然合法
            return dict(DEFAULTS)


def save(data: Dict[str, Any], path: Optional[str] = None) -> Dict[str, Any]:
    """校验并原子写入 settings.json，返回落盘后的设置。"""
    clean = validate(data)
    _atomic_write(path or acpath.SETTINGS_FILE,
                  json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=False) + "\n")
    return clean


# --------------------------------------------------------------------- XML
def name_format(settings: Dict[str, Any]) -> str:
    """``-N`` 的取值：上游默认是 ``%s+``，这里由「后缀」拼出来。"""
    return "%s" + str(settings.get("name_suffix", "+"))


def binding_string(settings: Dict[str, Any]) -> str:
    """把「接口 + UPnP 端口」拼成 ``-b`` / ``<binding>`` 的取值。

    ``?`` = 接口与端口都自动；``:49152`` = 接口自动、端口固定；
    ``wlo1`` = 指定接口、端口自动；``wlo1:49152`` = 都指定。
    """
    iface = str(settings.get("binding", "?")).strip()
    if iface in ("", "?", "auto"):
        iface = ""
    port = int(settings.get("upnp_port", 0) or 0)
    if not iface and not port:
        return "?"
    if not iface:
        return f":{port}"
    return f"{iface}:{port}" if port else iface


def render_xml(settings: Dict[str, Any], mode: str) -> str:
    """按上游 ``SaveConfig()`` 的节点顺序生成 AirConnect 配置文件。"""
    is_upnp = mode != "cast"
    root_tag = "airupnp" if is_upnp else "aircast"
    wanted_bridge = "cast" if mode == "cast" else "upnp"
    common = [
        ("enabled", int(settings["enabled_by_default"])),
        ("max_volume", int(settings["max_volume"])),
        ("http_length", int(settings["http_length"])),
        ("stream_type", str(settings["stream_type"])),
    ]
    if is_upnp:
        common.append(("upnp_max", int(settings["upnp_max"])))
    else:
        common.append(("stop_receiver", int(settings["stop_receiver"])))
        common.append(("media_volume", f"{float(settings['media_volume']):g}"))
    common += [
        ("codec", str(settings["codec"])),
        ("metadata", int(settings["metadata"])),
        ("flush", int(settings["flush"])),
        ("artwork", str(settings["artwork"])),
        ("latency", str(settings["latency"])),
        ("drift", int(settings["drift"])),
    ]

    globals_ = [
        ("main_log", str(settings["main_log"])),
        (("upnp_log" if is_upnp else "cast_log"),
         str(settings["upnp_log"] if is_upnp else settings["cast_log"])),
        ("util_log", str(settings["util_log"])),
    ]
    if is_upnp:
        globals_.append(("raop_log", str(settings["raop_log"])))
    globals_ += [
        ("log_limit", int(settings["log_limit"])),
        ("max_players", int(settings["max_players"])),
        ("binding", binding_string(settings)),
        ("ports", f"{int(settings['port_base'])}:{int(settings['port_range'])}"),
    ]

    lines = ['<?xml version="1.0"?>', f"<{root_tag}>", "<common>"]
    for key, value in common:
        lines.append(f"<{key}>{_xml_escape(value)}</{key}>")
    lines.append("</common>")
    for key, value in globals_:
        lines.append(f"<{key}>{_xml_escape(value)}</{key}>")
    for device in settings.get("devices", []):
        if device.get("bridge", "upnp") != wanted_bridge:
            continue
        lines.append("<device>")
        lines.append(f"<udn>{_xml_escape(device['udn'])}</udn>")
        lines.append(f"<name>{_xml_escape(device.get('name', ''))}</name>")
        if device.get("mac"):
            lines.append(f"<mac>{_xml_escape(device['mac'])}</mac>")
        lines.append(f"<enabled>{int(device.get('enabled', 1))}</enabled>")
        lines.append("</device>")
    lines.append(f"</{root_tag}>")
    return "\n".join(lines) + "\n"


def parse_xml(path: str) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    """解析 AirConnect 配置文件，返回 (全局键值, 设备列表)。解析失败返回空。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return {}, []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}, []
    globals_: Dict[str, str] = {}
    devices: List[Dict[str, Any]] = []
    for child in root:
        tag = child.tag
        if tag == "device":
            item: Dict[str, Any] = {}
            for node in child:
                item[node.tag] = (node.text or "").strip()
            if item.get("udn"):
                devices.append({
                    "udn": item["udn"],
                    "name": item.get("name", ""),
                    "mac": item.get("mac", ""),
                    "enabled": 1 if str(item.get("enabled", "1")).strip() not in ("0", "") else 0,
                })
        elif tag == "common":
            # <common> 里的键摊平到顶层，方便调用方读取「当前生效的通用设置」
            for node in child:
                globals_[node.tag] = (node.text or "").strip()
        else:
            globals_[tag] = (child.text or "").strip()
    return globals_, devices


def sync_devices(settings: Dict[str, Any], mode: str) -> Tuple[Dict[str, Any], bool]:
    """把 AirConnect 自动保存的设备合并回 settings（XML 为准）。

    只处理当前模式涉及的 XML；``both`` 时两份都读，并按来源打上 ``bridge`` 标记，
    这样 UI 能区分「这是 UPnP 音响」还是「这是 Chromecast」。
    """
    sources = []
    if mode in ("upnp", "both"):
        sources.append(("upnp", acpath.AIRUPNP_XML))
    if mode in ("cast", "both"):
        sources.append(("cast", acpath.AIRCAST_XML))

    known = {d["udn"]: d for d in settings.get("devices", [])}
    order: List[str] = []
    changed = False
    for bridge, path in sources:
        _, xml_devices = parse_xml(path)
        for found in xml_devices:
            udn = found["udn"]
            if udn not in order:
                order.append(udn)
            current = known.get(udn)
            if current is None:
                known[udn] = {
                    "udn": udn,
                    "name": found["name"],
                    "mac": found["mac"],
                    "enabled": found["enabled"],
                    "friendly_name": "",
                    "model": "",
                    "bridge": bridge,
                    "last_seen": "",
                }
                changed = True
                continue
            if found["mac"] and current.get("mac") != found["mac"]:
                current["mac"] = found["mac"]
                changed = True
            if found["name"] and current.get("name") != found["name"]:
                current["name"] = found["name"]
                changed = True
            if current.get("enabled") != found["enabled"]:
                current["enabled"] = found["enabled"]
                changed = True

    # 稳定顺序：XML 里出现的顺序优先，其余保持原样追加在后面。
    # 注意用 known（已包含本次新发现的设备）来取，不能再用旧的 devices 列表，
    # 否则刚发现的设备会被这一步过滤掉 —— 表现为「AirConnect 明明发现了设备，
    # 界面上却一直是空的」。
    ordered = [known[u] for u in order if u in known]
    seen = set(order)
    merged = ordered + [d for u, d in known.items() if u not in seen]
    if merged != settings.get("devices", []):
        changed = True
    settings = dict(settings)
    settings["devices"] = merged
    return settings, changed


def write_xml_files(settings: Dict[str, Any]) -> None:
    """把当前设置写成 airupnp.xml（以及需要时 aircast.xml）。"""
    mode = settings.get("mode", "upnp")
    if mode in ("upnp", "both"):
        _atomic_write(acpath.AIRUPNP_XML, render_xml(settings, "upnp"))
    if mode in ("cast", "both"):
        _atomic_write(acpath.AIRCAST_XML, render_xml(settings, "cast"))
