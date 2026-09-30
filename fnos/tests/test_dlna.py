#!/usr/bin/env python3
"""acdlna 的纯逻辑单元测试（不联网）。

重点守住两条：
  * 只有「忙碌态 + 位置冻得足够久」才允许自动释放 —— 否则会把正在正常播放
    的会话打断（这是这个功能唯一会伤到用户的路径）；
  * 位置字符串解析要能吃下 DLNA 常见的 HH:MM:SS[.fff] 形式。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "server"))

import acdlna  # noqa: E402


class ParseRealtimeTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(acdlna._parse_reltime("00:00:00"), 0)
        self.assertEqual(acdlna._parse_reltime("01:23:45"), 5025)
        self.assertEqual(acdlna._parse_reltime("01:23:45.678"), 5025)
        self.assertEqual(acdlna._parse_reltime(" 00:00:07 "), 7)

    def test_garbage(self):
        for bad in ("", "NOT_IMPLEMENTED", "--:--:--", None, "abc"):
            with self.subTest(bad=bad):
                self.assertIsNone(acdlna._parse_reltime(bad))


class ShouldReleaseTests(unittest.TestCase):
    def test_disabled_by_default(self):
        # threshold 0 = 关闭：无论冻多久都不许动
        self.assertFalse(acdlna.should_release("PLAYING", 9999, 0))

    def test_only_busy_states(self):
        for state in ("STOPPED", "NO_MEDIA_PRESENT", "UNKNOWN", "UNREACHABLE"):
            with self.subTest(state=state):
                self.assertFalse(acdlna.should_release(state, 9999, 30))

    def test_requires_freeze(self):
        self.assertFalse(acdlna.should_release("PLAYING", 5, 30))
        self.assertTrue(acdlna.should_release("PLAYING", 31, 30))
        self.assertTrue(acdlna.should_release("PAUSED_PLAYBACK", 31, 30))

    def test_never_fires_below_hard_floor(self):
        """阈值被设得很小（比如 1 秒）时也必须受硬下限保护 —— 否则开播/切换
        期间的 TRANSITIONING 会被误判成"卡住"。"""
        self.assertFalse(acdlna.should_release("TRANSITIONING", 3, 1))
        self.assertTrue(acdlna.should_release("TRANSITIONING",
                                              acdlna.MIN_FREEZE_SECONDS + 1, 1))


class RendererStateTests(unittest.TestCase):
    def _renderer(self):
        return acdlna.Renderer("uuid:x", "http://127.0.0.1:1/AVTransport/control", "测试音箱", "S12")

    def test_frozen_seconds_only_while_busy(self):
        r = self._renderer()
        r.state = "PLAYING"
        r.last_change = time.time() - 20
        self.assertGreaterEqual(r.frozen_seconds, 19)
        r.state = "STOPPED"
        self.assertEqual(r.frozen_seconds, 0.0)

    def test_snapshot_shape(self):
        r = self._renderer()
        r.state = "PLAYING"
        r.reltime = 42
        snap = r.snapshot()
        for key in ("udn", "state", "reltime", "frozen_seconds", "last_error", "control_url"):
            self.assertIn(key, snap)
        self.assertEqual(snap["reltime"], 42)

    def test_stop_failure_is_reported_not_raised(self):
        r = self._renderer()          # 控制地址是个连不上的端口
        self.assertFalse(r.stop())
        self.assertIn("Stop 失败", r.last_error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
