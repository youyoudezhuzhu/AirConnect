#!/usr/bin/env python3
"""端到端测试：真的把管理服务跑起来，用 HTTP 打它。

用「假 airupnp / aircast」脚本替换真二进制，因此不需要局域网里有音响，
也不需要 root。覆盖的都是真机上最容易翻车的地方：

  * 健康检查（cmd/main 判活依据）
  * 飞牛统一网关：套接字、裸前缀 307、剥前缀后的静态资源
  * CSRF 头缺失必须 403；非法配置必须 400 且不落盘
  * 保存设置 → 重写 AirConnect XML → 重启桥接进程（且命令行参数正确）
  * 设备启用/禁用/改名 → XML 生效
  * 日志接口与子进程输出的落盘
  * SIGTERM 能干净退出（不留 airupnp/aircast 孤儿进程）

运行：python3 tests/test_service.py
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

FNOX_DIR = Path(__file__).resolve().parent.parent
APP_DIR = FNOX_DIR / "app"
SERVER_DIR = APP_DIR / "server"

STUB = """#!/bin/sh
# 假桥接二进制：只回放日志并常驻
case "$*" in
  *-h*) echo "v1.12.4 (stub)"; exit 0 ;;
esac
echo "ARGV $0 $*" >> "$STUB_ARGV_LOG"
echo "[00:00:00.100] main:1407 Starting $(basename "$0") version: v1.12.4 (stub)"
echo "[00:00:00.200] Start:1112 Binding to iface 127.0.0.1:0 [lo]"
echo "[00:00:00.300] AddMRDevice:1038 [0x0]: adding renderer (测试音箱) with mac BBBB72C30EDB"
while true; do sleep 0.5; done
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="airconnect-e2e-")
        root = Path(cls.tmp.name)
        cls.target = root / "target"
        cls.var = root / "var"
        cls.etc = root / "etc"
        cls.home = root / "home"
        for path in (cls.target, cls.var, cls.etc, cls.home):
            path.mkdir(parents=True, exist_ok=True)
        shutil.copytree(APP_DIR, cls.target, dirs_exist_ok=True)

        cls.argv_log = root / "argv.log"
        # 全新 clone 里 fnos/app/bin 不存在（二进制由 build.sh 生成、被 .gitignore
        # 排除），必须先建出来，否则写假二进制会 FileNotFoundError。
        (cls.target / "bin").mkdir(parents=True, exist_ok=True)
        for name in ("airupnp", "aircast"):
            stub = cls.target / "bin" / name
            stub.write_text(STUB, encoding="utf-8")
            stub.chmod(0o755)

        cls.port = free_port()
        cls.socket_path = cls.target / "airconnect.sock"
        cls.env = dict(os.environ)
        cls.env.update({
            "TRIM_APPDEST": str(cls.target),
            "TRIM_PKGVAR": str(cls.var),
            "TRIM_PKGETC": str(cls.etc),
            "TRIM_PKGHOME": str(cls.home),
            "TRIM_SERVICE_PORT": str(cls.port),
            "TRIM_APPVER": "1.12.4-1",
            "TRIM_USERNAME": "root",
            "GATEWAY_SOCKET": str(cls.socket_path),
            "GATEWAY_PREFIX": "/app/airconnect",
            "STUB_ARGV_LOG": str(cls.argv_log),
        })
        cls.proc = subprocess.Popen(
            [sys.executable, str(cls.target / "server" / "main.py"),
             "--ui-dir", str(cls.target / "ui"), "--port", str(cls.port),
             "--version", "1.12.4-1"],
            env=cls.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                out = cls.proc.stdout.read().decode("utf-8", "replace") if cls.proc.stdout else ""
                raise RuntimeError(f"服务提前退出：\n{out}")
            try:
                status, _ = cls.request("GET", "/api/health")
                if status == 200:
                    return
            except OSError:
                pass
            time.sleep(0.3)
        raise RuntimeError("服务未在 30 秒内就绪")

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "proc", None) and cls.proc.poll() is None:
            cls.proc.send_signal(signal.SIGTERM)
            try:
                cls.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(cls.proc.pid), signal.SIGKILL)
        cls.tmp.cleanup()

    # ------------------------------------------------------------ 工具
    @classmethod
    def request(cls, method: str, path: str, body=None, headers=None, unix=False):
        payload = None
        hdrs = dict(headers or {})
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        if unix:
            conn = http.client.HTTPConnection("localhost")
            conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.sock.connect(str(cls.socket_path))
        else:
            conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=30)
        try:
            conn.request(method, path, body=payload, headers=hdrs)
            response = conn.getresponse()
            data = response.read()
            return response.status, data
        finally:
            conn.close()

    def api(self, method, path, body=None, csrf=True):
        headers = {"X-Requested-With": "XMLHttpRequest"} if csrf else {}
        status, data = self.request(method, path, body, headers)
        return status, json.loads(data.decode("utf-8")) if data else {}

    def xml(self, name="airupnp") -> str:
        return (self.home / f"{name}.xml").read_text(encoding="utf-8")

    # ------------------------------------------------------------ 健康 / 状态
    def test_01_health(self):
        status, data = self.api("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["version"], "1.12.4-1")
        self.assertEqual(data["mode"], "upnp")
        self.assertTrue(data["running"])
        # 规则 #16：HOME 必须被无条件覆盖成应用家目录
        self.assertEqual(data["home"], str(self.home))

    def test_02_status_shape(self):
        status, data = self.api("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["app"]["port"], self.port)
        self.assertIn("v1.12.4", data["binaries"]["airupnp"])
        procs = {p["binary"]: p for p in data["bridge"]["processes"]}
        self.assertTrue(procs["airupnp"]["running"])
        self.assertFalse(procs["aircast"]["running"])
        self.assertEqual(data["paths"]["socket"], str(self.socket_path))

    def test_03_stub_argv_has_required_flags(self):
        deadline = time.time() + 10
        text = ""
        while time.time() < deadline:
            if self.argv_log.exists():
                text = self.argv_log.read_text(encoding="utf-8")
                if "airupnp" in text:
                    break
            time.sleep(0.2)
        self.assertIn("-Z", text)                      # 非交互，否则 CPU 空转
        self.assertIn("-I", text)                      # 发现新设备时自动保存配置
        self.assertIn(f"-x {self.home}/airupnp.xml", text)
        self.assertIn("-N %s+", text)

    # ------------------------------------------------------------ 网关
    def test_04_gateway_socket_and_prefix(self):
        status, data = self.request("GET", "/app/airconnect/api/health", unix=True)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["ok"])

        status, _ = self.request("GET", "/app/airconnect", unix=True)
        self.assertEqual(status, 307)

        for path in ("/app/airconnect/", "/app/airconnect/index.html",
                     "/app/airconnect/css/app.css", "/app/airconnect/js/app.js",
                     "/app/airconnect/images/icon_64.png"):
            with self.subTest(path=path):
                status, body = self.request("GET", path, unix=True)
                self.assertEqual(status, 200)
                self.assertGreater(len(body), 0)

    def test_05_static_traversal_blocked(self):
        status, _ = self.request("GET", "/app/airconnect/../manifest", unix=True)
        self.assertIn(status, (400, 403, 404))

    # ------------------------------------------------------------ 校验 / CSRF
    def test_06_csrf_required(self):
        status, data = self.api("POST", "/api/settings", {"mode": "cast"}, csrf=False)
        self.assertEqual(status, 403)
        self.assertFalse(data["ok"])

    def test_07_validation_rejects_and_keeps_file(self):
        before = (self.etc / "settings.json").read_text(encoding="utf-8")
        for payload in ({"mode": "nope"}, {"codec": "ogg"}, {"latency": "xx"},
                        {"name_suffix": "bad%suffix"}, {"max_players": 1000}):
            with self.subTest(payload=payload):
                status, data = self.api("POST", "/api/settings", payload)
                self.assertEqual(status, 400)
                self.assertFalse(data["ok"])
        self.assertEqual(before, (self.etc / "settings.json").read_text(encoding="utf-8"))

    # ------------------------------------------------------------ 设置与重启
    def test_08_save_settings_rewrites_xml_and_restarts(self):
        _, before = self.api("GET", "/api/status")
        pid_before = {p["binary"]: p["pid"] for p in before["bridge"]["processes"]}

        status, data = self.api("POST", "/api/settings",
                                {"codec": "mp3:320", "latency": "1000:2000",
                                 "name_suffix": "·NAS", "port_base": 18400,
                                 "port_range": 64, "main_log": "warn"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        xml = self.xml()
        self.assertIn("<codec>mp3:320</codec>", xml)
        self.assertIn("<latency>1000:2000</latency>", xml)
        self.assertIn("<ports>18400:64</ports>", xml)
        self.assertIn("<main_log>warn</main_log>", xml)

        deadline = time.time() + 20
        while time.time() < deadline:
            _, after = self.api("GET", "/api/status")
            procs = {p["binary"]: p for p in after["bridge"]["processes"]}
            if procs["airupnp"]["pid"] not in (None, pid_before["airupnp"]):
                break
            time.sleep(0.3)
        self.assertNotEqual(procs["airupnp"]["pid"], pid_before["airupnp"])
        self.assertTrue(procs["airupnp"]["running"])
        # -N 必须跟着后缀一起更新
        argv = self.argv_log.read_text(encoding="utf-8")
        self.assertIn("-N %s·NAS", argv)

    def test_09_mode_switch_starts_and_stops_bridges(self):
        status, _ = self.api("POST", "/api/settings", {"mode": "both"})
        self.assertEqual(status, 200)
        deadline = time.time() + 20
        procs = {}
        while time.time() < deadline:
            _, data = self.api("GET", "/api/status")
            procs = {p["binary"]: p for p in data["bridge"]["processes"]}
            if procs["airupnp"]["running"] and procs["aircast"]["running"]:
                break
            time.sleep(0.3)
        self.assertTrue(procs["airupnp"]["running"])
        self.assertTrue(procs["aircast"]["running"])
        self.assertTrue((self.home / "aircast.xml").exists())
        self.assertIn("<aircast>", self.xml("aircast"))

        status, _ = self.api("POST", "/api/settings", {"mode": "upnp"})
        self.assertEqual(status, 200)
        deadline = time.time() + 20
        while time.time() < deadline:
            _, data = self.api("GET", "/api/status")
            procs = {p["binary"]: p for p in data["bridge"]["processes"]}
            if not procs["aircast"]["running"]:
                break
            time.sleep(0.3)
        self.assertFalse(procs["aircast"]["running"])
        self.assertTrue(procs["airupnp"]["running"])

    def test_10_stop_and_start_actions(self):
        status, _ = self.api("POST", "/api/action", {"action": "stop"})
        self.assertEqual(status, 200)
        deadline = time.time() + 20
        while time.time() < deadline:
            _, data = self.api("GET", "/api/status")
            if not data["bridge"]["running"]:
                break
            time.sleep(0.3)
        self.assertFalse(data["bridge"]["running"])

        status, _ = self.api("POST", "/api/action", {"action": "start"})
        self.assertEqual(status, 200)
        deadline = time.time() + 25
        while time.time() < deadline:
            _, data = self.api("GET", "/api/status")
            if data["bridge"]["running"]:
                break
            time.sleep(0.3)
        self.assertTrue(data["bridge"]["running"])

    def test_11_unknown_action_rejected(self):
        status, data = self.api("POST", "/api/action", {"action": "selfdestruct"})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    # ------------------------------------------------------------ 设备
    def test_12_devices_pick_up_autosaved_xml(self):
        """模拟 AirConnect 的 -I 自动保存：把设备写进 XML，接口应能读出来。"""
        xml = self.xml()
        device = ("<device>\n<udn>uuid:test-device</udn>\n<name>测试音箱·NAS</name>\n"
                  "<mac>bb:bb:db:0e:c3:72</mac>\n<enabled>1</enabled>\n</device>\n")
        (self.home / "airupnp.xml").write_text(
            xml.replace("</airupnp>", device + "</airupnp>"), encoding="utf-8")

        status, data = self.api("GET", "/api/devices")
        self.assertEqual(status, 200)
        udns = [d["udn"] for d in data["devices"]]
        self.assertIn("uuid:test-device", udns)

        # 日志里出现过同名设备 → 应回填 friendly_name 与 last_seen
        entry = next(d for d in data["devices"] if d["udn"] == "uuid:test-device")
        self.assertEqual(entry["friendly_name"], "测试音箱")
        self.assertTrue(entry["last_seen"])

    def test_13_disable_device_writes_xml(self):
        status, data = self.api("POST", "/api/devices",
                                {"udn": "uuid:test-device", "enabled": 0,
                                 "name": "客厅小爱"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        xml = self.xml()
        self.assertIn("<name>客厅小爱</name>", xml)
        self.assertIn("<enabled>0</enabled>", xml)

        _, data = self.api("GET", "/api/devices")
        entry = next(d for d in data["devices"] if d["udn"] == "uuid:test-device")
        self.assertEqual(entry["enabled"], 0)
        self.assertEqual(entry["name"], "客厅小爱")

    def test_14_device_validation(self):
        status, data = self.api("POST", "/api/devices", {"udn": "uuid:missing", "enabled": 0})
        self.assertEqual(status, 400)
        status, data = self.api("POST", "/api/devices",
                                {"udn": "uuid:test-device", "name": 'bad"name'})
        self.assertEqual(status, 400)

    def test_15_forget_device(self):
        status, data = self.api("POST", "/api/devices",
                                {"action": "forget", "udn": "uuid:test-device"})
        self.assertEqual(status, 200)
        _, data = self.api("GET", "/api/devices")
        self.assertNotIn("uuid:test-device", [d["udn"] for d in data["devices"]])

    # ------------------------------------------------------------ 日志
    def test_16_logs_endpoint(self):
        status, data = self.api("GET", "/api/logs?source=airupnp&lines=50")
        self.assertEqual(status, 200)
        self.assertIn("adding renderer", data["text"])

        status, data = self.api("GET", "/api/logs?source=server&lines=50")
        self.assertEqual(status, 200)
        self.assertIn("管理服务启动", data["text"])

        status, body = self.request("GET", "/api/logs/download?source=airupnp")
        self.assertEqual(status, 200)
        self.assertIn(b"adding renderer", body)

    def test_17_interfaces_listed(self):
        status, data = self.api("GET", "/api/interfaces")
        self.assertEqual(status, 200)
        self.assertIsInstance(data["interfaces"], list)

    def test_18_unknown_api_404(self):
        status, _ = self.api("GET", "/api/nope")
        self.assertEqual(status, 404)


class ShutdownTest(unittest.TestCase):
    """单独一个用例：验证 SIGTERM 之后没有孤儿桥接进程。"""

    def test_sigterm_leaves_no_orphans(self):
        with tempfile.TemporaryDirectory(prefix="airconnect-stop-") as tmp:
            root = Path(tmp)
            target = root / "target"
            shutil.copytree(APP_DIR, target, dirs_exist_ok=True)
            for sub in ("var", "etc", "home"):
                (root / sub).mkdir(parents=True, exist_ok=True)
            marker = root / "argv.log"
            (target / "bin").mkdir(parents=True, exist_ok=True)
            for name in ("airupnp", "aircast"):
                stub = target / "bin" / name
                stub.write_text(STUB, encoding="utf-8")
                stub.chmod(0o755)

            port = free_port()
            env = dict(os.environ)
            env.update({
                "TRIM_APPDEST": str(target), "TRIM_PKGVAR": str(root / "var"),
                "TRIM_PKGETC": str(root / "etc"), "TRIM_PKGHOME": str(root / "home"),
                "TRIM_SERVICE_PORT": str(port), "TRIM_APPVER": "1.12.4-1",
                "TRIM_USERNAME": "root",
                "GATEWAY_SOCKET": str(target / "airconnect.sock"),
                "GATEWAY_PREFIX": "/app/airconnect", "STUB_ARGV_LOG": str(marker),
            })
            proc = subprocess.Popen(
                [sys.executable, str(target / "server" / "main.py"),
                 "--ui-dir", str(target / "ui"), "--port", str(port)],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True)
            try:
                deadline = time.time() + 30
                ready = False
                while time.time() < deadline:
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=1):
                            ready = True
                            break
                    except OSError:
                        time.sleep(0.3)
                self.assertTrue(ready, "服务未就绪")

                # 找出桥接子进程
                time.sleep(1.5)
                children = subprocess.run(
                    ["pgrep", "-f", f"{target}/bin/airupnp"],  # noqa: S603,S607
                    capture_output=True, text=True, check=False).stdout.split()
                self.assertTrue(children, "没有找到 airupnp 子进程")

                proc.send_signal(signal.SIGTERM)
                self.assertEqual(proc.wait(timeout=25), 0)

                deadline = time.time() + 15
                alive = children
                while time.time() < deadline:
                    alive = subprocess.run(
                        ["pgrep", "-f", f"{target}/bin/airupnp"],  # noqa: S603,S607
                        capture_output=True, text=True, check=False).stdout.split()
                    if not alive:
                        break
                    time.sleep(0.5)
                self.assertEqual(alive, [], f"退出后仍有孤儿进程：{alive}")
            finally:
                if proc.poll() is None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
