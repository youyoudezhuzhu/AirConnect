#!/usr/bin/env python3
"""acconf / aclog 的单元测试。

覆盖的都是真机上踩过的缺陷：
  * 设备合并时把「本次新发现的设备」丢掉，界面永远空白；
  * 生成的 XML 与上游 ``SaveConfig()`` 的节点顺序/取值不一致；
  * 校验缺失导致非法值写进 XML，AirConnect 静默不生效。

运行：python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent / "app" / "server"
sys.path.insert(0, str(SERVER_DIR))


def _reload_modules(tmp: str):
    """把 acpath 的路径指到临时目录，并重新导入依赖它的模块。"""
    os.environ["TRIM_APPDEST"] = os.path.join(tmp, "target")
    os.environ["TRIM_PKGVAR"] = os.path.join(tmp, "var")
    os.environ["TRIM_PKGETC"] = os.path.join(tmp, "etc")
    os.environ["TRIM_PKGHOME"] = os.path.join(tmp, "home")
    for name in ("acpath", "aclog", "acconf"):
        sys.modules.pop(name, None)
    import acpath  # noqa: F401
    import aclog  # noqa: F401
    import acconf  # noqa: F401
    for module in (acpath, aclog, acconf):
        pass
    return sys.modules["acpath"], sys.modules["aclog"], sys.modules["acconf"]


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.acpath, self.aclog, self.acconf = _reload_modules(self.tmp.name)
        for sub in ("target", "var", "etc", "home"):
            os.makedirs(os.path.join(self.tmp.name, sub), exist_ok=True)

    # ------------------------------------------------------------ 校验
    def test_defaults_validate(self):
        clean = self.acconf.validate({})
        self.assertEqual(clean["mode"], "upnp")
        self.assertEqual(clean["name_suffix"], "+")
        self.assertEqual(clean["port_base"], self.acpath.DEFAULT_PORT_BASE)

    def test_rejects_bad_values(self):
        cases = [
            ({"mode": "dlna"}, "桥接模式"),
            ({"name_suffix": "a%sb"}, "百分号"),
            ({"name_suffix": "x" * 21}, "20"),
            ({"codec": "ogg"}, "编码器"),
            ({"latency": "abc"}, "延迟"),
            ({"stream_type": "weird"}, "stream_type"),
            ({"main_log": "verbose"}, "main_log"),
            ({"max_players": 999}, "max_players"),
            ({"port_base": 70000}, "port_base"),
            ({"media_volume": 3}, "media_volume"),
        ]
        for payload, needle in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(self.acconf.ConfigError) as ctx:
                    self.acconf.validate(payload)
                self.assertIn(needle, str(ctx.exception))

    def test_codec_and_latency_syntax(self):
        for codec in ("flac", "flac:5", "flac:8/1152", "mp3", "mp3:320", "aac:256",
                      "wav", "pcm", "FLAC"):
            with self.subTest(codec=codec):
                self.assertTrue(self.acconf.validate({"codec": codec})["codec"])
        for latency in ("0:0", "1000:2000", "1000:2000:f", "-500:0", ""):
            with self.subTest(latency=latency):
                self.acconf.validate({"latency": latency})

    def test_sdebug_normalized(self):
        self.assertEqual(self.acconf.validate({"main_log": "sdebug"})["main_log"], "debug")

    # ------------------------------------------------------------ 命名 / 绑定
    def test_name_format(self):
        self.assertEqual(self.acconf.name_format({"name_suffix": "+"}), "%s+")
        self.assertEqual(self.acconf.name_format({"name_suffix": ""}), "%s")

    def test_binding_string(self):
        self.assertEqual(self.acconf.binding_string({"binding": "?", "upnp_port": 0}), "?")
        self.assertEqual(self.acconf.binding_string({"binding": "?", "upnp_port": 49152}), ":49152")
        self.assertEqual(self.acconf.binding_string({"binding": "wlo1", "upnp_port": 0}), "wlo1")
        self.assertEqual(self.acconf.binding_string({"binding": "wlo1", "upnp_port": 49152}),
                         "wlo1:49152")

    # ------------------------------------------------------------ XML
    def test_render_xml_matches_upstream_layout(self):
        settings = self.acconf.validate({})
        xml = self.acconf.render_xml(settings, "upnp")
        lines = xml.splitlines()
        self.assertEqual(lines[0], '<?xml version="1.0"?>')
        self.assertEqual(lines[1], "<airupnp>")
        self.assertEqual(lines[2], "<common>")
        # <common> 必须排在全局键之前，设备在最后
        self.assertLess(xml.index("</common>"), xml.index("<main_log>"))
        self.assertLess(xml.index("<ports>"), xml.index("</airupnp>"))
        for key in ("<enabled>1</enabled>", "<codec>flac</codec>", "<latency>0:0</latency>",
                    "<binding>?</binding>", "<ports>18300:128</ports>",
                    "<max_players>32</max_players>"):
            self.assertIn(key, xml)

    def test_render_xml_cast_root_and_keys(self):
        xml = self.acconf.render_xml(self.acconf.validate({}), "cast")
        self.assertIn("<aircast>", xml)
        self.assertIn("<cast_log>", xml)
        self.assertIn("<stop_receiver>", xml)
        self.assertNotIn("<upnp_log>", xml)
        self.assertNotIn("<raop_log>", xml)
        self.assertNotIn("<upnp_max>", xml)

    def test_render_xml_escapes_and_filters_bridge(self):
        settings = self.acconf.validate({
            "devices": [
                {"udn": "uuid:upnp-1", "name": "客厅 & <音响>", "mac": "bb:bb:db:0e:c3:72",
                 "enabled": 1, "bridge": "upnp"},
                {"udn": "uuid:cast-1", "name": "卧室", "mac": "", "enabled": 0,
                 "bridge": "cast"},
            ]
        })
        upnp_xml = self.acconf.render_xml(settings, "upnp")
        self.assertIn("客厅 &amp; &lt;音响&gt;", upnp_xml)
        self.assertIn("uuid:upnp-1", upnp_xml)
        self.assertNotIn("uuid:cast-1", upnp_xml)

        cast_xml = self.acconf.render_xml(settings, "cast")
        self.assertIn("uuid:cast-1", cast_xml)
        self.assertNotIn("uuid:upnp-1", cast_xml)

    def test_parse_xml_roundtrip(self):
        settings = self.acconf.validate({
            "devices": [{"udn": "uuid:a", "name": "音响+", "mac": "bb:bb:db:0e:c3:72",
                         "enabled": 0, "bridge": "upnp"}]
        })
        self.acconf.write_xml_files(settings)
        globals_, devices = self.acconf.parse_xml(self.acpath.AIRUPNP_XML)
        self.assertEqual(globals_["codec"], "flac")
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["udn"], "uuid:a")
        self.assertEqual(devices[0]["enabled"], 0)

    def test_parse_xml_tolerates_garbage(self):
        Path(self.acpath.AIRUPNP_XML).write_text("<airupnp><common>", encoding="utf-8")
        globals_, devices = self.acconf.parse_xml(self.acpath.AIRUPNP_XML)
        self.assertEqual(globals_, {})
        self.assertEqual(devices, [])

    # ------------------------------------------------------------ 设备合并
    def test_sync_devices_adds_new_ones(self):
        """回归：新发现的设备曾被 merge 步骤过滤掉，界面永远空白。"""
        settings = self.acconf.validate({})
        self.acconf.write_xml_files(settings)
        Path(self.acpath.AIRUPNP_XML).write_text(
            '<?xml version="1.0"?>\n<airupnp>\n<common><enabled>1</enabled></common>\n'
            '<device>\n<udn>uuid:new</udn>\n<name>新音箱+</name>\n'
            '<mac>bb:bb:db:0e:c3:72</mac>\n<enabled>1</enabled>\n</device>\n</airupnp>\n',
            encoding="utf-8")

        merged, changed = self.acconf.sync_devices(settings, "upnp")
        self.assertTrue(changed)
        self.assertEqual(len(merged["devices"]), 1)
        self.assertEqual(merged["devices"][0]["udn"], "uuid:new")
        self.assertEqual(merged["devices"][0]["bridge"], "upnp")

    def test_sync_devices_keeps_known_but_absent_devices(self):
        settings = self.acconf.validate({
            "devices": [{"udn": "uuid:gone", "name": "老设备", "mac": "", "enabled": 1,
                         "bridge": "upnp"}]
        })
        Path(self.acpath.AIRUPNP_XML).write_text(
            '<?xml version="1.0"?>\n<airupnp>\n<common><enabled>1</enabled></common>\n'
            '<device>\n<udn>uuid:new</udn>\n<name>新设备+</name>\n<enabled>1</enabled>\n'
            '</device>\n</airupnp>\n', encoding="utf-8")

        merged, _ = self.acconf.sync_devices(settings, "upnp")
        udns = [d["udn"] for d in merged["devices"]]
        # XML 里出现的排前面，已知但没出现的不能被丢掉
        self.assertEqual(udns[0], "uuid:new")
        self.assertIn("uuid:gone", udns)

    def test_sync_devices_picks_up_enabled_and_name(self):
        settings = self.acconf.validate({
            "devices": [{"udn": "uuid:a", "name": "旧名", "mac": "", "enabled": 1,
                         "bridge": "upnp"}]
        })
        Path(self.acpath.AIRUPNP_XML).write_text(
            '<?xml version="1.0"?>\n<airupnp>\n<common><enabled>1</enabled></common>\n'
            '<device>\n<udn>uuid:a</udn>\n<name>新名</name>\n'
            '<mac>aa:bb:cc:dd:ee:ff</mac>\n<enabled>0</enabled>\n</device>\n</airupnp>\n',
            encoding="utf-8")
        merged, changed = self.acconf.sync_devices(settings, "upnp")
        self.assertTrue(changed)
        self.assertEqual(merged["devices"][0]["name"], "新名")
        self.assertEqual(merged["devices"][0]["enabled"], 0)
        self.assertEqual(merged["devices"][0]["mac"], "aa:bb:cc:dd:ee:ff")

    def test_sync_devices_produces_no_change_when_equal(self):
        settings = self.acconf.validate({
            "devices": [{"udn": "uuid:a", "name": "A", "mac": "aa:bb:cc:dd:ee:ff",
                         "enabled": 1, "bridge": "upnp"}]
        })
        self.acconf.write_xml_files(settings)
        merged, changed = self.acconf.sync_devices(settings, "upnp")
        self.assertFalse(changed)
        self.assertEqual(merged["devices"][0]["udn"], "uuid:a")

    def test_device_sanitizing(self):
        settings = self.acconf.validate({"devices": [
            {"udn": "uuid:a", "name": "A", "mac": "zz:zz", "enabled": 1},
            {"udn": "uuid:a", "name": "dup", "enabled": 1},
            {"udn": "", "name": "empty"},
            "not-a-dict",
        ]})
        self.assertEqual(len(settings["devices"]), 1)
        self.assertEqual(settings["devices"][0]["mac"], "")

    # ------------------------------------------------------------ 落盘
    def test_save_and_load_roundtrip(self):
        clean = self.acconf.save({"mode": "both", "name_suffix": "·NAS"})
        self.assertEqual(clean["mode"], "both")
        loaded = self.acconf.load()
        self.assertEqual(loaded["name_suffix"], "·NAS")
        raw = json.loads(Path(self.acpath.SETTINGS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], self.acconf.SCHEMA_VERSION)

    def test_load_corrupt_file_falls_back(self):
        Path(self.acpath.SETTINGS_FILE).write_text("{not json", encoding="utf-8")
        loaded = self.acconf.load()
        self.assertEqual(loaded["mode"], "upnp")

    def test_load_out_of_range_field_recovers(self):
        Path(self.acpath.SETTINGS_FILE).write_text(
            json.dumps({"mode": "bogus", "devices": [{"udn": "uuid:a"}]}), encoding="utf-8")
        loaded = self.acconf.load()
        self.assertEqual(loaded["mode"], "upnp")
        self.assertEqual(len(loaded["devices"]), 1)

    def test_write_xml_files_only_writes_enabled_modes(self):
        os.unlink(self.acpath.AIRCAST_XML) if os.path.exists(self.acpath.AIRCAST_XML) else None
        self.acconf.write_xml_files(self.acconf.validate({"mode": "upnp"}))
        self.assertTrue(os.path.exists(self.acpath.AIRUPNP_XML))
        self.assertFalse(os.path.exists(self.acpath.AIRCAST_XML))
        self.acconf.write_xml_files(self.acconf.validate({"mode": "cast"}))
        self.assertTrue(os.path.exists(self.acpath.AIRCAST_XML))


class LogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.acpath, self.aclog, self.acconf = _reload_modules(self.tmp.name)
        self.path = os.path.join(self.tmp.name, "app.log")

    def test_trim_inplace_keeps_inode_and_fd(self):
        with open(self.path, "wb") as handle:
            for i in range(500):
                handle.write(("line%04d\n" % i).encode())
        inode = os.stat(self.path).st_ino
        # 模拟子进程持有的 O_APPEND 描述符
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
        self.addCleanup(os.close, fd)

        rotated = self.aclog.trim_inplace(self.path, max_bytes=1000, keep_bytes=200)
        self.assertTrue(rotated)
        self.assertEqual(os.stat(self.path).st_ino, inode)
        size = os.path.getsize(self.path)
        self.assertGreater(size, 0)
        self.assertLessEqual(size, 1000)
        with open(self.path, encoding="utf-8") as handle:
            first = handle.readline().strip()
        self.assertRegex(first, r"^line\d{4}$")

        os.write(fd, b"AFTER_ROTATE_MARKER\n")
        with open(self.path, encoding="utf-8") as handle:
            self.assertIn("AFTER_ROTATE_MARKER", handle.read())

    def test_trim_inplace_single_huge_line(self):
        with open(self.path, "wb") as handle:
            handle.write(b"z" * 5000)
        self.aclog.trim_inplace(self.path, max_bytes=1000, keep_bytes=200)
        self.assertGreater(os.path.getsize(self.path), 0)

    def test_trim_inplace_noop_below_limit(self):
        with open(self.path, "wb") as handle:
            handle.write(b"small\n")
        self.assertFalse(self.aclog.trim_inplace(self.path, max_bytes=1000, keep_bytes=200))
        self.assertEqual(os.path.getsize(self.path), 6)

    def test_tail_text(self):
        with open(self.path, "wb") as handle:
            for i in range(50):
                handle.write(("row%02d\n" % i).encode())
        text = self.aclog.tail_text(self.path, 3)
        self.assertEqual(text.splitlines(), ["row47", "row48", "row49"])
        self.assertEqual(self.aclog.tail_text("/nonexistent", 3), "")

    def test_append_writer_rotates(self):
        writer = self.aclog.AppendWriter(self.path, max_bytes=200, keep_bytes=100)
        for i in range(60):
            writer.write("entry-%03d" % i)
        self.assertLessEqual(os.path.getsize(self.path), 200)
        self.assertGreater(os.path.getsize(self.path), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
