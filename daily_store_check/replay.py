"""只使用已关店原快照补写飞书，不启动浏览器或推送消息。"""

from __future__ import annotations

from datetime import date
from typing import Any

from .config import StoreTask
from .feishu_client import FeishuClient, FeishuWriteUncertainError
from .reporting import today_record
from .storage_schema import TODAY_FIELD_ORDER_BY_PLATFORM
from .write_outbox import WriteOutbox


class SnapshotReplayer:
    """日常采集之外的显式历史补写，沿用每个原批次的独立目标进度。"""

    def __init__(self, client: FeishuClient, outbox: WriteOutbox):
        self.client = client
        self.outbox = outbox

    @staticmethod
    def validate(snapshot: dict[str, Any]) -> dict[str, Any]:
        platform = snapshot.get("平台")
        if platform not in TODAY_FIELD_ORDER_BY_PLATFORM:
            raise ValueError("补写快照平台无效")
        if snapshot.get("浏览器已关闭") is not True:
            raise ValueError("快照没有确认浏览器已关闭，禁止补写")
        if not snapshot.get("店铺名") or not isinstance(snapshot.get("原始结果"), list) or not snapshot["原始结果"]:
            raise ValueError("补写快照缺少店铺或原始结果")
        if any(str(row.get("指标") or "").endswith("_失败") for row in snapshot["原始结果"]):
            raise ValueError("采集失败快照不能补写")
        batch = str(snapshot.get("采集批次") or "")
        if len(batch) != 24 or any(char not in "0123456789abcdef" for char in batch):
            raise ValueError("补写采集批次格式无效")
        date.fromisoformat(snapshot["业务日期"])
        progress = snapshot.get("已完成写入")
        if not isinstance(progress, dict) or any(not isinstance(value, bool) for value in progress.values()):
            raise ValueError("补写进度格式无效")
        fields = today_record(platform, snapshot["店铺名"], snapshot["原始结果"])
        if not any(name in fields for name in TODAY_FIELD_ORDER_BY_PLATFORM[platform]):
            raise ValueError("快照没有有效今日业务指标，不补写空业务行")
        if any(fields[name] != snapshot[name] for name in ("采集批次", "业务日期", "采集时间")):
            raise ValueError("原始结果与快照采集身份不一致，禁止改日期或批次后补写")
        return fields

    def plan(self, *, business_dates: set[str] | None = None) -> list[dict[str, Any]]:
        snapshots = self.outbox.pending_batches(business_dates=business_dates)
        for snapshot in snapshots:
            self.validate(snapshot)
        return snapshots

    def replay(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        fields = self.validate(snapshot)
        latest = self.outbox.load_batch(fields["采集批次"])
        if latest is not None:
            self.validate(latest)
            if any(latest[name] != snapshot[name] for name in ("店铺名", "平台", "业务日期", "采集时间")):
                raise ValueError("原快照身份已变更，停止补写")
            snapshot = latest
            fields = self.validate(snapshot)
        if not self.client.configured:
            raise RuntimeError("飞书未配置，不能把模拟写入标记为真实补写成功")
        task = StoreTask(store_name=snapshot["店铺名"], platform=snapshot["平台"], recipient="")
        progress = dict(snapshot["已完成写入"])
        info = {**snapshot, "采集状态": "成功"}
        errors: list[str] = []
        written: list[str] = []
        skipped: list[str] = []

        def save() -> None:
            info["写入错误"] = "；".join(errors)
            self.outbox.save(task, snapshot["原始结果"], info, progress)

        def mark_append_in_flight() -> None:
            progress["sheet_uncertain"] = True
            save()

        # 先保存已验证的原批次；落盘失败时不允许派发任何写入请求。
        save()
        for destination in ("多维表", "电子表"):
            if progress.get(destination):
                skipped.append(destination)
                continue
            uncertain = bool(progress.get("sheet_uncertain"))
            try:
                if destination == "多维表":
                    self.client.write_today_bitable(task.platform, fields, batch_id=fields["采集批次"])
                else:
                    self.client.write_today_spreadsheet(
                        task.platform, fields, batch_id=fields["采集批次"],
                        verify_only=uncertain, on_append_attempt=mark_append_in_flight,
                    )
                progress[destination] = True
                if destination == "电子表":
                    progress.pop("sheet_uncertain", None)
                written.append(destination)
            except Exception as exc:
                if destination == "电子表":
                    if uncertain or isinstance(exc, FeishuWriteUncertainError) and exc.record_write_uncertain:
                        progress["sheet_uncertain"] = True
                    else:
                        progress.pop("sheet_uncertain", None)
                errors.append(f"{destination}写入失败: {exc}")
            finally:
                save()
        return {
            "店铺名": task.store_name, "平台": task.platform, "业务日期": fields["业务日期"],
            "采集批次": fields["采集批次"], "采集状态": "成功",
            "写入状态": "成功" if progress.get("多维表") and progress.get("电子表") else "待补写",
            "已完成写入": progress, "本次补写": written, "已完成跳过": skipped,
            "写入错误": "；".join(errors),
        }
