"""同一输出目录的日常任务与历史补写进程互斥，进程退出后系统自动释放。"""

from __future__ import annotations

import os
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from .config import PROJECT_ROOT


@contextmanager
def output_run_lock(output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / ".daily_store_check.lock"
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("同一数据目录已有采集或补写任务执行中，请等待完成后再运行") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def exclusive_daily_run(method):
    @wraps(method)
    def run(checker, *args, **kwargs):
        output_dir = PROJECT_ROOT / str(checker.config.get("data", {}).get("output_dir", "data") or "data")
        with output_run_lock(output_dir):
            return method(checker, *args, **kwargs)
    return run
