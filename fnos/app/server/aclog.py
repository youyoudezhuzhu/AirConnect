"""日志工具：原地轮转 + 尾部读取 + 供子进程输出使用的追加写入器。

轮转必须是**原地重写**（``cat tmp > file``），绝不能 ``mv``/``rename``：
写入方（子进程的 stdout/stderr、shell 的 ``>>``）以 ``O_APPEND`` 持有文件描述符，
一旦改名，它们会继续写进「已改名的旧 inode」，表面上轮转成功、实际在丢日志。
本模块所有写入器都用 ``ab`` 打开，因此原地重写之后仍然落在同一个文件上。
"""

from __future__ import annotations

import logging
import os
import threading

LOG_MAX_BYTES = 5 * 1024 * 1024      # 单文件 5 MiB
LOG_KEEP_BYTES = 1024 * 1024         # 超限后保留尾部 1 MiB
TRIM_INTERVAL_SECONDS = 60.0


def trim_inplace(path: str, max_bytes: int = LOG_MAX_BYTES,
                 keep_bytes: int = LOG_KEEP_BYTES) -> bool:
    """超过 ``max_bytes`` 时把文件原地截断为最后 ``keep_bytes`` 字节。

    返回 True 表示确实做了轮转。任何异常都被吞掉 —— 日志轮转失败绝不能
    影响业务。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size <= max_bytes:
        return False
    try:
        with open(path, "rb") as src:
            start = max(0, size - keep_bytes)
            src.seek(start)
            if start > 0:
                # 丢掉被截断的首行，避免日志里留下半行
                src.readline()
            payload = src.read()
        if not payload:
            # 极端情况：单行比 keep_bytes 还长 → 退化为保留原始尾部字节
            with open(path, "rb") as src:
                src.seek(max(0, size - keep_bytes))
                payload = src.read()
        if not payload:
            return False
        # 关键：写回**同一个 inode**（不能 rename，也不能换文件）
        with open(path, "r+b") as dst:
            dst.seek(0)
            dst.truncate(0)
            dst.write(payload)
            dst.flush()
            os.fsync(dst.fileno())
        return True
    except OSError:
        return False


def tail_text(path: str, lines: int = 300, max_scan: int = 1024 * 1024) -> str:
    """高效读取文件最后 ``lines`` 行（最多回扫 ``max_scan`` 字节）。"""
    lines = max(1, min(int(lines), 5000))
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size == 0:
        return ""
    start = max(0, size - max_scan)
    try:
        with open(path, "rb") as handle:
            handle.seek(start)
            data = handle.read()
    except OSError:
        return ""
    if start > 0:
        # 丢掉被截断的首行
        idx = data.find(b"\n")
        data = data[idx + 1:] if idx >= 0 else b""
    text = data.decode("utf-8", "replace")
    chunks = text.splitlines()
    if len(chunks) > lines:
        chunks = chunks[-lines:]
    return "\n".join(chunks)


def file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class AppendWriter:
    """以 ``O_APPEND`` 追加写日志，并在写入时按需原地轮转。

    单实例单文件：写入通过锁串行化，避免多线程交错出半行。
    """

    def __init__(self, path: str, max_bytes: int = LOG_MAX_BYTES,
                 keep_bytes: int = LOG_KEEP_BYTES) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.keep_bytes = keep_bytes
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, text: str) -> None:
        if not text:
            return
        payload = text if text.endswith("\n") else text + "\n"
        data = payload.encode("utf-8", "replace")
        with self._lock:
            try:
                # 以 ab 打开：每次写入前由内核定位到文件末尾，因此即使期间
                # 另一个描述符把文件原地截断，写入依然落在同一个 inode 上。
                with open(self.path, "ab") as handle:
                    handle.write(data)
            except OSError:
                return
            if file_size(self.path) > self.max_bytes:
                trim_inplace(self.path, self.max_bytes, self.keep_bytes)


class InplaceRotatingHandler(logging.Handler):
    """把 logging 记录写进文件，并原地轮转（不使用 rename）。"""

    def __init__(self, path: str, max_bytes: int = LOG_MAX_BYTES,
                 keep_bytes: int = LOG_KEEP_BYTES) -> None:
        super().__init__()
        self.writer = AppendWriter(path, max_bytes, keep_bytes)
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            self.writer.write(self.format(record))
        except Exception:  # noqa: BLE001 - 日志失败绝不影响业务
            pass


def setup_logging(path: str, level: int = logging.INFO) -> logging.Logger:
    """配置根 logger：只写文件（控制台输出仅在有 TTY 时启用）。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)
    root.addHandler(InplaceRotatingHandler(path))
    import sys
    if sys.stderr.isatty():
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
        root.addHandler(console)
    return logging.getLogger("airconnect")


class RotationThread(threading.Thread):
    """周期性对所有日志做原地轮转（覆盖不写日志的子系统）。"""

    def __init__(self, paths, interval: float = TRIM_INTERVAL_SECONDS) -> None:
        super().__init__(name="log-rotate", daemon=True)
        self.paths = list(paths)
        self.interval = interval
        # ⚠️ 绝不能叫 self._stop：threading.Thread 自己有一个 _stop() 方法，
        # join() 内部会调用它；用同名属性覆盖 → 退出时 join 抛
        # "TypeError: 'Event' object is not callable"，进程以非 0 退出。
        self._stop_event = threading.Event()

    def run(self) -> None:  # noqa: D102
        warned: set = set()
        while not self._stop_event.wait(self.interval):
            for path in self.paths:
                # 权限不足时 trim_inplace 会静默返回 False（它只捕获 OSError），
                # 表现成「日志一直涨、永远不轮转」。这里把它显式暴露出来 ——
                # 真机上踩过一次：日志文件被 root 创建，应用用户既写不进也轮转不了。
                if os.path.exists(path) and not os.access(path, os.W_OK):
                    if path not in warned:
                        warned.add(path)
                        logging.getLogger("airconnect.logrotate").warning(
                            "日志文件当前用户不可写，无法轮转（属主/权限不对）：%s", path)
                    continue
                trim_inplace(path)

    def stop(self) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=2.0)
