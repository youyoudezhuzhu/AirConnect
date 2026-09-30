"""airupnp / aircast 进程监管。

本模块负责：

* 按当前设置拉起 ``airupnp``（DLNA/UPnP/Sonar/Heos）与 ``aircast``（Chromecast）；
* 把子进程的 stdout/stderr 收进带原地轮转的日志文件（AirConnect 只往 stderr
  打印，且每行 ``fflush``，所以管道不会带来缓冲延迟）；
* 崩溃自动重启（带退避与崩溃风暴保护）；
* 从日志里提取「发现了哪些设备」，回填设备的原始友好名。

进程模型：AirConnect 本体不需要特权端口，但飞牛的生命周期脚本以 root 运行，
真正的桥接进程由本服务（也是 root 启动）直接 fork —— 与 Air2DLNA 的
shairport-sync 不同，airupnp/aircast 只处理 RTSP/HTTP/UPnP，不含需要降权解析
的媒体解码，因此保持与父进程同用户即可，避免 socket/端口权限的额外复杂度。
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import acpath
from aclog import AppendWriter, tail_text

log = logging.getLogger("airconnect.supervisor")

# 上游日志形如：
#   [23:00:06.150] AddMRDevice:1038 [0x...]: adding renderer (小爱音箱-2284) with mac BBBB72C30EDB
RENDERER_RE = re.compile(r"adding renderer \((?P<name>.*)\) with mac (?P<mac>[0-9A-Fa-f]+)")
BYEBYE_RE = re.compile(r"renderer <(?P<name>.*?)> bye-bye")
REMOVING_RE = re.compile(r"removing unresponsive player \((?P<name>.*)\)")

RESTART_BACKOFF_MIN = 2.0
RESTART_BACKOFF_MAX = 30.0
CRASH_WINDOW = 600.0
CRASH_LIMIT = 8
STOP_GRACE_SECONDS = 8.0


def _mac_candidates(mac: str) -> set:
    """把 ``bb:bb:db:0e:c3:72`` 转成 AirConnect 日志里 ``%hX%X`` 的等价写法。

    上游打印的是 ``*(uint16_t*)mac`` 与 ``*(uint32_t*)(mac+2)``，即前两字节按
    本机序解释成 16 位、后四字节解释成 32 位，且不带前导零。这里按同样的规则
    生成候选串，避免用「字符串包含」这种会误判的匹配。
    """
    parts = re.findall(r"[0-9a-fA-F]{2}", mac or "")
    if len(parts) != 6:
        return set()
    raw = bytes(int(p, 16) for p in parts)
    first = int.from_bytes(raw[0:2], "little")
    second = int.from_bytes(raw[2:6], "little")
    plain = raw.hex()
    return {
        f"{first:X}{second:X}".upper(),
        f"{first:x}{second:x}".lower(),
        plain.upper(),
        ":".join(parts).upper(),
    }


class BridgeProcess:
    """单个 AirConnect 进程（airupnp 或 aircast）。"""

    def __init__(self, kind: str, settings_provider: Callable[[], Dict[str, Any]]) -> None:
        self.kind = kind                       # "upnp" | "cast"
        self.binary_name = "airupnp" if kind == "upnp" else "aircast"
        self.settings_provider = settings_provider
        self.log_path = os.path.join(acpath.PKG_VAR, f"{self.binary_name}.log")
        self.pid_path = os.path.join(acpath.PKG_VAR, f"{self.binary_name}.pid")
        self.writer = AppendWriter(self.log_path)
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._started_at: float = 0.0
        self._restarts = 0
        self._last_exit: Optional[Dict[str, Any]] = None
        self._crash_times: List[float] = []
        self._crash_loop = False
        self._want_running = False
        self.seen_devices: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------ 路径
    @property
    def binary(self) -> str:
        return acpath.binary_path(self.binary_name)

    @property
    def xml_path(self) -> str:
        return acpath.AIRUPNP_XML if self.kind == "upnp" else acpath.AIRCAST_XML

    # ------------------------------------------------------------ 参数
    def build_argv(self, settings: Dict[str, Any]) -> List[str]:
        """命令行参数。

        只有 ``-N``（名称格式）没有对应的 XML 键，必须走命令行；其余全部写进
        XML。AirConnect 先 ``LoadConfig`` 再 ``ParseArgs``，因此命令行优先级更高。
        """
        argv = [
            self.binary,
            "-Z",                       # 非交互：不做 TTY 提示（否则 CPU 空转）
            "-I",                       # 发现新设备时自动保存配置（仅变更时才写盘）
            "-x", self.xml_path,
            "-p", self.pid_path,
            "-N", "%s" + str(settings.get("name_suffix", "+")),
        ]
        return argv

    # ------------------------------------------------------------ 生命周期
    def alive(self) -> bool:
        with self._lock:
            proc = self._proc
        return proc is not None and proc.poll() is None

    @property
    def pid(self) -> Optional[int]:
        with self._lock:
            proc = self._proc
        return proc.pid if proc is not None and proc.poll() is None else None

    @property
    def uptime(self) -> float:
        return max(0.0, time.time() - self._started_at) if self.alive() else 0.0

    def start(self, settings: Dict[str, Any]) -> bool:
        with self._lock:
            if self.alive():
                return True
            if not os.access(self.binary, os.X_OK):
                self._append(f"[supervisor] 二进制不存在或不可执行：{self.binary}")
                return False
            os.makedirs(acpath.PKG_VAR, exist_ok=True)
            os.makedirs(acpath.PKG_HOME, exist_ok=True)
            argv = self.build_argv(settings)
            env = dict(os.environ)
            # 规则 #16：HOME 必须**无条件**覆盖，飞牛已注入 HOME=/root，
            # 用 ${HOME:-...} 这种写法不会生效。
            env["HOME"] = acpath.PKG_HOME
            env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
            self._append(f"[supervisor] 启动：{' '.join(argv)}")
            try:
                self._proc = subprocess.Popen(          # noqa: S603 - 参数为常量路径
                    argv,
                    cwd=acpath.PKG_HOME,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as exc:
                self._append(f"[supervisor] 启动失败：{exc}")
                self._proc = None
                return False
            self._started_at = time.time()
            self._want_running = True
            self._reader = threading.Thread(target=self._pump, name=f"{self.binary_name}-log",
                                            daemon=True)
            self._reader.start()
            return True

    def _pump(self) -> None:
        """把子进程输出按行写进日志，并解析设备发现事件。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        pending = b""
        while True:
            try:
                chunk = stream.read(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                self._handle_line(line.decode("utf-8", "replace"))
        if pending:
            self._handle_line(pending.decode("utf-8", "replace"))

    def _handle_line(self, line: str) -> None:
        self.writer.write(line)
        match = RENDERER_RE.search(line)
        if match:
            name = match.group("name").strip()
            mac = match.group("mac").strip().upper()
            self.seen_devices[mac] = {"friendly_name": name, "mac": mac,
                                      "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            return
        for regex in (BYEBYE_RE, REMOVING_RE):
            match = regex.search(line)
            if match:
                name = match.group("name").strip()
                for entry in list(self.seen_devices.values()):
                    if entry.get("friendly_name") == name:
                        entry["offline"] = True
                return

    def _append(self, text: str) -> None:
        self.writer.write(text)

    def stop(self, grace: float = STOP_GRACE_SECONDS) -> None:
        with self._lock:
            self._want_running = False
            proc = self._proc
        if proc is None or proc.poll() is not None:
            self._reap()
            return
        pid = proc.pid
        self._append(f"[supervisor] 停止 {self.binary_name} (pid={pid})")
        try:
            # AirConnect 的 SIGTERM 处理是「优雅停播 + 退出」，先给它机会；
            # 注意不要用 -k，那样会跳过 AVTStop。
            proc.send_signal(signal.SIGTERM)
        except OSError:
            pass
        deadline = time.time() + grace
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is None:
            self._append(f"[supervisor] {self.binary_name} 未在 {grace:.0f}s 内退出，强制结束")
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        self._reap()

    def _reap(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None:
            code = proc.poll()
            self._last_exit = {"code": code, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
        try:
            if os.path.exists(self.pid_path):
                os.unlink(self.pid_path)
        except OSError:
            pass

    def collect_exit(self) -> Optional[Dict[str, Any]]:
        """若进程已退出则回收并返回退出信息（供监管线程判断是否需要重启）。"""
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is None:
            return None
        info = {"code": proc.returncode, "want_running": self._want_running}
        self._reap()
        return info

    # ------------------------------------------------------------ 崩溃保护
    def note_crash(self) -> float:
        """记录一次异常退出，返回下次重启应等待的秒数（或 -1 表示放弃）。"""
        now = time.time()
        self._crash_times = [t for t in self._crash_times if now - t < CRASH_WINDOW]
        self._crash_times.append(now)
        self._restarts += 1
        if len(self._crash_times) >= CRASH_LIMIT:
            self._crash_loop = True
            return -1.0
        exponent = min(len(self._crash_times) - 1, 5)
        return min(RESTART_BACKOFF_MAX, RESTART_BACKOFF_MIN * (2 ** exponent))

    def clear_crash_loop(self) -> None:
        self._crash_loop = False
        self._crash_times.clear()

    # ------------------------------------------------------------ 查询
    def snapshot(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "binary": self.binary_name,
            "running": self.alive(),
            "pid": self.pid,
            "uptime": round(self.uptime, 1),
            "restarts": self._restarts,
            "crash_loop": self._crash_loop,
            "last_exit": self._last_exit,
            "log": self.log_path,
            "devices_seen": list(self.seen_devices.values()),
        }

    def tail(self, lines: int = 300) -> str:
        return tail_text(self.log_path, lines)

    def enrich(self, devices: List[Dict[str, Any]], name_suffix: str) -> bool:
        """用日志里发现的原始设备名回填 ``friendly_name``。"""
        if not self.seen_devices:
            return False
        by_mac: Dict[str, str] = {}
        for entry in self.seen_devices.values():
            for candidate in _mac_candidates(entry.get("mac", "")):
                by_mac[candidate] = entry["friendly_name"]
        changed = False
        for device in devices:
            if device.get("friendly_name"):
                continue
            guess = ""
            mac = (device.get("mac") or "").upper()
            for candidate in _mac_candidates(device.get("mac", "")):
                if candidate in by_mac:
                    guess = by_mac[candidate]
                    break
            if not guess:
                # 自动命名的设备：配置里的名字就是 友好名 + 后缀
                expected = device.get("name", "")
                for entry in self.seen_devices.values():
                    if entry.get("friendly_name", "") + name_suffix == expected:
                        guess = entry["friendly_name"]
                        break
            if guess:
                device["friendly_name"] = guess
                device["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
                changed = True
        return changed


class Supervisor:
    """按模式同时监管 airupnp / aircast。"""

    def __init__(self, settings_provider: Callable[[], Dict[str, Any]]) -> None:
        self.settings_provider = settings_provider
        self.upnp = BridgeProcess("upnp", settings_provider)
        self.cast = BridgeProcess("cast", settings_provider)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._pending: Dict[str, float] = {}

    # ------------------------------------------------------------ 组合
    def processes_for(self, mode: str) -> List[BridgeProcess]:
        if mode == "cast":
            return [self.cast]
        if mode == "both":
            return [self.upnp, self.cast]
        return [self.upnp]

    def all_processes(self) -> List[BridgeProcess]:
        return [self.upnp, self.cast]

    # ------------------------------------------------------------ 动作
    def start_mode(self, settings: Dict[str, Any]) -> Dict[str, bool]:
        result = {}
        wanted = self.processes_for(settings.get("mode", "upnp"))
        for proc in self.all_processes():
            if proc in wanted:
                proc.clear_crash_loop()
                result[proc.binary_name] = proc.start(settings)
            else:
                proc.stop()
                result.setdefault(proc.binary_name, False)
        return result

    def stop_all(self) -> None:
        for proc in self.all_processes():
            proc.stop()

    def restart(self, settings: Dict[str, Any]) -> Dict[str, bool]:
        self.stop_all()
        self._pending.clear()
        return self.start_mode(settings)

    # ------------------------------------------------------------ 监管线程
    def start_monitor(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="supervisor", daemon=True)
        self._thread.start()

    def stop_monitor(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _loop(self) -> None:
        while not self._stop.wait(3.0):
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - 监管线程绝不能死
                log.exception("监管循环异常")

    def _tick(self) -> None:
        settings = self.settings_provider()
        mode = settings.get("mode", "upnp")
        wanted = self.processes_for(mode)
        now = time.time()
        for proc in self.all_processes():
            if proc not in wanted:
                continue
            info = proc.collect_exit()
            if info is not None and info.get("want_running"):
                if proc._crash_loop:      # noqa: SLF001 - 同一模块内的内部状态
                    continue
                delay = proc.note_crash()
                if delay < 0:
                    proc._append("[supervisor] 短时间内反复退出，已停止自动重启；"
                                 "请检查日志与设置后手动启动")  # noqa: SLF001
                    continue
                self._pending[proc.binary_name] = now + delay
                continue
            with self._lock:
                due = self._pending.get(proc.binary_name)
            if due is not None and now >= due:
                self._pending.pop(proc.binary_name, None)
                proc.start(settings)

    # ------------------------------------------------------------ 查询
    def snapshot(self) -> Dict[str, Any]:
        settings = self.settings_provider()
        mode = settings.get("mode", "upnp")
        entries = [p.snapshot() for p in self.all_processes()]
        active = [e for e in entries if e["kind"] == mode or mode == "both"]
        running = any(e["running"] for e in active)
        return {
            "mode": mode,
            "running": running,
            "processes": entries,
        }

    def enrich_devices(self, settings: Dict[str, Any]) -> bool:
        devices = settings.get("devices", [])
        suffix = str(settings.get("name_suffix", "+"))
        changed = False
        for proc in self.processes_for(settings.get("mode", "upnp")):
            changed = proc.enrich(devices, suffix) or changed
        return changed

    def logs(self, source: str, lines: int = 300) -> str:
        if source == "cast":
            return self.cast.tail(lines)
        return self.upnp.tail(lines)
