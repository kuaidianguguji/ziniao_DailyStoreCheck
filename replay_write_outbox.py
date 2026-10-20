"""显式补写原快照，默认只预览；不采集、不发送机器人消息、不调用AI。"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
from collections import Counter
from datetime import date, datetime, timezone

from daily_store_check.config import PROJECT_ROOT, load_config
from daily_store_check.feishu_client import FeishuClient
from daily_store_check.replay import SnapshotReplayer
from daily_store_check.run_lock import output_run_lock
from daily_store_check.write_outbox import WriteOutbox


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="config.yaml 路径")
    parser.add_argument("--dates", nargs="+", type=date.fromisoformat, help="原业务日期，如 2026-10-14 2026-10-15；省略则全部待补写")
    parser.add_argument("--apply", action="store_true", help="实际补写；省略时只列出计划")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    config = load_config(args.config)
    output = PROJECT_ROOT / str(config.get("data", {}).get("output_dir", "data") or "data")
    if args.apply:
        with output_run_lock(output):
            return execute(config, output, args)
    return execute(config, output, args)


def execute(config, output, args) -> int:
    outbox = WriteOutbox(output)
    client = FeishuClient(config)
    replayer = SnapshotReplayer(client, outbox)
    snapshots = replayer.plan(business_dates={value.isoformat() for value in args.dates} if args.dates else None)
    summary = {
        "模式": "实际补写" if args.apply else "预览", "待补写批次": len(snapshots),
        "业务日期": dict(Counter(item["业务日期"] for item in snapshots)),
        "目标": dict(Counter(destination for item in snapshots for destination in ("多维表", "电子表")
                            if not item["已完成写入"].get(destination))),
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if not args.apply or not snapshots:
        return 0
    if not client.configured:
        raise RuntimeError("飞书凭据未配置，停止补写")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = output / "replay_backups" / run_id
    backup.mkdir(parents=True)
    for snapshot in snapshots:
        path = outbox.directory / f"{snapshot['采集批次']}.json"
        shutil.copy2(path, backup / path.name)
    report_path = output / "replay_reports" / f"{run_id}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    results = []
    try:
        for snapshot in snapshots:
            try:
                result = replayer.replay(snapshot)
            except Exception as exc:
                result = {
                    "店铺名": snapshot["店铺名"], "平台": snapshot["平台"], "业务日期": snapshot["业务日期"],
                    "采集批次": snapshot["采集批次"], "写入状态": "待补写", "写入错误": str(exc),
                }
            results.append(result)
            temporary = report_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps({**summary, "备份": str(backup), "结果": results}, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(report_path)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        client.session.close()
    failed = sum(item["写入状态"] != "成功" for item in results)
    print(json.dumps({"成功": len(results) - failed, "失败": failed, "报告": str(report_path)}, ensure_ascii=False), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
