from __future__ import annotations

import logging
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from daily_store_check.config import StoreTask
from daily_store_check.orchestrator import DailyStoreCheck
from daily_store_check.ziniao_client import ZiniaoStoreCloseError, ZiniaoStoreSession


class StoreRetryRoundTests(unittest.TestCase):
    def setUp(self):
        # 本组主动模拟异常，只保留测试断言结果，不输出预期异常的完整堆栈。
        previous_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous_level)

    def make_checker(
        self, outcomes: dict[str, list[object]], *, close_failure: str = "", close_failure_attempt: int = 1,
    ):
        checker = object.__new__(DailyStoreCheck)
        checker.config = {}
        checker.ziniao = SimpleNamespace()
        checker.deepseek = SimpleNamespace(single_store_enabled=True)
        checker.store_concurrency = 3
        checker._stop_opening_stores = threading.Event()
        checker._save_store_capture = Mock()
        checker._format_mercado_notification = lambda name, rows, config: (name, "数据")
        lock = threading.Lock()
        events: list[tuple[str, str, int]] = []
        attempts: dict[str, int] = {}

        class FakeSession:
            def __init__(self, client, identifier, store_name):
                self.name = store_name
                self.download_path = ""
                self.opened = {"debuggingPort": 12345}

            def __enter__(self):
                with lock:
                    attempts[self.name] = attempts.get(self.name, 0) + 1
                    events.append(("打开", self.name, attempts[self.name]))
                return self

            def __exit__(self, *args):
                with lock:
                    events.append(("关闭", self.name, attempts[self.name]))
                if self.name == close_failure and attempts[self.name] == close_failure_attempt:
                    raise ZiniaoStoreCloseError("未确认关闭")
                return False

        def collect(name, *args):
            attempt = attempts[name]
            time.sleep(0.002)
            outcome = outcomes[name][attempt - 1]
            if isinstance(outcome, Exception):
                raise outcome
            if isinstance(outcome, list):
                return outcome
            return [{"采集时间": "2026-10-06T08:00:00+00:00", "飞书字段": {"今天总销售额": outcome}}]

        def record(kind, name):
            with lock:
                events.append((kind, name, attempts[name]))

        checker._load_crawler = lambda platform: SimpleNamespace(collect=collect)
        checker._write_feishu = lambda task, rows, **kwargs: record("写表", task.store_name)
        checker._safe_notify_markdown = lambda recipient, title, content: record("成功消息", title)
        checker._safe_notify = lambda recipient, title, content: record("失败消息", title.split(" ")[0])
        checker._send_store_deepseek_analysis = lambda recipient, info: record("分析", info["店铺名"])
        return checker, FakeSession, events, attempts

    @staticmethod
    def task(name: str) -> StoreTask:
        return StoreTask(store_name=name, platform="mercado", recipient="ou_test", browser_oauth=name)

    def test_first_failure_is_silent_and_does_not_write_tables(self):
        checker, session, events, _ = self.make_checker({"店铺A": [RuntimeError("广告页错误")]})
        with patch("daily_store_check.orchestrator.ZiniaoStoreSession", session):
            result = checker._run_store(self.task("店铺A"))
        self.assertEqual(result["状态"], "失败")
        self.assertEqual([event[0] for event in events], ["打开", "关闭"])

    def test_retries_only_failed_stores_serially_after_every_first_attempt_closes(self):
        checker, session, events, attempts = self.make_checker({
            "店铺A": [RuntimeError("首次错误"), 100],
            "店铺B": [RuntimeError("首次错误"), RuntimeError("补跑仍错误")],
            "店铺C": [300],
            "店铺D": [400],
        })
        tasks = [self.task(name) for name in ("店铺A", "店铺B", "店铺C", "店铺D")]
        info, timings = [], {}
        with patch("daily_store_check.orchestrator.ZiniaoStoreSession", session):
            with ThreadPoolExecutor(max_workers=3) as stores, ThreadPoolExecutor(max_workers=3) as ai:
                stopped = checker._run_concurrent_stores(tasks, stores, ai, info, timings)
        self.assertFalse(stopped)
        self.assertEqual(attempts, {"店铺A": 2, "店铺B": 2, "店铺C": 1, "店铺D": 1})
        self.assertEqual([item["店铺名"] for item in info], [task.store_name for task in tasks])
        self.assertEqual([item["状态"] for item in info], ["成功", "失败", "成功", "成功"])
        self.assertEqual(info[0]["数据"]["今天总销售额"], 100)
        self.assertTrue(all("_已完成写入" not in item for item in info))
        first_close_positions = [index for index, event in enumerate(events) if event[0] == "关闭" and event[2] == 1]
        retry_open_positions = [index for index, event in enumerate(events) if event[0] == "打开" and event[2] == 2]
        self.assertLess(max(first_close_positions), min(retry_open_positions))
        self.assertLess(events.index(("关闭", "店铺A", 2)), events.index(("打开", "店铺B", 2)))
        self.assertEqual([event for event in events if event[0] == "失败消息"], [("失败消息", "店铺B", 2)])
        for kind, name, attempt in events:
            if kind in {"写表", "成功消息"}:
                self.assertLess(events.index(("关闭", name, attempt)), events.index((kind, name, attempt)))
        self.assertEqual(sorted(name for kind, name, _ in events if kind == "分析"), ["店铺A", "店铺C", "店铺D"])
        self.assertEqual(len(timings), 4)
        self.assertIn("补跑耗时秒", timings["店铺A"])

    def test_explicit_failed_page_result_is_not_written(self):
        failed_rows = [{"指标": "Shopee商业分析概述_今天_失败", "显示值": "日期切换超时"}]
        checker, session, events, _ = self.make_checker({"店铺A": [failed_rows, failed_rows]})
        with patch("daily_store_check.orchestrator.ZiniaoStoreSession", session):
            first = checker._run_store(self.task("店铺A"))
            final = checker._run_store(self.task("店铺A"), final_attempt=True)
        self.assertEqual(first["状态"], "失败")
        self.assertEqual(final["状态"], "失败")
        self.assertNotIn("写表", [event[0] for event in events])
        self.assertEqual([event for event in events if event[0] == "失败消息"], [("失败消息", "店铺A", 2)])

    def test_close_failure_does_not_publish_and_prevents_unsafe_reopening(self):
        checker, session, events, attempts = self.make_checker({"店铺A": [100]}, close_failure="店铺A")
        info, timings = [], {}
        with patch("daily_store_check.orchestrator.ZiniaoStoreSession", session):
            with ThreadPoolExecutor(max_workers=1) as stores, ThreadPoolExecutor(max_workers=1) as ai:
                stopped = checker._run_concurrent_stores([self.task("店铺A")], stores, ai, info, timings)
        self.assertTrue(stopped)
        self.assertEqual(attempts["店铺A"], 1)
        self.assertNotIn("写表", [event[0] for event in events])
        self.assertEqual(sum(event[0] == "失败消息" for event in events), 1)

    def test_missing_browser_identifier_is_silent_until_final_attempt(self):
        checker = object.__new__(DailyStoreCheck)
        checker._find_browser_identifier = lambda *args: ""
        checker._safe_notify = Mock()
        task = StoreTask(store_name="店铺A", platform="mercado", recipient="ou_test")
        self.assertEqual(checker._run_store(task)["状态"], "失败")
        checker._safe_notify.assert_not_called()
        self.assertEqual(checker._run_store(task, final_attempt=True)["状态"], "失败")
        checker._safe_notify.assert_called_once()

    def test_retry_close_failure_stops_remaining_retries_and_notifies_each_failure(self):
        checker, session, events, attempts = self.make_checker(
            {"店铺A": [RuntimeError("首次错误"), 100], "店铺B": [RuntimeError("首次错误")]},
            close_failure="店铺A", close_failure_attempt=2,
        )
        info, timings = [], {}
        with patch("daily_store_check.orchestrator.ZiniaoStoreSession", session):
            with ThreadPoolExecutor(max_workers=3) as stores, ThreadPoolExecutor(max_workers=3) as ai:
                stopped = checker._run_concurrent_stores(
                    [self.task("店铺A"), self.task("店铺B")], stores, ai, info, timings,
                )
        self.assertTrue(stopped)
        self.assertEqual(attempts, {"店铺A": 2, "店铺B": 1})
        self.assertEqual([item["状态"] for item in info], ["失败", "失败"])
        self.assertEqual(sum(event[0] == "失败消息" for event in events), 2)
        self.assertNotIn("写表", [event[0] for event in events])

    def test_unexpected_first_thread_error_is_retried_and_only_final_error_notified(self):
        checker, _, _, _ = self.make_checker({})
        checker._run_store_timed = Mock(side_effect=[RuntimeError("首次线程异常"), RuntimeError("补跑线程异常")])
        checker._notify_store_failure = Mock()
        info, timings = [], {}
        with ThreadPoolExecutor(max_workers=1) as stores, ThreadPoolExecutor(max_workers=1) as ai:
            stopped = checker._run_concurrent_stores([self.task("店铺A")], stores, ai, info, timings)
        self.assertFalse(stopped)
        self.assertEqual(checker._run_store_timed.call_count, 2)
        checker._notify_store_failure.assert_called_once()
        self.assertEqual(info[0]["错误"], "补跑线程异常")

    def test_only_failed_table_destination_is_written_again(self):
        checker = object.__new__(DailyStoreCheck)
        checker.config = {}
        checker.feishu = SimpleNamespace(
            get_bitable_ref=lambda *args: ("test-token", "test-table"),
            batch_create_records=Mock(side_effect=[RuntimeError("多维表失败"), None]),
            append_spreadsheet_rows=Mock(),
        )
        checker._build_mercado_feishu_rows = lambda *args: ([{"店铺名": "店铺A"}], [["店铺A"]])
        progress = {}
        with self.assertRaisesRegex(RuntimeError, "多维表失败"):
            checker._write_feishu(self.task("店铺A"), [], completed_writes=progress)
        self.assertEqual(progress, {"电子表": True})
        checker._write_feishu(self.task("店铺A"), [], completed_writes=progress)
        self.assertEqual(checker.feishu.batch_create_records.call_count, 2)
        checker.feishu.append_spreadsheet_rows.assert_called_once()
        self.assertEqual(progress, {"电子表": True, "多维表": True})

    def test_initialization_and_close_failure_propagates_close_guard(self):
        client = SimpleNamespace(
            open_store=lambda identifier: {"debuggingPort": 12345},
            get_driver=Mock(side_effect=RuntimeError("接管失败")),
            close_store=Mock(side_effect=RuntimeError("关闭失败")),
        )
        with self.assertRaises(ZiniaoStoreCloseError):
            ZiniaoStoreSession(client, "test-browser", "店铺A").__enter__()

    def test_final_summary_waits_for_retry_and_uses_only_final_result(self):
        checker, session, events, attempts = self.make_checker({"店铺A": [RuntimeError("首次错误"), 100]})
        checker.feishu = SimpleNamespace(list_control_tasks=lambda: [self.task("店铺A")])
        checker._prepare_ziniao = Mock()
        checker._cleanup_retention = Mock()
        checker._print_store_processing_times = Mock()
        checker.ziniao.exit_client = Mock()

        def summarize(info):
            self.assertEqual(attempts, {"店铺A": 2})
            self.assertEqual(len(info), 1)
            self.assertEqual(info[0]["状态"], "成功")
            self.assertNotIn("错误", info[0])
            self.assertIn(("分析", "店铺A", 2), events)

        checker._send_all_info_summary = Mock(side_effect=summarize)
        with patch("daily_store_check.orchestrator.ZiniaoStoreSession", session):
            checker.run_once()
        checker._send_all_info_summary.assert_called_once()
        checker.ziniao.exit_client.assert_called_once()


if __name__ == "__main__":
    unittest.main()
