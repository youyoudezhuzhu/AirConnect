#!/usr/bin/env python3
"""独立探测 DLNA 渲染器的 AVTransport 状态，用于把「延迟」拆成时间线。

不依赖 AirConnect 自身的日志：直接对音箱发 UPnP 查询，每 0.5 秒记录一次
TransportState 与 RelTime。这样就能区分：

  * 听到声音停了   —— 但音箱还报 PLAYING  → 停的是 AirConnect 的音频供给
  * 音箱报 STOPPED —— 而声音还在响        → 停的是音箱自己的缓冲

用法: python3 dlna_probe.py [--seconds 180] [--interval 0.5] [--out /tmp/probe.log]
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

SSDP_ADDR = ("239.255.255.250", 1900)
# 注意：<serviceType>/<controlURL> 属于**设备**命名空间，不是 service 命名空间。
# 用 urn:schemas-upnp-org:service-1-0 去找会静默找不到任何服务。
D = "urn:schemas-upnp-org:device-1-0"

GET_TRANSPORT_INFO = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body><u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
<InstanceID>0</InstanceID></u:GetTransportInfo></s:Body></s:Envelope>"""

GET_POSITION_INFO = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body><u:GetPositionInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
<InstanceID>0</InstanceID></u:GetPositionInfo></s:Body></s:Envelope>"""


def ssdp_find(timeout: float = 4.0) -> list[str]:
    msg = "\r\n".join([
        "M-SEARCH * HTTP/1.1",
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}",
        'MAN: "ssdp:discover"',
        "MX: 2",
        "ST: urn:schemas-upnp-org:device:MediaRenderer:1",
        "", "",
    ]).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    found: list[str] = []
    try:
        sock.sendto(msg, SSDP_ADDR)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                break
            text = data.decode("utf-8", "replace")
            match = re.search(r"LOCATION:\s*(\S+)", text, re.I)
            if match and match.group(1) not in found:
                found.append(match.group(1))
    finally:
        sock.close()
    return found


def control_url(location: str) -> tuple[str, str]:
    """返回 (friendlyName, AVTransport 的 controlURL 绝对地址)。"""
    with urllib.request.urlopen(location, timeout=5) as resp:
        root = ET.fromstring(resp.read())
    name = (root.findtext(f".//{{{D}}}friendlyName", default="?", namespaces={}) or "?").strip()
    model = (root.findtext(f".//{{{D}}}modelName", default="", namespaces={}) or "").strip()
    if model:
        name = f"{name} (model={model})"
    for service in root.iter(f"{{{D}}}service"):
        stype = service.findtext(f"{{{D}}}serviceType") or ""
        if "AVTransport" in stype:
            ctrl = service.findtext(f"{{{D}}}controlURL") or ""
            base = re.match(r"(https?://[^/]+)", location).group(1)
            if ctrl.startswith("http"):
                return name, ctrl
            return name, base + ("" if ctrl.startswith("/") else "/") + ctrl
    raise RuntimeError("设备描述里没有 AVTransport 服务")


def soap(url: str, action: str, body: str) -> str:
    request = urllib.request.Request(
        url, data=body.encode(),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
        })
    with urllib.request.urlopen(request, timeout=4) as resp:
        return resp.read().decode("utf-8", "replace")


def pick(xml_text: str, tag: str) -> str:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", xml_text, re.S)
    return match.group(1) if match else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=180)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--out", default="/tmp/dlna-probe.log")
    args = parser.parse_args()

    locations = ssdp_find()
    if not locations:
        print("SSDP 没有发现 MediaRenderer", file=sys.stderr)
        return 1
    target = None
    for location in locations:
        try:
            name, url = control_url(location)
        except Exception as exc:  # noqa: BLE001
            print(f"跳过 {location}: {exc}", file=sys.stderr)
            continue
        print(f"发现渲染器: {name}  {url}")
        if target is None:
            target = (name, url)
    if target is None:
        return 1
    name, url = target

    print(f"开始探测 {args.seconds:.0f} 秒，每 {args.interval}s 一次 → {args.out}")
    start = time.time()
    last_state = ""
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(f"# 渲染器: {name}\n# 字段: 本地时间 | 相对启动秒 | TransportState | RelTime | TrackURI\n")
        while time.time() - start < args.seconds:
            stamp = time.strftime("%H:%M:%S")
            elapsed = time.time() - start
            try:
                info = soap(url, "GetTransportInfo", GET_TRANSPORT_INFO)
                state = pick(info, "CurrentTransportState")
                pos = soap(url, "GetPositionInfo", GET_POSITION_INFO)
                rel = pick(pos, "RelTime")
                uri = pick(pos, "TrackURI")
            except Exception as exc:  # noqa: BLE001
                state, rel, uri = f"ERR({type(exc).__name__})", "", ""
            line = f"{stamp} {elapsed:7.1f} {state:18s} {rel:>12s} {uri}"
            handle.write(line + "\n")
            handle.flush()
            if state != last_state:
                print(f"  {stamp} 状态变化 → {state}  rel={rel}")
                last_state = state
            time.sleep(args.interval)
    print(f"完成，日志：{args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
