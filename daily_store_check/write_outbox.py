"""已关闭浏览器的采集快照与飞书补写状态，不保存凭据。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import StoreTask
from .reporting import BRAZIL_TIMEZONE
from datetime import datetime


class WriteOutbox:
    """原子保存每批次进度及电子表追加不确定标记，支持重启后仅核验。"""

    def __init__(self, output_dir: Path):
        self.directory = output_dir / "write_outbox"

    def save(self, task: StoreTask, rows: list[dict[str, Any]], info: dict[str, Any], progress: dict[str, bool]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        batch = str(info["采集批次"])
        if len(batch) != 24 or any(char not in "0123456789abcdef" for char in batch):
            raise ValueError("采集批次格式无效，不能保存补写状态")
        payload = {
            "店铺名": task.store_name, "平台": task.platform, "业务日期": info["业务日期"],
            "采集批次": batch, "采集时间": info["采集时间"], "数据": info["数据"],
            "指标元数据": info.get("指标元数据", {}), "原始结果": rows, "已完成写入": dict(progress),
            "浏览器已关闭": True, "状态": "完成" if progress.get("多维表") and progress.get("电子表") else "待补写",
        }
        path = self.directory / f"{batch}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        temporary.replace(path)

    def load(self, task: StoreTask) -> dict[str, Any] | None:
        """恢复今天已关闭浏览器的未完成批次，保留追加不确定状态。"""
        if not self.directory.exists():
            return None
        today = datetime.now(BRAZIL_TIMEZONE).date().isoformat()
        for path in sorted(self.directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                payload.get("店铺名") == task.store_name and payload.get("平台") == task.platform
                and payload.get("业务日期") == today and payload.get("状态") == "待补写"
                and payload.get("浏览器已关闭") is True and isinstance(payload.get("原始结果"), list)
            ):
                return {
                    **payload, "_采集结果": payload["原始结果"], "_已完成写入": payload.get("已完成写入", {}),
                }
        return None
