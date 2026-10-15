"""每日店铺任务编排器。"""

from __future__ import annotations

import importlib
import json
import logging
import re
import threading
import time
import unicodedata
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, StoreTask, is_period_enabled, normalise_platform
from .deepseek_client import DeepSeekClient
from .feishu_client import FeishuClient, FeishuWriteUncertainError
from .reporting import business_date, capture_batch, display_metric, extract_today, today_record
from .storage_schema import TODAY_FIELD_ORDER_BY_PLATFORM, today_field_names
from .write_outbox import WriteOutbox
from .ziniao_client import ZiniaoClient, ZiniaoStoreCloseError, ZiniaoStoreSession

LOGGER = logging.getLogger(__name__)


# 字段结构由唯一的今日 schema 维护，禁止继续沿用昨日、7天、30天的列顺序。
TIKTOK_TABLE_FIELD_ORDER = TIKTOK_SPREADSHEET_FIELD_ORDER = today_field_names("tiktok")
SHOPEE_TABLE_FIELD_ORDER = SHOPEE_SPREADSHEET_FIELD_ORDER = today_field_names("shopee")
MERCADO_TABLE_FIELD_ORDER = MERCADO_SPREADSHEET_FIELD_ORDER = today_field_names("mercado")
TIKTOK_FORMULA_FIELDS: frozenset[str] = frozenset()

TIKTOK_MESSAGE_GROUPS = (
    ("今天广告数据", (
        ("成本", "今天成本"), ("SKU订单数", "今天广告SKU订单数"), ("均单价", "今天均单价"),
        ("总收入", "今天总收入"), ("ROI", "今天ROI"),
    )),
    ("今天概览数据", (
        ("GMV", "今天GMV"), ("成交件数", "今天成交件数"), ("SKU订单数", "今天概览SKU订单数"),
        ("订单数", "今天订单数"), ("客户数", "今天客户数"), ("商品访客数", "今天商品访客数"),
        ("曝光数", "今天曝光数"), ("去重曝光数", "今天去重曝光数"), ("直播GMV", "今天直播GMV"),
        ("短视频GMV", "今天短视频GMV"), ("商品卡GMV", "今天商品卡GMV"),
        ("直播GMV占比", "今天GMV直播比"), ("短视频GMV占比", "今天GMV视频比"), ("商品卡GMV占比", "今天GMV商品卡比"),
    )),
)
SHOPEE_MESSAGE_GROUPS = (
    ("今天广告数据", tuple(
        (name.removeprefix("今天广告"), name)
        for name in TODAY_FIELD_ORDER_BY_PLATFORM["shopee"] if name.startswith("今天广告")
    )),
)
SHOPEE_ANALYTICS_PAGE_NAMES = ("商业分析概述",)
MERCADO_MESSAGE_GROUPS = (
    ("今天经营数据", tuple((name.removeprefix("今天"), name)
        for name in TODAY_FIELD_ORDER_BY_PLATFORM["mercado"] if not name.startswith("今天广告"))),
    ("今天广告数据", tuple((name.removeprefix("今天广告"), name)
        for name in TODAY_FIELD_ORDER_BY_PLATFORM["mercado"] if name.startswith("今天广告"))),
)
SHOPEE_PERCENT_TEXT_FIELDS = frozenset({"今天广告点击率", "今天广告加购率"})
SHOPEE_ROAS_TEXT_FIELDS = frozenset({"今天广告支出回报率"})
SHOPEE_CURRENCY_FIELDS = frozenset(name for name in TODAY_FIELD_ORDER_BY_PLATFORM["shopee"]
    if any(token in name for token in ("销售额", "花费", "优惠价金额")))
SHOPEE_DOUBLE_FIELDS = frozenset(TODAY_FIELD_ORDER_BY_PLATFORM["shopee"])
SHOPEE_INTEGER_FIELDS = SHOPEE_DOUBLE_FIELDS - SHOPEE_CURRENCY_FIELDS - SHOPEE_PERCENT_TEXT_FIELDS - SHOPEE_ROAS_TEXT_FIELDS
MERCADO_PROGRESS_FIELDS = frozenset(name for name in TODAY_FIELD_ORDER_BY_PLATFORM["mercado"] if "转换率" in name)
MERCADO_CURRENCY_FIELDS = frozenset(name for name in TODAY_FIELD_ORDER_BY_PLATFORM["mercado"]
    if any(token in name for token in ("销售额", "单价", "价值", "成本")))
MERCADO_INTEGER_FIELDS = frozenset(TODAY_FIELD_ORDER_BY_PLATFORM["mercado"]) - MERCADO_PROGRESS_FIELDS - MERCADO_CURRENCY_FIELDS


class DailyStoreCheck:
    """控制表 -> 紫鸟单店铺 -> 平台爬虫 -> 飞书写入/推送的主流程。"""

    def __init__(self, config: dict[str, Any]):
        """创建飞书和紫鸟客户端，并准备当前轮次的店铺缓存。"""
        self.config = config
        self.feishu = FeishuClient(config)
        self.deepseek = DeepSeekClient(config)
        self.ziniao = ZiniaoClient(config)
        self.browser_list: list[dict[str, Any]] = []
        self.store_concurrency = max(1, int(config.get("data", {}).get("store_concurrency", 3) or 3))
        self._stop_opening_stores = threading.Event()

    def run_once(self) -> None:
        """执行一轮任务；店铺采集和单店 DeepSeek 分别使用独立并发队列。"""
        # ALL_info 保存本轮所有平台、所有店铺的采集结果和失败状态。
        # 它只在全部店铺结束后用于 summary_recipients 汇总推送，不影响每店铺即时推送。
        ALL_info: list[dict[str, Any]] = []
        # 仅在内存中保存本轮各店铺的完整处理耗时，最后通过 print 输出到终端，不写入运行日志或飞书。
        store_processing_times: dict[str, dict[str, Any]] = {}
        tasks = self.feishu.list_control_tasks()
        if not tasks:
            LOGGER.info("没有可执行店铺，可能是控制表为空、开关暂停或飞书尚未配置")
            self._print_store_processing_times(store_processing_times)
            return

        try:
            self._prepare_ziniao()
            self._stop_opening_stores.clear()
            LOGGER.info(
                "[并发][启动] 店铺并发数=%s，DeepSeek单店分析并发数=%s，任务数=%s",
                self.store_concurrency,
                self.store_concurrency,
                len(tasks),
            )
            with ThreadPoolExecutor(
                max_workers=self.store_concurrency,
                thread_name_prefix="store-collector",
            ) as store_executor, ThreadPoolExecutor(
                max_workers=self.store_concurrency,
                thread_name_prefix="deepseek-store",
            ) as deepseek_executor:
                close_failed = self._run_concurrent_stores(
                    tasks,
                    store_executor,
                    deepseek_executor,
                    ALL_info,
                    store_processing_times,
                )
                # 店铺队列结束后可以清理历史数据；已提交的 DeepSeek 仍在独立线程中继续执行。
                if close_failed:
                    LOGGER.warning("本轮因店铺未确认关闭而停止启动新店铺，跳过90天数据清理")
                else:
                    self._cleanup_retention()
                LOGGER.info("[并发][DeepSeek等待] 店铺采集已结束，等待剩余单店分析完成")
        finally:
            # 即使旧数据清理或某个店铺异常，也尽量发送已经收集到的最终汇总；
            # 无论汇总推送是否成功，最后都必须退出紫鸟客户端。
            try:
                self._send_all_info_summary(ALL_info)
            finally:
                try:
                    self.ziniao.exit_client()
                finally:
                    # 用户要求在所有业务、汇总推送和紫鸟退出之后，最末尾只向终端打印计时字典。
                    self._print_store_processing_times(store_processing_times)

    def _run_concurrent_stores(
        self,
        tasks: list[StoreTask],
        store_executor: ThreadPoolExecutor,
        deepseek_executor: ThreadPoolExecutor,
        all_info: list[dict[str, Any]],
        store_processing_times: dict[str, dict[str, Any]],
    ) -> bool:
        """维持固定数量的店铺任务，并在每店关闭后立即异步提交 DeepSeek。"""
        pending: dict[Future[tuple[dict[str, Any], float]], tuple[int, StoreTask]] = {}
        results_by_index: dict[int, dict[str, Any]] = {}
        timing_keys_by_index: dict[int, str] = {}
        next_task_index = 0
        close_failed = False
        single_store_enabled = bool(getattr(getattr(self, "deepseek", None), "single_store_enabled", True))

        def submit_available_tasks() -> None:
            nonlocal next_task_index
            while (
                not self._stop_opening_stores.is_set()
                and next_task_index < len(tasks)
                and len(pending) < self.store_concurrency
            ):
                task_index = next_task_index
                task = tasks[task_index]
                next_task_index += 1
                future = store_executor.submit(self._run_store_timed, task)
                pending[future] = (task_index, task)
                LOGGER.info(
                    "[并发][店铺提交] 序号=%s/%s，店铺=%s，平台=%s，运行中=%s/%s",
                    task_index + 1,
                    len(tasks),
                    task.store_name,
                    task.platform,
                    len(pending),
                    self.store_concurrency,
                )

        submit_available_tasks()
        while pending:
            completed, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
            for future in completed:
                task_index, task = pending.pop(future)
                try:
                    store_info, elapsed_seconds = future.result()
                except Exception as exc:
                    LOGGER.exception("[并发][店铺线程异常] 店铺=%s，平台=%s", task.store_name, task.platform)
                    store_info = {
                        "店铺名": task.store_name,
                        "平台": task.platform,
                        "状态": "失败",
                        "采集时间": "",
                        "数据": {},
                        "错误": str(exc),
                    }
                    elapsed_seconds = 0.0

                results_by_index[task_index] = store_info
                timing_key = self._build_store_timing_key(store_processing_times, task.store_name)
                timing_keys_by_index[task_index] = timing_key
                store_processing_times[timing_key] = {
                    "平台": task.platform,
                    "耗时秒": round(elapsed_seconds, 2),
                    "耗时": self._format_elapsed_time(elapsed_seconds),
                }
                LOGGER.info(
                    "[并发][店铺完成] 序号=%s/%s，店铺=%s，状态=%s，耗时=%s，剩余运行=%s",
                    task_index + 1,
                    len(tasks),
                    task.store_name,
                    store_info.get("状态"),
                    self._format_elapsed_time(elapsed_seconds),
                    len(pending),
                )

                if store_info.get("中止后续店铺"):
                    close_failed = True
                    self._stop_opening_stores.set()
                    LOGGER.critical(
                        "店铺 %s 未确认关闭，停止提交尚未启动的店铺；当前已运行店铺继续完成并关闭",
                        task.store_name,
                    )
                elif store_info.get("状态") == "成功":
                    self._submit_store_analysis(task, store_info, deepseek_executor, single_store_enabled)

            submit_available_tasks()

        # 首轮的全部采集线程退出后，失败店铺按控制表顺序单个补跑一次，不重新进入并发队列。
        failed_indices = [index for index in sorted(results_by_index) if results_by_index[index].get("状态") == "失败"]
        LOGGER.info("[补跑][首轮结束] 已执行=%s，失败=%s，补跑并发数=1，补跑次数=1", len(results_by_index), len(failed_indices))
        for retry_number, task_index in enumerate(failed_indices, start=1):
            task = tasks[task_index]
            first_result = results_by_index[task_index]
            if self._stop_opening_stores.is_set():
                # 未确认关闭的浏览器不能再开店；在首轮结束后统一发送最终失败说明。
                first_result["错误"] = f"{first_result.get('错误', '')}\n有店铺未确认关闭，已停止补跑。"
                self._notify_store_failure(task, first_result)
                continue
            LOGGER.info("[补跑][开始] 序号=%s/%s，店铺=%s，平台=%s，首轮错误=%s", retry_number, len(failed_indices), task.store_name, task.platform, first_result.get("错误", ""))
            retry_started_at = time.perf_counter()
            try:
                store_info, retry_elapsed = self._run_store_timed(
                    task,
                    final_attempt=True,
                    completed_writes=first_result.get("_已完成写入", {}),
                    cached_capture=first_result if first_result.get("_阶段") == "写入" else None,
                )
            except Exception as exc:
                LOGGER.exception("[补跑][店铺线程异常] 店铺=%s，平台=%s", task.store_name, task.platform)
                store_info = {"店铺名": task.store_name, "平台": task.platform, "状态": "失败", "采集时间": "", "数据": {}, "错误": str(exc)}
                retry_elapsed = time.perf_counter() - retry_started_at
                self._notify_store_failure(task, store_info)
            store_info["执行次数"] = 2
            results_by_index[task_index] = store_info
            timing = store_processing_times[timing_keys_by_index[task_index]]
            total_elapsed = float(timing["耗时秒"]) + retry_elapsed
            timing.update({"耗时秒": round(total_elapsed, 2), "耗时": self._format_elapsed_time(total_elapsed), "补跑耗时秒": round(retry_elapsed, 2)})
            LOGGER.info("[补跑][完成] 店铺=%s，状态=%s，补跑耗时=%s，两次总耗时=%s", task.store_name, store_info.get("状态"), self._format_elapsed_time(retry_elapsed), timing["耗时"])
            if store_info.get("中止后续店铺"):
                close_failed = True
                self._stop_opening_stores.set()
            elif store_info.get("状态") == "成功":
                self._submit_store_analysis(task, store_info, deepseek_executor, single_store_enabled)

        # ALL_info 仍按飞书控制表顺序排列，避免并发完成顺序影响最终经理汇总。
        for store_info in results_by_index.values():
            store_info.pop("_已完成写入", None)
            store_info.pop("_采集结果", None)
            store_info.pop("_阶段", None)
        all_info.extend(results_by_index[index] for index in sorted(results_by_index))
        skipped_count = len(tasks) - len(results_by_index)
        if skipped_count:
            LOGGER.warning("[并发][未启动店铺] 因关闭失败跳过=%s", skipped_count)
        return close_failed

    def _submit_store_analysis(
        self, task: StoreTask, store_info: dict[str, Any], executor: ThreadPoolExecutor, enabled: bool,
    ) -> None:
        """仅为最终成功结果提交单店分析，失败的首轮或补跑均不提交。"""
        if task.platform not in {"tiktok", "shopee", "mercado"}:
            return
        if enabled:
            executor.submit(self._send_store_deepseek_analysis, task.recipient, store_info)
            LOGGER.info("[并发][DeepSeek提交] 店铺=%s，平台=%s；采集线程已释放，可启动下一店铺", task.store_name, task.platform)
        else:
            LOGGER.info("[并发][DeepSeek单店跳过] 店铺=%s，平台=%s，配置开关已关闭", task.store_name, task.platform)

    def _run_store_timed(
        self, task: StoreTask, *, final_attempt: bool = False, completed_writes: dict[str, bool] | None = None,
        cached_capture: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], float]:
        """执行单店采集并返回不包含 DeepSeek 等待时间的耗时。"""
        started_at = time.perf_counter()
        store_info = self._run_store(task, final_attempt=final_attempt, completed_writes=completed_writes, cached_capture=cached_capture)
        return store_info, time.perf_counter() - started_at

    @staticmethod
    def _build_store_timing_key(store_processing_times: dict[str, Any], store_name: str) -> str:
        """生成不会覆盖旧结果的店铺键；控制表出现同名店铺时自动增加序号。"""
        base_name = str(store_name or "未命名店铺")
        if base_name not in store_processing_times:
            return base_name

        duplicate_index = 2
        while f"{base_name}#{duplicate_index}" in store_processing_times:
            duplicate_index += 1
        return f"{base_name}#{duplicate_index}"

    @staticmethod
    def _format_elapsed_time(elapsed_seconds: float) -> str:
        """把秒数转换为便于终端阅读的“小时/分/秒”文本，并保留两位小数。"""
        safe_seconds = max(0.0, float(elapsed_seconds))
        hours = int(safe_seconds // 3600)
        minutes = int((safe_seconds % 3600) // 60)
        seconds = safe_seconds % 60
        if hours:
            return f"{hours}小时{minutes}分{seconds:.2f}秒"
        if minutes:
            return f"{minutes}分{seconds:.2f}秒"
        return f"{seconds:.2f}秒"

    @staticmethod
    def _print_store_processing_times(store_processing_times: dict[str, dict[str, Any]]) -> None:
        """在整轮末尾打印单店铺耗时和四项平台汇总，不写入日志文件。"""
        display_times = {
            store_name: str(details.get("耗时") or "")
            for store_name, details in store_processing_times.items()
        }
        print(json.dumps(display_times, ensure_ascii=False, separators=(",", ":")))

        # 直接累计每间店铺记录的秒数，不从“2分31.51秒”等显示文本反向解析，
        # 避免字符串格式变化或跨小时后造成统计错误。
        platform_seconds = {
            "tiktok": 0.0,
            "shopee": 0.0,
            "mercado": 0.0,
        }
        total_seconds = 0.0
        for details in store_processing_times.values():
            try:
                elapsed_seconds = max(0.0, float(details.get("耗时秒") or 0))
            except (TypeError, ValueError):
                elapsed_seconds = 0.0
            total_seconds += elapsed_seconds
            platform = str(details.get("平台") or "").strip().lower()
            if platform in platform_seconds:
                platform_seconds[platform] += elapsed_seconds

        summary_times = {
            "总时间": DailyStoreCheck._format_total_minutes_seconds(total_seconds),
            "TK总时间": DailyStoreCheck._format_total_minutes_seconds(platform_seconds["tiktok"]),
            "虾皮总时间": DailyStoreCheck._format_total_minutes_seconds(platform_seconds["shopee"]),
            "美客多总时间": DailyStoreCheck._format_total_minutes_seconds(platform_seconds["mercado"]),
        }
        print(json.dumps(summary_times, ensure_ascii=False, separators=(",", ":")))

    @staticmethod
    def _format_total_minutes_seconds(elapsed_seconds: float) -> str:
        """把汇总秒数转换为累计“分钟+秒”；超过一小时也不改成小时格式。"""
        safe_seconds = max(0.0, float(elapsed_seconds))
        total_minutes = int(safe_seconds // 60)
        seconds = safe_seconds % 60
        return f"{total_minutes}分{seconds:.2f}秒"

    def _prepare_ziniao(self) -> None:
        """启动紫鸟、更新内核并缓存全部店铺信息。"""
        self.ziniao.start_client()
        self.ziniao.update_core()
        self.browser_list = self.ziniao.list_browsers()

    def _outbox(self) -> WriteOutbox:
        """补写状态只保存到本机业务输出目录，不包含登录或接口凭据。"""
        return WriteOutbox(PROJECT_ROOT / str(self.config.get("data", {}).get("output_dir", "data")))

    def _run_store(
        self, task: StoreTask, *, final_attempt: bool = False, completed_writes: dict[str, bool] | None = None,
        cached_capture: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """采集失败重开店；已关闭浏览器的写入失败只补写同一批次。"""
        store_info: dict[str, Any] = {
            "店铺名": task.store_name, "平台": task.platform, "状态": "未执行", "采集时间": "", "数据": {},
        }
        write_progress = dict(completed_writes or {})
        rows: list[dict[str, Any]] = []
        try:
            if cached_capture is None and hasattr(self, "config"):
                cached_capture = self._outbox().load(task)
            if cached_capture is not None:
                rows = cached_capture["_采集结果"]
                write_progress.update(cached_capture.get("_已完成写入", {}))
                LOGGER.info("[补写][恢复快照] 店铺=%s，批次=%s，不重新打开紫鸟浏览器", task.store_name, cached_capture.get("采集批次", ""))
            else:
                identifier = task.browser_oauth or task.browser_id or self._find_browser_identifier(task.store_name, task.platform)
                if not identifier:
                    raise RuntimeError("没有找到紫鸟店铺标识，请检查店铺名是否与紫鸟一致。")
                store_info["_阶段"] = "采集"
                with ZiniaoStoreSession(self.ziniao, identifier, task.store_name) as session:
                    crawler = self._load_crawler(task.platform)
                    rows = crawler.collect(task.store_name, session.download_path, session.opened.get("debuggingPort"))
                    collected_at, values, metadata = extract_today(rows, task.platform)
                    store_info.update({"采集时间": collected_at, "数据": values, "指标元数据": metadata})
                    # 即使部分模块失败，也保存已拿到的原始结果；失败快照不得进入写表补写队列。
                    self._save_store_capture(task, rows, store_info)
                    row_errors = [
                        f"{row.get('指标')}: {row.get('显示值') or '采集失败'}"
                        for row in rows if str(row.get("指标") or "").endswith("_失败")
                    ]
                    if row_errors:
                        raise RuntimeError("；".join(row_errors))
            collected_at, values, metadata = extract_today(rows, task.platform)
            store_info.update({
                "采集时间": collected_at, "数据": values, "指标元数据": metadata,
                "业务日期": business_date(collected_at),
                "采集批次": capture_batch(task.platform, task.store_name, collected_at),
                "_采集结果": rows, "_阶段": "写入",
            })
            # 到这里浏览器已成功关闭；任何补写都不得再次打开店铺。
            try:
                self._outbox().save(task, rows, store_info, write_progress)
            except OSError:
                LOGGER.exception("[补写][本机状态保存失败] 店铺=%s，继续尝试飞书写入", task.store_name)
            self._write_feishu(task, rows, completed_writes=write_progress)
            self._outbox().save(task, rows, store_info, write_progress)
            platform_config = self.config.get("platforms", {}).get(task.platform, {})
            formatter = {
                "tiktok": self._format_tiktok_notification,
                "shopee": self._format_shopee_notification,
                "mercado": self._format_mercado_notification,
            }.get(task.platform)
            if formatter:
                title, body = formatter(task.store_name, rows, platform_config)
                self._safe_notify_markdown(task.recipient, title, body)
                if task.platform == "shopee":
                    self._send_shopee_analytics_notifications(task.recipient, task.store_name, rows, platform_config)
            else:
                self._safe_notify(task.recipient, f"{task.store_name} 数据", self._format_rows(rows))
            store_info["状态"] = "成功"
        except ZiniaoStoreCloseError as exc:
            LOGGER.exception("店铺 %s 未能关闭，必须中止后续店铺", task.store_name)
            store_info.update({"状态": "失败", "错误": str(exc), "中止后续店铺": True, "_阶段": "关闭"})
        except Exception as exc:
            LOGGER.exception("店铺 %s 处理失败，阶段=%s", task.store_name, store_info.get("_阶段", "准备"))
            store_info.update({"状态": "失败", "错误": str(exc)})
        if store_info["状态"] == "失败":
            store_info["_已完成写入"] = write_progress
            if store_info.get("_阶段") == "写入" and rows:
                try:
                    self._outbox().save(task, rows, store_info, write_progress)
                except OSError:
                    LOGGER.exception("[补写][失败状态保存异常] 店铺=%s", task.store_name)
            if final_attempt:
                self._notify_store_failure(task, store_info)
            else:
                LOGGER.warning("[补跑][首轮失败暂存] 店铺=%s，阶段=%s，等待首轮结束后重试", task.store_name, store_info.get("_阶段", "准备"))
        return store_info

    def _notify_store_failure(self, task: StoreTask, store_info: dict[str, Any]) -> None:
        """仅在补跑结束或无法安全补跑时发送最终失败状态。"""
        title = f"{task.store_name} 关闭失败" if store_info.get("中止后续店铺") else f"{task.store_name} 数据任务失败"
        error_message = str(store_info.get("错误") or "店铺执行失败")
        if store_info.get("中止后续店铺"):
            error_message += "\n店铺未确认关闭，已停止打开新店铺。"
        self._safe_notify(task.recipient, title, error_message)

    def _save_store_capture(
        self,
        task: StoreTask,
        rows: list[dict[str, Any]],
        store_info: dict[str, Any],
    ) -> None:
        """把已采集的店铺结果写入本机 JSON，供飞书或消息失败后补写。"""
        output_dir = PROJECT_ROOT / str(self.config.get("data", {}).get("output_dir", "data") or "data")
        capture_dir = output_dir / "captures"
        capture_dir.mkdir(parents=True, exist_ok=True)
        collected_at = store_info.get("采集时间") or datetime.now(timezone.utc).isoformat()
        timestamp = re.sub(r"[^0-9]", "", str(collected_at))[:14] or datetime.now().strftime("%Y%m%d%H%M%S")
        safe_name = re.sub(r'[<>:"/\\|?*\s]+', "_", task.store_name).strip("._") or "store"
        path = capture_dir / f"{timestamp}_{task.platform}_{safe_name}.json"
        suffix = 2
        while path.exists():
            path = capture_dir / f"{timestamp}_{task.platform}_{safe_name}_{suffix}.json"
            suffix += 1
        payload = {
            "店铺名": task.store_name,
            "平台": task.platform,
            "采集时间": collected_at,
            "推送人员": task.recipient,
            "数据": store_info.get("数据", {}),
            "原始结果": rows,
        }
        temporary_path = path.with_suffix(".json.tmp")
        temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        temporary_path.replace(path)
        LOGGER.info("[采集][本机已保存] 店铺=%s，平台=%s，文件=%s", task.store_name, task.platform, path.name)

    @staticmethod
    def _extract_all_info_values(rows: list[dict[str, Any]]) -> tuple[Any, dict[str, Any]]:
        """兼容旧调用入口，返回规范化的今天指标，不再覆盖不同来源的订单数。"""
        collected_at, values, _ = extract_today(rows)
        return collected_at, values

    def _send_all_info_summary(self, ALL_info: list[dict[str, Any]]) -> None:
        """把非空 ALL_info 交给 DeepSeek 分析，再把返回文本发送给全部汇总接收人。"""
        if not ALL_info:
            LOGGER.info("[DeepSeek][ALL_info汇总] 本轮没有店铺结果，不调用 DeepSeek")
            return

        LOGGER.info(
            "[DeepSeek][ALL_info汇总] 准备分析本轮全部店铺，店铺数=%s，ALL_info=%s",
            len(ALL_info),
            json.dumps(ALL_info, ensure_ascii=False, default=str),
        )
        try:
            analysis_text = self.deepseek.analyze_all_info(ALL_info)
        except Exception:
            # AI 汇总属于最终附加步骤，失败不能影响已完成的数据写入、紫鸟退出和计时输出。
            LOGGER.exception("[DeepSeek][ALL_info分析失败] 本轮不发送 AI 汇总")
            return
        if not analysis_text:
            LOGGER.warning("[DeepSeek][ALL_info汇总跳过] 未获得有效分析文本，不发送最终汇总")
            return

        recipients = self.feishu.get_summary_recipients()
        if not recipients:
            LOGGER.info("[飞书][DeepSeek汇总] summary_recipients 为空，分析完成但没有接收人")
            return

        LOGGER.info(
            "[飞书][DeepSeek汇总准备] 接收人数=%s，分析字符数=%s，分析内容=%r",
            len(recipients),
            len(analysis_text),
            analysis_text,
        )
        for recipient_name, receive_id in recipients.items():
            LOGGER.info("[飞书][DeepSeek汇总发送] 接收人姓名=%s，准备发送 AI 店铺分析", recipient_name)
            self._safe_notify_markdown(receive_id, "DeepSeek全部店铺数据分析", analysis_text, compact=True)

    def _send_store_deepseek_analysis(self, recipient: str, store_info: dict[str, Any]) -> None:
        """原始平台消息发送完毕后，同步分析单店数据并回发同一运营人员。"""
        if not bool(getattr(getattr(self, "deepseek", None), "single_store_enabled", True)):
            LOGGER.info("[DeepSeek][单店跳过] 配置开关 deepseek.single_store_enabled=false")
            return
        store_name = str(store_info.get("店铺名") or "未知店铺")
        platform = str(store_info.get("平台") or "").strip().lower()
        platform_name = {
            "tiktok": "TikTok Shop",
            "shopee": "Shopee",
            "mercado": "Mercado Livre",
        }.get(platform, platform or "未知平台")
        if not str(recipient or "").strip():
            LOGGER.warning("[DeepSeek][单店跳过] 平台=%s，店铺=%s，没有对应运营人员", platform_name, store_name)
            return

        LOGGER.info(
            "[DeepSeek][单店分析开始] 平台=%s，店铺=%s，等待分析结果后回发运营人员",
            platform_name,
            store_name,
        )
        try:
            analysis_text = self.deepseek.analyze_store(store_info)
        except Exception:
            # 单店 AI 分析属于附加步骤，失败不能覆盖已经成功的采集、写表和原始消息。
            LOGGER.exception(
                "[DeepSeek][单店分析失败] 平台=%s，店铺=%s，不影响原始数据结果",
                platform_name,
                store_name,
            )
            self._safe_notify_markdown(
                recipient,
                f"{store_name} {platform_name} 每日经营分析失败",
                "DeepSeek 暂时未能返回分析结果，原始采集数据不受影响。请稍后查看运行日志或重试。",
            )
            return
        if not analysis_text:
            LOGGER.warning(
                "[DeepSeek][单店分析跳过] 平台=%s，店铺=%s，未获得有效分析文本",
                platform_name,
                store_name,
            )
            return

        LOGGER.info(
            "[飞书][DeepSeek单店发送] 平台=%s，店铺=%s，分析字符数=%s",
            platform_name,
            store_name,
            len(analysis_text),
        )
        self._safe_notify_markdown(
            recipient,
            f"{store_name} {platform_name} 每日经营分析",
            analysis_text,
        )

    def _send_shopee_analytics_notifications(
        self,
        recipient: str,
        store_name: str,
        rows: list[dict[str, Any]],
        platform_config: dict[str, Any] | None = None,
    ) -> None:
        """把商业分析每个页面的今天、昨天、近7天合并为横向对比表。"""
        analytics_values = {
            str(row.get("指标") or ""): str(row.get("显示值") or "")
            for row in rows
            if str(row.get("指标") or "").startswith("Shopee")
        }
        for page_name in SHOPEE_ANALYTICS_PAGE_NAMES:
            period_payloads: dict[str, dict[str, Any]] = {}
            for period in ("今天",):
                if not is_period_enabled(platform_config, period):
                    continue
                field_name = f"Shopee{page_name}_{period}"
                payload = analytics_values.get(field_name, "")
                if not payload:
                    continue
                try:
                    parsed_payload = json.loads(payload)
                except (TypeError, ValueError, json.JSONDecodeError):
                    LOGGER.warning(
                        "[飞书][Shopee商业分析解析失败] 店铺=%s，页面=%s，时间范围=%s",
                        store_name,
                        page_name,
                        period,
                    )
                    continue
                if isinstance(parsed_payload, dict):
                    period_payloads[period] = parsed_payload
            if not period_payloads:
                LOGGER.warning("[飞书][Shopee商业分析跳过] 店铺=%s，页面=%s，没有有效数据", store_name, page_name)
                continue
            markdown = self._format_shopee_analytics_period_table(page_name, period_payloads, platform_config)
            chunks = self._split_markdown_table(markdown, page_name, max_chunk_size=3500)
            for chunk_index, chunk in enumerate(chunks, start=1):
                suffix = f"（{chunk_index}/{len(chunks)}）" if len(chunks) > 1 else ""
                self._safe_notify_markdown(
                    recipient,
                    f"{store_name} Shopee {page_name}{suffix}",
                    chunk,
                )

    @staticmethod
    def _format_shopee_analytics_period_table(
        page_name: str,
        period_payloads: dict[str, dict[str, Any]],
        platform_config: dict[str, Any] | None = None,
    ) -> str:
        """商业分析只展示今天，数据未准备好的提示只发消息。"""
        period_order = tuple(
            period for period in ("今天",) if is_period_enabled(platform_config, period)
        )
        if not period_order:
            return f"## {page_name}\n\n本次未开启任何商业分析时间范围。"
        value_maps: dict[str, dict[str, str]] = {}
        metric_order: list[str] = []
        for period in period_order:
            payload = period_payloads.get(period, {})
            values: dict[str, str] = {}
            if payload.get("状态") == "数据尚未准备好":
                values["状态"] = str(payload.get("提示") or "数据尚未准备好")
            metrics = payload.get("指标", [])
            if isinstance(metrics, list):
                for metric in metrics:
                    if not isinstance(metric, dict):
                        continue
                    name = str(metric.get("名称") or "").strip()
                    section = str(metric.get("分组") or "").strip()
                    if not name:
                        continue
                    base_name = f"{section} / {name}" if section else name
                    if all(key in metric for key in ("全部", "APP", "PC")):
                        for device in ("全部", "APP", "PC"):
                            values[f"{base_name}（{device}）"] = str(metric.get(device, "-") or "-")
                    else:
                        values[base_name] = str(metric.get("值", "-") or "-")
            sources = payload.get("来源分布", [])
            if isinstance(sources, list):
                for source in sources:
                    if not isinstance(source, dict):
                        continue
                    source_name = str(source.get("流量来源") or "未知来源").strip()
                    for field_name, field_value in source.items():
                        if field_name == "流量来源":
                            continue
                        values[f"来源分布 / {source_name} / {field_name}"] = str(field_value or "-")
            value_maps[period] = values
            for metric_name in values:
                if metric_name not in metric_order:
                    metric_order.append(metric_name)

        period_labels = {"今天": "今天", "昨天": "昨天", "7天": "近7天"}
        header_cells = ["指标", *(period_labels[period] for period in period_order)]
        lines = [
            f"## {page_name}",
            "",
            f"| {' | '.join(header_cells)} |",
            "|---|" + "---:|" * len(period_order),
        ]
        for metric_name in metric_order:
            cells = [DailyStoreCheck._escape_markdown_table_cell(metric_name)]
            cells.extend(
                DailyStoreCheck._escape_markdown_table_cell(value_maps.get(period, {}).get(metric_name, "-"))
                for period in period_order
            )
            lines.append(f"| {' | '.join(cells)} |")
        return "\n".join(lines)

    @staticmethod
    def _escape_markdown_table_cell(value: Any) -> str:
        """转义表格单元格中的竖线和换行，避免破坏 Markdown 列结构。"""
        return str(value if value not in (None, "") else "-").replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")

    @staticmethod
    def _split_markdown_table(content: str, group_name: str, max_chunk_size: int) -> list[str]:
        """按完整数据行拆分长表格，并在每段重复标题和表头。"""
        lines = content.splitlines()
        table_index = next((index for index, line in enumerate(lines) if line.startswith("| 指标 |")), -1)
        if table_index < 0 or table_index + 1 >= len(lines):
            return [content]
        data_rows = lines[table_index + 2:]
        table_header = lines[table_index:table_index + 2]
        chunks: list[str] = []
        current_lines = lines[:table_index] + table_header
        for row in data_rows:
            candidate = "\n".join(current_lines + [row])
            if len(candidate) > max_chunk_size and len(current_lines) > len(table_header):
                chunks.append("\n".join(current_lines))
                current_lines = [f"## {group_name}（续）", ""] + table_header
            current_lines.append(row)
        if current_lines:
            chunks.append("\n".join(current_lines))
        return chunks

    def _safe_notify(self, recipient: str, title: str, content: str) -> None:
        """推送失败只记录日志，不影响关闭店铺和后续店铺。"""
        try:
            LOGGER.info(
                "[飞书][机器人消息打包] recipient=%s，title=%r，content=%r",
                recipient or "<空接收人>",
                title,
                content,
            )
            self.feishu.send_robot_message(recipient, title, content)
        except Exception:
            LOGGER.exception("飞书消息推送失败 recipient=%s", recipient)

    def _safe_notify_markdown(
        self, recipient: str, title: str, content: str, *, compact: bool = False,
    ) -> None:
        """以 interactive Markdown 卡片发送 DeepSeek 汇总，失败时不影响任务收尾。"""
        try:
            LOGGER.info(
                "[飞书][Markdown机器人消息打包] recipient=%s，title=%r，markdown=%r",
                recipient or "<空接收人>",
                title,
                content,
            )
            if compact:
                self.feishu.send_robot_markdown_message(recipient, title, content, compact=True)
            else:
                self.feishu.send_robot_markdown_message(recipient, title, content)
        except Exception:
            LOGGER.exception("飞书 Markdown 消息推送失败 recipient=%s", recipient)

    def _find_browser_identifier(self, store_name: str, platform: str = "") -> str:
        """按店铺名查找紫鸟标识，先精确匹配，再按“平台+店号”匹配。"""
        wanted_name = self._normalise_store_name(store_name)
        wanted_key = self._build_platform_store_key(store_name, platform)
        LOGGER.info(
            "[紫鸟][店铺匹配] 控制表名称=%r，平台=%r，精确名称=%r，平台店号键=%r",
            store_name,
            platform,
            wanted_name,
            wanted_key or "<无法提取>",
        )

        # 如果名称完全一致，优先使用原来的精确匹配行为。
        for browser in self.browser_list:
            browser_name = str(browser.get("browserName") or browser.get("name") or "").strip()
            if self._normalise_store_name(browser_name) != wanted_name:
                continue
            identifier = str(browser.get("browserOauth") or browser.get("browserId") or "")
            if identifier:
                LOGGER.info("[紫鸟][店铺精确匹配成功] 控制表=%r，紫鸟名称=%r，标识=%s", store_name, browser_name, identifier)
                return identifier

        if not wanted_key:
            LOGGER.warning("[紫鸟][店铺匹配失败] 名称=%r 无法提取平台+店号", store_name)
            return ""

        # 紫鸟名称可能带公司名、空格、末尾“店”等附加内容，只比较规范化后的平台+店号键。
        candidates: list[tuple[str, dict[str, Any]]] = []
        for browser in self.browser_list:
            browser_name = str(browser.get("browserName") or browser.get("name") or "").strip()
            browser_key = self._build_platform_store_key(browser_name, platform)
            if browser_key == wanted_key:
                candidates.append((browser_name, browser))

        if not candidates:
            LOGGER.warning("[紫鸟][店铺平台店号匹配失败] 控制表=%r，平台店号键=%s", store_name, wanted_key)
            return ""

        if len(candidates) > 1:
            LOGGER.warning(
                "[紫鸟][店铺平台店号匹配歧义] 控制表=%r，键=%s，候选=%s；采用第一个候选",
                store_name,
                wanted_key,
                [name for name, _ in candidates],
            )

        # 某些紫鸟列表项可能只有展示名称没有可用 browserOauth/browserId，跳过这类候选，
        # 避免它排在前面时遮挡后面真正可打开的同键店铺。
        valid_candidates = [
            (name, browser)
            for name, browser in candidates
            if str(browser.get("browserOauth") or browser.get("browserId") or "")
        ]
        if not valid_candidates:
            LOGGER.warning("[紫鸟][店铺平台店号匹配失败] 键=%s 的候选都没有 browserOauth/browserId", wanted_key)
            return ""

        browser_name, browser = valid_candidates[0]
        identifier = str(browser.get("browserOauth") or browser.get("browserId") or "")
        if identifier:
            LOGGER.info(
                "[紫鸟][店铺平台店号匹配成功] 控制表=%r -> 紫鸟名称=%r，键=%s，标识=%s",
                store_name,
                browser_name,
                wanted_key,
                identifier,
            )
        return identifier

    @staticmethod
    def _normalise_store_name(store_name: Any) -> str:
        """统一店铺名称的 Unicode、大小写和空白，用于精确名称比较。"""
        value = unicodedata.normalize("NFKC", str(store_name or "")).strip().casefold()
        return re.sub(r"\s+", "", value)

    @staticmethod
    def _build_platform_store_key(store_name: Any, platform: str = "") -> str:
        """从任意店铺名中提取稳定的 ``平台:店号`` 键。"""
        value = unicodedata.normalize("NFKC", str(store_name or "")).strip().casefold()
        # 与控制表读取阶段共用平台别名规则，兼容 tk/sp/mkd/meicado 等写法。
        platform_value = normalise_platform(unicodedata.normalize("NFKC", str(platform or "")).strip())
        patterns: dict[str, tuple[str, ...]] = {
            "tiktok": (r"(?:tiktok|tk)\s*[-_#]?\s*0*(\d+)",),
            "shopee": (r"(?:shopee|虾皮)\s*[-_#]?\s*0*(\d+)",),
            "mercado": (r"(?:mercado(?:\s*libre)?|美客多)\s*[-_#]?\s*0*(\d+)",),
        }
        selected_patterns = patterns.get(platform_value, ())
        if not selected_patterns:
            # platform 为空或使用了未知别名时，按所有平台尝试；正常控制表任务都会带平台字段。
            selected_patterns = tuple(pattern for group in patterns.values() for pattern in group)

        for pattern in selected_patterns:
            match = re.search(pattern, value, flags=re.IGNORECASE)
            if match:
                store_number = str(int(match.group(1)))
                for platform_key, platform_patterns in patterns.items():
                    if pattern in platform_patterns:
                        return f"{platform_key}:{store_number}"
        return ""

    def _load_crawler(self, platform: str) -> Any:
        """从配置中的 module:Class 动态加载平台爬虫。"""
        crawler_path = self.config.get("platforms", {}).get(platform, {}).get("crawler", "")
        if ":" not in crawler_path:
            raise ValueError(f"平台 {platform} 未配置 crawler")
        module_name, class_name = crawler_path.split(":", 1)
        crawler_class = getattr(importlib.import_module(module_name), class_name)
        platform_config = dict(self.config.get("platforms", {}).get(platform, {}) or {})
        # 真人鼠标设置是跨平台总开关，平台配置不得单独覆盖。
        platform_config["human_interaction"] = dict(self.config.get("human_interaction", {}) or {})
        return crawler_class(platform_config)

    def _write_feishu(
        self, task: StoreTask, rows: list[dict[str, Any]], *, completed_writes: dict[str, bool] | None = None,
    ) -> None:
        """按今天宽字段分别写两种表；每个目的地都有独立幂等键和补写进度。"""
        fields = today_record(task.platform, task.store_name, rows)
        batch_id = fields["采集批次"]
        progress = completed_writes if completed_writes is not None else {}
        if not any(name in fields for name in TODAY_FIELD_ORDER_BY_PLATFORM[task.platform]):
            LOGGER.info("[飞书][今日无有效业务数据] 店铺=%s，仅保留提示消息，不写空业务行", task.store_name)
            progress.update({"多维表": True, "电子表": True})
            progress.pop("sheet_uncertain", None)
            return
        errors: list[str] = []
        collected_at, values, metadata = extract_today(rows, task.platform)
        info = {
            "采集时间": collected_at, "业务日期": fields["业务日期"],
            "采集批次": batch_id, "数据": values, "指标元数据": metadata,
        }

        def mark_append_in_flight() -> None:
            # 本机状态未保存成功时不允许发送无幂等保护的追加请求。
            progress["sheet_uncertain"] = True
            self._outbox().save(task, rows, info, progress)

        for destination, writer in (
            ("多维表", self.feishu.write_today_bitable),
            ("电子表", self.feishu.write_today_spreadsheet),
        ):
            if progress.get(destination):
                LOGGER.info("[补写][已完成跳过] 店铺=%s，批次=%s，目标=%s", task.store_name, batch_id, destination)
                continue
            sheet_was_uncertain = bool(progress.get("sheet_uncertain"))
            try:
                if destination == "电子表":
                    # 上次追加结果不确定时只能回读核验；补跑和重启都不得再派发追加。
                    writer(
                        task.platform, fields, batch_id=batch_id, verify_only=sheet_was_uncertain,
                        on_append_attempt=mark_append_in_flight,
                    )
                else:
                    writer(task.platform, fields, batch_id=batch_id)
                progress[destination] = True
                if destination == "电子表":
                    progress.pop("sheet_uncertain", None)
            except Exception as exc:
                if destination == "电子表" and (
                    sheet_was_uncertain or (
                        isinstance(exc, FeishuWriteUncertainError) and exc.record_write_uncertain
                    )
                ):
                    progress["sheet_uncertain"] = True
                    LOGGER.warning("[补写][追加结果不确定] 店铺=%s，批次=%s，保留状态，后续仅核验不重复追加", task.store_name, batch_id)
                elif destination == "电子表":
                    # 明确拒绝或尚未派发业务行的字段准备失败，可按正常补写流程重试。
                    progress.pop("sheet_uncertain", None)
                LOGGER.exception("[飞书][今日写入失败] 店铺=%s，批次=%s，目标=%s", task.store_name, batch_id, destination)
                errors.append(f"{destination}写入失败: {exc}")
            finally:
                try:
                    self._outbox().save(task, rows, info, progress)
                except OSError:
                    LOGGER.exception("[补写][目的地进度保存失败] 店铺=%s", task.store_name)
        if errors:
            raise RuntimeError("；".join(errors))

    def _align_shopee_bitable_fields(
        self,
        records: list[dict[str, Any]],
        app_token: str,
        table_id: str,
    ) -> list[dict[str, Any]]:
        """按飞书真实字段名对齐 Shopee 记录，兼容“券/劵”和首尾空格差异。"""
        try:
            actual_fields = self.feishu.list_table_field_names(table_id, app_token=app_token)
        except Exception:
            LOGGER.exception("[飞书][SP字段预检失败] 无法读取真实字段，暂时沿用代码字段名")
            return records
        if not actual_fields:
            return records

        actual_by_normalized: dict[str, list[str]] = {}
        for actual_name in actual_fields:
            normalized = self._normalise_feishu_field_name(actual_name)
            actual_by_normalized.setdefault(normalized, []).append(actual_name)

        aligned_records: list[dict[str, Any]] = []
        for record_index, record in enumerate(records, start=1):
            aligned: dict[str, Any] = {}
            missing_required: list[str] = []
            for code_name, value in record.items():
                if code_name in actual_fields:
                    aligned[code_name] = value
                    continue

                candidates = actual_by_normalized.get(self._normalise_feishu_field_name(code_name), [])
                if len(candidates) == 1:
                    actual_name = candidates[0]
                    aligned[actual_name] = value
                    LOGGER.warning("[飞书][SP字段自动对齐] 代码字段=%r -> 真实字段=%r", code_name, actual_name)
                    continue

                if code_name in {"店铺名", "采集时间"}:
                    missing_required.append(code_name)
                elif len(candidates) > 1:
                    LOGGER.error("[飞书][SP字段歧义] 代码字段=%r 匹配到多个真实字段=%s，本次跳过", code_name, candidates)
                else:
                    LOGGER.error("[飞书][SP字段不存在] 代码字段=%r 不在真实多维表中，本次跳过该字段", code_name)

            if missing_required:
                raise RuntimeError(f"Shopee 多维表缺少必要字段: {missing_required}")
            aligned_records.append(aligned)
            LOGGER.info(
                "[飞书][SP字段预检完成] 第 %s 条记录，发送字段数=%s，字段=%s",
                record_index,
                len(aligned),
                list(aligned),
            )
        return aligned_records

    @staticmethod
    def _normalise_feishu_field_name(field_name: str) -> str:
        """仅用于字段匹配：统一全半角、首尾空格，并把易混淆的“劵”归一为“券”。"""
        return unicodedata.normalize("NFKC", str(field_name)).strip().replace("劵", "券")

    def _build_default_feishu_rows(
        self,
        task: StoreTask,
        rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[list[Any]]]:
        """为以后新增但尚未提供专用飞书字段结构的平台打包通用字段。"""
        bitable = self.config.get("feishu", {}).get("bitable", {})
        fields = bitable.get("data_fields", {})
        now = datetime.now(timezone.utc).isoformat()
        record_rows: list[dict[str, Any]] = []
        spreadsheet_rows: list[list[Any]] = []
        for row in rows:
            collected_at = row.get("采集时间", now)
            record = {
                fields.get("store_name", "店铺名"): row.get("店铺名", task.store_name),
                fields.get("collected_at", "采集时间"): self._to_feishu_timestamp_ms(collected_at),
                fields.get("metric", "指标"): row.get("指标", ""),
                fields.get("value", "数值"): row.get("数值", ""),
                fields.get("raw_data", "原始数据"): row.get("原始数据", ""),
            }
            # 平台可以提供自己的字段字典。美客多使用独立的 32 字段打包逻辑，
            # 其他平台没有该字段时仍沿用上面的通用结构。
            platform_fields = row.get("飞书字段", {})
            if isinstance(platform_fields, dict):
                # 空字符串不能写入飞书数字、货币、百分比等字段，因此空指标不进入请求体。
                record.update({name: value for name, value in platform_fields.items() if value not in ("", None)})
            record_rows.append(record)
            spreadsheet_row = [
                record.get(fields.get("store_name", "店铺名")),
                collected_at,
                task.platform,
                record.get(fields.get("metric", "指标")),
                record.get(fields.get("value", "数值")),
                record.get(fields.get("raw_data", "原始数据")),
            ]
            if isinstance(platform_fields, dict):
                spreadsheet_row.extend(platform_fields.values())
            spreadsheet_rows.append(spreadsheet_row)
        return record_rows, spreadsheet_rows

    def _build_today_feishu_rows(
        self, task: StoreTask, rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[list[Any]]]:
        """统一今日宽行；三个平台不再各自维护一份过时字段过滤表。"""
        fields = today_record(task.platform, task.store_name, rows)
        order = today_field_names(task.platform)
        record = {name: fields[name] for name in order if fields.get(name) not in ("", None)}
        return [record], [[fields.get(name, "") for name in order]]

    def _build_tiktok_feishu_rows(self, task: StoreTask, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[list[Any]]]:
        return self._build_today_feishu_rows(task, rows)

    def _build_shopee_feishu_rows(self, task: StoreTask, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[list[Any]]]:
        return self._build_today_feishu_rows(task, rows)

    def _build_mercado_feishu_rows(self, task: StoreTask, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[list[Any]]]:
        return self._build_today_feishu_rows(task, rows)

    @staticmethod
    def _format_roas_text(value: Any, platform_log_name: str = "") -> str:
        """把广告支出回报率转换为飞书 STRING 字段需要的普通小数字符串。"""
        text = str(value).strip()
        if not text:
            return ""
        try:
            number = float(text.replace(",", ".").replace("%", "").strip())
        except (TypeError, ValueError):
            LOGGER.warning("[飞书][%s广告支出回报率转换失败] 原始值=%r，保留原文本", platform_log_name or "平台", value)
            return text
        return f"{number:.2f}"

    @staticmethod
    def _format_double_value(value: Any, field_name: str, platform_log_name: str = "") -> int | float | str:
        """把 SP DOUBLE 字段规范成数字；误传带 R$ 的展示文本时先解析再写入。"""
        if isinstance(value, bool):
            LOGGER.error("[飞书][%s DOUBLE类型错误] 字段=%s，禁止写入布尔值=%r", platform_log_name or "平台", field_name, value)
            return ""
        if isinstance(value, (int, float)):
            return value

        text = str(value or "").strip()
        if not text:
            return ""
        cleaned = text.replace("R$", "").replace("BRL", "").replace(" ", "")
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", ".")
        try:
            number = float(cleaned)
        except ValueError:
            LOGGER.error("[飞书][%s DOUBLE转换失败] 字段=%s，值=%r", platform_log_name or "平台", field_name, value)
            return ""
        return int(number) if number.is_integer() else round(number, 2)

    @staticmethod
    def _log_and_validate_shopee_bitable_record(record: dict[str, Any]) -> None:
        """打印 SP 最终请求字段并在本地拦截 STRING/DOUBLE 类型错误。"""
        for field_name, value in record.items():
            LOGGER.info(
                "[飞书][SP最终字段] 字段=%s，值=%r，Python类型=%s",
                field_name,
                value,
                type(value).__name__,
            )
            if field_name in SHOPEE_PERCENT_TEXT_FIELDS or field_name in SHOPEE_ROAS_TEXT_FIELDS:
                if not isinstance(value, str):
                    raise TypeError(f"SP 字段 {field_name} 必须是 STRING，实际类型={type(value).__name__}")
            elif field_name in SHOPEE_DOUBLE_FIELDS:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise TypeError(f"SP 字段 {field_name} 必须是 DOUBLE，实际值={value!r}")

    @staticmethod
    def _format_percent_text(value: Any, platform_log_name: str = "") -> str:
        """把平台内部的数值比例转换为飞书文本字段使用的百分比字符串。"""
        text = str(value).strip()
        already_percent = text.endswith("%")
        number_text = text[:-1].strip() if already_percent else text

        try:
            number = float(number_text.replace(",", "."))
        except (TypeError, ValueError):
            LOGGER.warning("[飞书][%s百分比转换失败] 原始值=%r，保留原文本", platform_log_name or "平台", value)
            return text

        # 正常爬虫结果是 0~1 的比例；也兼容 "33,3%" 或人工传入 33.3 这种百分数。
        percent_number = number if already_percent or abs(number) > 1 else number * 100
        return f"{percent_number:.1f}%"

    @staticmethod
    def _to_feishu_timestamp_ms(value: Any) -> int:
        """把 ISO 日期、秒级时间戳或毫秒时间戳统一转换成飞书日期字段需要的毫秒整数。"""
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            number = float(value)
            return int(number if number >= 1_000_000_000_000 else number * 1000)
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
        except (TypeError, ValueError, OSError):
            fallback = int(datetime.now(timezone.utc).timestamp() * 1000)
            LOGGER.warning("[飞书][日期转换失败] 原始值=%r，改用当前时间戳=%s", value, fallback)
            return fallback

    def _cleanup_retention(self) -> None:
        """清理三张数据多维表中的旧记录，历史电子表不删除。"""
        bitable = self.config.get("feishu", {}).get("bitable", {})
        fields = bitable.get("data_fields", {})
        retention = int(self.config.get("data", {}).get("retention_days", 90))
        for platform in ("tiktok", "shopee", "mercado"):
            app_token, table_id = self.feishu.get_bitable_ref("data", platform)
            if table_id:
                removed = self.feishu.remove_old_records(table_id, fields.get("collected_at", "采集时间"), retention, app_token=app_token)
                if removed:
                    LOGGER.info("平台 %s 清理旧数据 %s 条", platform, removed)
        self._cleanup_old_captures()

    def _cleanup_old_captures(self) -> None:
        """删除超过保留天数的本机采集 JSON，避免 data/captures 无限增长。"""
        data_config = self.config.get("data", {})
        retention_days = int(data_config.get("capture_retention_days", data_config.get("retention_days", 90)) or 90)
        output_dir = PROJECT_ROOT / str(data_config.get("output_dir", "data") or "data")
        capture_dir = output_dir / "captures"
        if not capture_dir.is_dir():
            return
        cutoff = time.time() - retention_days * 24 * 60 * 60
        removed = 0
        for path in capture_dir.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                LOGGER.warning("[采集][本机清理失败] 文件=%s", path.name, exc_info=True)
        if removed:
            LOGGER.info("[采集][本机清理] 删除超过 %s 天的采集文件 %s 个", retention_days, removed)

    @staticmethod
    def _format_today_notification(
        platform: str, store_name: str, rows: list[dict[str, Any]],
    ) -> tuple[str, str]:
        """机器人与 AI 共用今日字段及币种，缺失数据不冒充零或历史指标。"""
        collected_at, values, metadata = extract_today(rows, platform)
        groups = {"tiktok": TIKTOK_MESSAGE_GROUPS, "shopee": SHOPEE_MESSAGE_GROUPS, "mercado": MERCADO_MESSAGE_GROUPS}[platform]
        title = f"{store_name} {platform} 今天数据 - {business_date(collected_at)}（巴西）"
        lines: list[str] = []
        for group_name, metrics in groups:
            available = [(label, field) for label, field in metrics if field in values]
            if not available:
                continue
            lines.append(f"### {group_name}")
            if platform == "shopee":
                lines.extend(["| 指标 | 今天 |", "|---|---:|"])
                lines.extend(f"| {label} | {display_metric(field, values[field], metadata.get(field, {}))} |" for label, field in available)
            else:
                lines.extend(f"- **{label}**：{display_metric(field, values[field], metadata.get(field, {}))}" for label, field in available)
            lines.append("")
        return title, "\n".join(lines).strip() or "今天暂无有效数据。"

    @staticmethod
    def _format_tiktok_notification(store_name: str, rows: list[dict[str, Any]], platform_config: dict[str, Any] | None = None) -> tuple[str, str]:
        return DailyStoreCheck._format_today_notification("tiktok", store_name, rows)

    @staticmethod
    def _format_shopee_notification(store_name: str, rows: list[dict[str, Any]], platform_config: dict[str, Any] | None = None) -> tuple[str, str]:
        return DailyStoreCheck._format_today_notification("shopee", store_name, rows)

    @staticmethod
    def _format_mercado_notification(store_name: str, rows: list[dict[str, Any]], platform_config: dict[str, Any] | None = None) -> tuple[str, str]:
        return DailyStoreCheck._format_today_notification("mercado", store_name, rows)

    @staticmethod
    def _format_mercado_display_value(field_name: str, value: Any) -> str:
        """把美客多字段转换成机器人可读文本：金额为 BRL，比例为百分比，计数为整数。"""
        if value in (None, ""):
            return "-"

        text = str(value).strip()
        if field_name in MERCADO_PROGRESS_FIELDS:
            return DailyStoreCheck._format_percent_text(value, platform_log_name="MKD")

        if field_name in MERCADO_CURRENCY_FIELDS:
            cleaned = text.upper().replace("BRL", "").replace("R$", "").replace(" ", "")
            try:
                # 巴西格式如 18.558,26；同时兼容爬虫内部的 18558.26。
                if "," in cleaned and "." in cleaned:
                    cleaned = cleaned.replace(".", "").replace(",", ".")
                elif "." in cleaned:
                    # 只有点号且点后为 3 位时是千位分隔，例如 R$ 3.522 应显示为 R$3522.00。
                    parts = cleaned.split(".")
                    if len(parts) > 1 and len(parts[0]) <= 3 and all(len(part) == 3 for part in parts[1:]):
                        cleaned = "".join(parts)
                else:
                    cleaned = cleaned.replace(",", ".")
                return f"R${float(cleaned):.2f}"
            except (TypeError, ValueError):
                return text

        if field_name in MERCADO_INTEGER_FIELDS:
            cleaned = text.replace(" ", "")
            try:
                # 1,400 是计数字段的千位分隔，不能按小数 1.4 处理。
                if "," in cleaned and "." not in cleaned and len(cleaned.rsplit(",", 1)[-1]) == 3:
                    cleaned = cleaned.replace(",", "")
                elif "." in cleaned and "," not in cleaned:
                    # 巴西计数字段也可能使用点号千位分隔，例如 3.522。
                    parts = cleaned.split(".")
                    if len(parts) > 1 and len(parts[0]) <= 3 and all(len(part) == 3 for part in parts[1:]):
                        cleaned = "".join(parts)
                else:
                    cleaned = cleaned.replace(",", ".")
                return str(int(float(cleaned)))
            except (TypeError, ValueError):
                return text

        return text

    @staticmethod
    def _format_shopee_display_value(field_name: str, value: Any) -> str:
        """补充 Shopee 机器人字段的货币、百分比和小数格式。"""
        if value in (None, ""):
            return "-"
        text = str(value).strip()
        if field_name in SHOPEE_PERCENT_TEXT_FIELDS:
            return DailyStoreCheck._format_percent_text(value, platform_log_name="SP")
        if field_name in SHOPEE_CURRENCY_FIELDS:
            if text.upper().startswith("R$"):
                return text
            try:
                return f"R${float(value):.2f}"
            except (TypeError, ValueError):
                return text
        if field_name in SHOPEE_INTEGER_FIELDS:
            try:
                return str(int(float(value)))
            except (TypeError, ValueError):
                return text
        if "广告支出回报率" in field_name:
            try:
                return f"{float(value):.2f}"
            except (TypeError, ValueError):
                return text
        return text

    @staticmethod
    def _format_tiktok_display_value(field_name: str, value: Any, raw_text: str = "") -> str:
        """把 TK 数值格式化为机器人和人工可读文本，并根据原始值保留 R$/USD 符号。"""
        if value in (None, ""):
            return "-"
        if isinstance(value, str) and "%" in value:
            return value
        is_currency = any(token in field_name for token in ("成本", "均单价", "总收入", "GMV"))
        if is_currency:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return str(value)
            raw_upper = raw_text.upper()
            symbol = "R$" if ("R$" in raw_text or "BRL" in raw_upper) else "$" if ("$" in raw_text or "USD" in raw_upper) else ""
            return f"{symbol}{number:.2f}" if symbol else f"{number:.2f}"
        if "ROI" in field_name:
            try:
                return f"{float(value):.2f}"
            except (TypeError, ValueError):
                return str(value)
        if isinstance(value, bool):
            return str(value)
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)

    @staticmethod
    def _format_rows(rows: list[dict[str, Any]]) -> str:
        """生成适合机器人阅读的短文本，避免把大段原始数据直接推送。"""
        if not rows:
            return "本次没有采集到数据。"
        if any(row.get("平台") == "tiktok" for row in rows):
            _, message_body = DailyStoreCheck._format_tiktok_notification(
                str(rows[0].get("店铺名") or "TikTok"),
                rows,
            )
            return message_body
        if any(row.get("平台") == "mercado" for row in rows):
            _, message_body = DailyStoreCheck._format_mercado_notification(
                str(rows[0].get("店铺名") or "Mercado"),
                rows,
            )
            return message_body
        if any(row.get("平台") == "shopee" for row in rows):
            _, message_body = DailyStoreCheck._format_shopee_notification(
                str(rows[0].get("店铺名") or "Shopee"),
                rows,
            )
            return message_body
        lines = []
        for row in rows[:20]:
            platform_fields = row.get("飞书字段", {})
            if isinstance(platform_fields, dict):
                for field_name, value in platform_fields.items():
                    if value != "":
                        lines.append(f"{field_name}: {value}")
                continue
            value = str(row.get("数值", ""))
            lines.append(f"{row.get('指标', '')}: {value}")
        if not lines:
            return "本次未抓取到有效指标，空值已写入飞书。"
        if len(rows) > 20:
            lines.append(f"其余 {len(rows) - 20} 条已写入飞书。")
        return "\n".join(lines)
