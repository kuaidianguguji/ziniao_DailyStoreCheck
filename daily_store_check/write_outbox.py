"""已关闭浏览器的采集快照与飞书补写状态，不保存凭据。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .config import StoreTask
from .reporting import BRAZIL_TIMEZONE
from datetime import datetime

_OUTBOX_LOCK = threading.RLock()


class WriteOutbox:
    """原子保存每批次进度及电子表追加不确定标记，支持重启后仅核验。"""

    def __init__(self, output_dir: Path):
        self.directory = output_dir / "write_outbox"

    def save(self, task: StoreTask, rows: list[dict[str, Any]], info: dict[str, Any], progress: dict[str, bool]) -> None:
        """保存写入进度，合并独立消息进度，避免目的地回写覆盖已经发送的标记。"""
        with _OUTBOX_LOCK:
            self._save(task, rows, info, progress)

    def _save(self, task: StoreTask, rows: list[dict[str, Any]], info: dict[str, Any], progress: dict[str, bool]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        batch = str(info["采集批次"])
        if len(batch) != 24 or any(char not in "0123456789abcdef" for char in batch):
            raise ValueError("采集批次格式无效，不能保存补写状态")
        path = self.directory / f"{batch}.json"
        existing: dict[str, Any] = {}
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict) or any(
                existing.get(name) != value for name, value in (
                    ("采集批次", batch), ("店铺名", task.store_name), ("平台", task.platform),
                    ("业务日期", info["业务日期"]),
                )
            ):
                raise ValueError("已有补写快照身份不一致，禁止覆盖")
        messages = dict(existing.get("消息进度") or {})
        messages.update(info.get("_消息进度") or {})
        messages.update(info.get("消息进度") or {})
        progress = dict(progress)
        # 已确认的目标只会由未完成变为完成；旧调用者不能把已完成目标退回待写。
        for destination in ("多维表", "电子表"):
            if existing.get("已完成写入", {}).get(destination):
                progress[destination] = True
        if progress.get("电子表"):
            progress.pop("sheet_uncertain", None)
        complete = bool(progress.get("多维表") and progress.get("电子表"))
        payload = {
            "店铺名": task.store_name, "平台": task.platform, "业务日期": info["业务日期"],
            "采集批次": batch, "采集时间": info["采集时间"], "数据": info["数据"],
            "指标元数据": info.get("指标元数据", {}), "原始结果": rows, "已完成写入": dict(progress),
            "浏览器已关闭": True, "状态": "完成" if complete else "待补写",
            "采集状态": info.get("采集状态", existing.get("采集状态", "成功")),
            "写入状态": "成功" if complete else "待补写",
            "写入错误": "" if complete else info.get("写入错误", existing.get("写入错误", "")),
            "消息进度": messages,
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _restore(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            **payload, "_采集结果": payload["原始结果"], "_已完成写入": dict(payload.get("已完成写入", {})),
            "_消息进度": dict(payload.get("消息进度") or {}),
        }

    def load_batch(self, batch: str) -> dict[str, Any] | None:
        """在持有运行锁时重新读取指定原批次，包括已经完成的批次。"""
        if len(batch) != 24 or any(char not in "0123456789abcdef" for char in batch):
            raise ValueError("采集批次格式无效")
        path = self.directory / f"{batch}.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("采集批次") != batch:
            raise ValueError("补写快照采集身份不一致")
        return self._restore(payload)

    def pending_batches(self, *, business_dates: set[str] | None = None) -> list[dict[str, Any]]:
        """显式历史补写入口：按原业务日期列出全部待补写批次，不改变日常只恢复当天的规则。"""
        if not self.directory.exists():
            return []
        batches: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"补写快照不是对象: {path.name}")
            if payload.get("状态") != "待补写":
                continue
            if business_dates is not None and payload.get("业务日期") not in business_dates:
                continue
            if (
                payload.get("浏览器已关闭") is not True
                or not isinstance(payload.get("原始结果"), list)
                or not payload["原始结果"]
                or payload.get("采集批次") != path.stem
                or not isinstance(payload.get("已完成写入"), dict)
            ):
                raise ValueError(f"待补写快照不完整，不能安全恢复: {path.name}")
            batches.append(self._restore(payload))
        return sorted(batches, key=lambda item: (item["业务日期"], str(item.get("采集时间", "")), item["采集批次"]))

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
                return self._restore(payload)
        return None
