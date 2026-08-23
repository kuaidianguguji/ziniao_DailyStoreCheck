"""每日店铺任务编排器。"""

from __future__ import annotations

import importlib
import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

from .config import StoreTask, normalise_platform
from .deepseek_client import DeepSeekClient
from .feishu_client import FeishuClient
from .ziniao_client import ZiniaoClient, ZiniaoStoreCloseError, ZiniaoStoreSession

LOGGER = logging.getLogger(__name__)


# TikTok 多维表的固定字段顺序，与用户建立的33个字段完全一致。
# 历史电子表使用下面单独定义的 TIKTOK_SPREADSHEET_FIELD_ORDER。
TIKTOK_TABLE_FIELD_ORDER: tuple[str, ...] = (
    "店铺名",
    "直播GMV",
    "短视频GMV",
    "7天均单价",
    "昨天ROI",
    "昨天订单数",
    "昨天GMV视频比",
    "7天订单数",
    "昨天总收入",
    "昨天SKU订单数",
    "7天GMV",
    "昨天商品访客数",
    "昨天曝光数",
    "7天成交件数",
    "7天曝光数",
    "7天成本",
    "昨天GMV直播比",
    "采集时间",
    "昨天GMV商品卡比",
    "7天总收入",
    "昨天成本",
    "7天GMV直播比",
    "商品卡GMV",
    "昨天客户数",
    "昨天去重曝光数",
    "7天客户数",
    "7天ROI",
    "昨天GMV",
    "7天商品访客数",
    "7天去重曝光数",
    "昨天均单价",
    "昨天成交件数",
    "7天SKU订单数",
)

# 飞书多维表的公式字段由飞书自己计算，调用新增记录接口时不能赋值。
# 这些字段的爬虫原始结果仍会写入历史电子表和机器人消息，方便核对公式。
TIKTOK_FORMULA_FIELDS: frozenset[str] = frozenset(
    {
        "昨天ROI",
        "昨天GMV视频比",
        "昨天GMV直播比",
        "昨天GMV商品卡比",
        "7天GMV直播比",
        "7天ROI",
    }
)

# TikTok 历史电子表专用顺序：昨天广告、7天广告、昨天概览、7天概览。
# 与多维表原有顺序分开维护，避免机器人和电子表受字段插入顺序影响。
TIKTOK_SPREADSHEET_FIELD_ORDER: tuple[str, ...] = (
    "店铺名", "昨天成本", "昨天SKU订单数", "昨天均单价", "昨天总收入", "昨天ROI",
    "7天成本", "7天SKU订单数", "7天均单价", "7天总收入", "7天ROI",
    "昨天GMV", "昨天成交件数", "昨天订单数", "昨天客户数", "昨天商品访客数", "昨天曝光数", "昨天去重曝光数",
    "直播GMV", "短视频GMV", "商品卡GMV", "昨天GMV直播比", "昨天GMV视频比", "昨天GMV商品卡比",
    "7天GMV", "7天成交件数", "7天订单数", "7天客户数", "7天商品访客数", "7天曝光数", "7天去重曝光数", "7天GMV直播比",
    "采集时间",
)

# 机器人消息固定使用四个分组；同名 SKU 订单数字段在不同分组中分别读取，不互相覆盖。
TIKTOK_MESSAGE_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("昨日广告数据", (("成本", "昨天成本"), ("SKU订单数", "昨天SKU订单数"), ("均单价", "昨天均单价"), ("总收入", "昨天总收入"), ("ROI", "昨天ROI"))),
    ("最近7天广告数据", (("成本", "7天成本"), ("SKU订单数", "7天SKU订单数"), ("均单价", "7天均单价"), ("总收入", "7天总收入"), ("ROI", "7天ROI"))),
    ("昨日概览数据", (("GMV", "昨天GMV"), ("成交件数", "昨天成交件数"), ("SKU订单数", "昨天SKU订单数"), ("订单数", "昨天订单数"), ("客户数", "昨天客户数"), ("商品访客数", "昨天商品访客数"), ("曝光数", "昨天曝光数"), ("去重曝光数", "昨天去重曝光数"), ("直播GMV", "直播GMV"), ("短视频GMV", "短视频GMV"), ("商品卡GMV", "商品卡GMV"))),
    ("最近7天概览数据", (("GMV", "7天GMV"), ("成交件数", "7天成交件数"), ("SKU订单数", "7天SKU订单数"), ("订单数", "7天订单数"), ("客户数", "7天客户数"), ("商品访客数", "7天商品访客数"), ("曝光数", "7天曝光数"), ("去重曝光数", "7天去重曝光数"), ("直播GMV", "_7天直播GMV"), ("短视频GMV", "_7天短视频GMV"), ("商品卡GMV", "_7天商品卡GMV"))),
)


# Shopee 多维表和历史电子表的固定 26 字段顺序，严格采用用户提供的表格顺序。
# 除店铺名和采集时间外，其余 24 个字段全部来自 SP_auto.py 的指标结果。
SHOPEE_TABLE_FIELD_ORDER: tuple[str, ...] = (
    "店铺名",
    "7天ALL销售额",
    "昨天ALL广告支出回报率",
    "7天ALL订单量",
    "7天ALL广告支出回报率",
    "7天ALL优惠劵带来销售额",
    "昨天ALL优惠价金额",
    "7天ALL优惠价金额",
    "7天ALL商品已出售",
    "昨天ALL点击数",
    "昨天ALL订单量",
    "7天ALL点击数",
    "昨天ALL加购率",
    "7天ALL点击率",
    "昨天ALL商品已出售",
    "7天ALL展示次数",
    "昨天ALL花费",
    "昨天ALL优惠劵带来销售额",
    "7天ALL加购次数",
    "昨天ALL展示次数",
    "昨天ALL加购次数",
    "7天ALL加购率",
    "采集时间",
    "昨天ALL销售额",
    "7天ALL花费",
    "昨天ALL点击率",
)

# Shopee 只有点击率和加购率属于百分比；广告支出回报率是普通倍数，不能追加百分号。
# SP_auto.py 内部用 0.033 表示 3.3%，写入飞书多维表和电子表时再转换成 "3.3%" 文本。
SHOPEE_PERCENT_TEXT_FIELDS: frozenset[str] = frozenset(
    {
        "昨天ALL点击率",
        "7天ALL点击率",
        "昨天ALL加购率",
        "7天ALL加购率",
    }
)

# Shopee 历史电子表使用便于人工阅读的业务顺序：店铺名、昨日12项、最近7天12项、采集时间。
# 多维表仍使用 SHOPEE_TABLE_FIELD_ORDER 中的真实字段名，不受电子表列顺序调整影响。
SHOPEE_SPREADSHEET_FIELD_ORDER: tuple[str, ...] = (
    "店铺名",
    "昨天ALL展示次数",
    "昨天ALL点击数",
    "昨天ALL点击率",
    "昨天ALL订单量",
    "昨天ALL商品已出售",
    "昨天ALL销售额",
    "昨天ALL花费",
    "昨天ALL广告支出回报率",
    "昨天ALL优惠价金额",
    "昨天ALL优惠劵带来销售额",
    "昨天ALL加购次数",
    "昨天ALL加购率",
    "7天ALL展示次数",
    "7天ALL点击数",
    "7天ALL点击率",
    "7天ALL订单量",
    "7天ALL商品已出售",
    "7天ALL销售额",
    "7天ALL花费",
    "7天ALL广告支出回报率",
    "7天ALL优惠价金额",
    "7天ALL优惠劵带来销售额",
    "7天ALL加购次数",
    "7天ALL加购率",
    "采集时间",
)

# 机器人 Markdown 与电子表使用相同的指标先后顺序；展示标签去掉“昨天/7天/ALL”前缀。
SHOPEE_MESSAGE_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "昨日广告数据",
        (
            ("展示次数", "昨天ALL展示次数"),
            ("点击数", "昨天ALL点击数"),
            ("点击率", "昨天ALL点击率"),
            ("订单量", "昨天ALL订单量"),
            ("商品已出售", "昨天ALL商品已出售"),
            ("销售额", "昨天ALL销售额"),
            ("花费", "昨天ALL花费"),
            ("广告支出回报率", "昨天ALL广告支出回报率"),
            ("优惠价金额", "昨天ALL优惠价金额"),
            ("优惠劵带来销售额", "昨天ALL优惠劵带来销售额"),
            ("加购次数", "昨天ALL加购次数"),
            ("加购率", "昨天ALL加购率"),
        ),
    ),
    (
        "最近7天广告数据",
        (
            ("展示次数", "7天ALL展示次数"),
            ("点击数", "7天ALL点击数"),
            ("点击率", "7天ALL点击率"),
            ("订单量", "7天ALL订单量"),
            ("商品已出售", "7天ALL商品已出售"),
            ("销售额", "7天ALL销售额"),
            ("花费", "7天ALL花费"),
            ("广告支出回报率", "7天ALL广告支出回报率"),
            ("优惠价金额", "7天ALL优惠价金额"),
            ("优惠劵带来销售额", "7天ALL优惠劵带来销售额"),
            ("加购次数", "7天ALL加购次数"),
            ("加购率", "7天ALL加购率"),
        ),
    ),
)

# 当爬虫没有提供“显示值”时，机器人根据字段类别补充货币符号或合适的小数位。
SHOPEE_CURRENCY_FIELDS: frozenset[str] = frozenset(
    field_name
    for field_name in SHOPEE_TABLE_FIELD_ORDER
    if any(keyword in field_name for keyword in ("销售额", "花费", "优惠价金额"))
)
SHOPEE_INTEGER_FIELDS: frozenset[str] = frozenset(
    field_name
    for field_name in SHOPEE_TABLE_FIELD_ORDER
    if any(keyword in field_name for keyword in ("展示次数", "点击数", "订单量", "商品已出售", "加购次数"))
)


# 美客多多维表和历史电子表的固定 32 字段顺序。
# 多维表只接收这些已建立字段，避免通用“指标/数值/原始数据”导致 WrongRequestBody。
MERCADO_TABLE_FIELD_ORDER: tuple[str, ...] = (
    "7天取消的销售数量",
    "30天取消的销售价值",
    "7天转换率",
    "30天已售件数",
    "30天购买意向",
    "7天意向购买转换率",
    "30天退货价值",
    "7天已售件数",
    "7天退货价值",
    "7天平均单价",
    "店铺名",
    "7天总销售额",
    "7天独立意向转换率",
    "7天独特的参观",
    "7天取消的销售价值",
    "7天购买意向",
    "7天销售量",
    "30天意向购买转换率",
    "7天退货数量",
    "30天访问",
    "30天总销售额",
    "30天独特的参观",
    "7天访问",
    "30天取消的销售数量",
    "30天独立意向转换率",
    "采集时间",
    "30天转换率",
    "30天退货数量",
    "30天平均单价",
    "30天销售量",
    "7天总转换率",
    "30天总转换率",
)

# 美客多爬虫内部用 0.333 表示 33.3%，写入飞书多维表、电子表和机器人消息时再转成百分比文本。
# 飞书端这些字段当前均为文本类型，所以传输值必须是 "33.3%" 字符串，不能直接发送 0.333 浮点数。
MERCADO_PROGRESS_FIELDS: frozenset[str] = frozenset(
    {
        "7天转换率",
        "7天意向购买转换率",
        "7天独立意向转换率",
        "30天意向购买转换率",
        "30天独立意向转换率",
        "30天转换率",
        "7天总转换率",
        "30天总转换率",
    }
)


class DailyStoreCheck:
    """控制表 -> 紫鸟单店铺 -> 平台爬虫 -> 飞书写入/推送的主流程。"""

    def __init__(self, config: dict[str, Any]):
        """创建飞书和紫鸟客户端，并准备当前轮次的店铺缓存。"""
        self.config = config
        self.feishu = FeishuClient(config)
        self.deepseek = DeepSeekClient(config)
        self.ziniao = ZiniaoClient(config)
        self.browser_list: list[dict[str, Any]] = []

    def run_once(self) -> None:
        """执行一轮任务；店铺永远按控制表顺序串行处理。"""
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
            close_failed = False
            for task in tasks:
                # 从即将打开当前店铺开始计时，直到爬取、飞书处理和店铺关闭全部执行完才停止。
                store_started_at = time.perf_counter()
                try:
                    store_info = self._run_store(task)
                finally:
                    elapsed_seconds = time.perf_counter() - store_started_at
                    timing_key = self._build_store_timing_key(store_processing_times, task.store_name)
                    store_processing_times[timing_key] = {
                        "平台": task.platform,
                        "耗时秒": round(elapsed_seconds, 2),
                        "耗时": self._format_elapsed_time(elapsed_seconds),
                    }
                ALL_info.append(store_info)
                if store_info.get("中止后续店铺"):
                    LOGGER.critical("店铺 %s 未确认关闭，本轮不再打开后续店铺", task.store_name)
                    close_failed = True
                    break
            if close_failed:
                LOGGER.warning("本轮因店铺未确认关闭而提前结束，跳过90天数据清理并立即进入最终收尾")
            else:
                self._cleanup_retention()
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

    def _run_store(self, task: StoreTask) -> dict[str, Any]:
        """处理一间店铺并返回 ALL_info 项；context manager 确保店铺串行关闭。"""
        store_info: dict[str, Any] = {
            "店铺名": task.store_name,
            "平台": task.platform,
            "状态": "未执行",
            "采集时间": "",
            "数据": {},
        }
        identifier = task.browser_oauth or task.browser_id or self._find_browser_identifier(task.store_name, task.platform)
        if not identifier:
            LOGGER.error("找不到店铺 %s 对应的紫鸟 browserOauth/browserId，跳过", task.store_name)
            error_message = "没有找到紫鸟店铺标识，请检查店铺名是否与紫鸟一致。"
            store_info["状态"] = "失败"
            store_info["错误"] = error_message
            self._safe_notify(task.recipient, f"{task.store_name} 数据任务失败", error_message)
            return store_info

        try:
            with ZiniaoStoreSession(self.ziniao, identifier, task.store_name) as session:
                crawler = self._load_crawler(task.platform)
                rows = crawler.collect(task.store_name, session.download_path, session.opened.get("debuggingPort"))
                collected_at, metric_values = self._extract_all_info_values(rows)
                store_info["采集时间"] = collected_at
                store_info["数据"] = metric_values
                self._write_feishu(task, rows)
                if task.platform == "tiktok":
                    message_title, message_body = self._format_tiktok_notification(task.store_name, rows)
                    self._safe_notify_markdown(task.recipient, message_title, message_body)
                elif task.platform == "shopee":
                    message_title, message_body = self._format_shopee_notification(task.store_name, rows)
                    self._safe_notify_markdown(task.recipient, message_title, message_body)
                else:
                    self._safe_notify(task.recipient, f"{task.store_name} {task.platform} 广告数据", self._format_rows(rows))
                store_info["状态"] = "成功"
        except ZiniaoStoreCloseError as exc:
            LOGGER.exception("店铺 %s 未能关闭，必须中止后续店铺", task.store_name)
            store_info["状态"] = "失败"
            store_info["错误"] = str(exc)
            store_info["中止后续店铺"] = True
            self._safe_notify(task.recipient, f"{task.store_name} 关闭失败", f"{exc}\n为避免同时打开多个店铺，已中止本轮后续店铺。")
        except Exception as exc:
            LOGGER.exception("店铺 %s 处理失败", task.store_name)
            store_info["状态"] = "失败"
            store_info["错误"] = str(exc)
            self._safe_notify(task.recipient, f"{task.store_name} 数据任务失败", str(exc))
        return store_info

    @staticmethod
    def _extract_all_info_values(rows: list[dict[str, Any]]) -> tuple[Any, dict[str, Any]]:
        """从平台爬虫结果提取采集时间和全部指标，供 ALL_info 保存。"""
        collected_at: Any = ""
        metric_values: dict[str, Any] = {}
        for row in rows:
            if not collected_at and row.get("采集时间"):
                collected_at = row["采集时间"]

            # Shopee 当前使用“一项指标一行”的通用结果结构，空值也保留在汇总中便于排错。
            metric_name = str(row.get("指标") or "").strip()
            if metric_name:
                metric_values[metric_name] = row.get("显示值", row.get("数值", ""))

            # TikTok 和美客多使用“飞书字段”字典；后写入的同名字段覆盖前一模块。
            platform_fields = row.get("飞书字段", {})
            if isinstance(platform_fields, dict):
                metric_values.update(platform_fields)
        return collected_at, metric_values

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
            self._safe_notify_markdown(receive_id, "DeepSeek全部店铺数据分析", analysis_text)

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

    def _safe_notify_markdown(self, recipient: str, title: str, content: str) -> None:
        """以 interactive Markdown 卡片发送 DeepSeek 汇总，失败时不影响任务收尾。"""
        try:
            LOGGER.info(
                "[飞书][Markdown机器人消息打包] recipient=%s，title=%r，markdown=%r",
                recipient or "<空接收人>",
                title,
                content,
            )
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
        platform_config = self.config.get("platforms", {}).get(platform, {})
        return crawler_class(platform_config)

    def _write_feishu(self, task: StoreTask, rows: list[dict[str, Any]]) -> None:
        """把标准行写入对应多维表，并追加到对应历史电子表。"""
        feishu_cfg = self.config.get("feishu", {})
        app_token, table_id = self.feishu.get_bitable_ref("data", task.platform)
        if task.platform == "tiktok":
            record_rows, spreadsheet_rows = self._build_tiktok_feishu_rows(task, rows)
        elif task.platform == "shopee":
            record_rows, spreadsheet_rows = self._build_shopee_feishu_rows(task, rows)
        elif task.platform == "mercado":
            record_rows, spreadsheet_rows = self._build_mercado_feishu_rows(task, rows)
        else:
            record_rows, spreadsheet_rows = self._build_default_feishu_rows(task, rows)

        LOGGER.info(
            "[飞书][传输准备] 店铺=%s，平台=%s，多维表记录数=%s，电子表行数=%s",
            task.store_name,
            task.platform,
            len(record_rows),
            len(spreadsheet_rows),
        )
        write_errors: list[str] = []

        # 多维表和历史电子表是两个独立目标；一个失败时仍然尝试另一个，避免采集数据全部丢失。
        try:
            bitable_rows = record_rows
            if task.platform == "shopee":
                bitable_rows = self._align_shopee_bitable_fields(record_rows, app_token, table_id)
            self.feishu.batch_create_records(table_id, bitable_rows, app_token=app_token)
        except Exception as exc:
            LOGGER.exception("[飞书][多维表写入失败] 店铺=%s，平台=%s", task.store_name, task.platform)
            write_errors.append(f"多维表写入失败: {exc}")

        try:
            spreadsheet_cfg = feishu_cfg.get("spreadsheets", {}).get(task.platform, "")
            if isinstance(spreadsheet_cfg, dict):
                token = str(spreadsheet_cfg.get("token") or spreadsheet_cfg.get("spreadsheet_token") or "")
                sheet_id = str(spreadsheet_cfg.get("sheet_id") or "")
                range_end_by_platform = {"tiktok": "AG", "shopee": "Z", "mercado": "AF"}
                range_end = range_end_by_platform.get(task.platform, "Z")
                range_name = str(spreadsheet_cfg.get("range") or (f"{sheet_id}!A:{range_end}" if sheet_id else f"Sheet1!A:{range_end}"))
                self.feishu.append_spreadsheet_rows(token, spreadsheet_rows, range_name)
            else:
                self.feishu.append_spreadsheet_rows(str(spreadsheet_cfg), spreadsheet_rows)
        except Exception as exc:
            LOGGER.exception("[飞书][电子表写入失败] 店铺=%s，平台=%s", task.store_name, task.platform)
            write_errors.append(f"电子表写入失败: {exc}")

        if write_errors:
            raise RuntimeError("；".join(write_errors))

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

    def _build_tiktok_feishu_rows(
        self,
        task: StoreTask,
        rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[list[Any]]]:
        """把 TikTok 广告和概览结果合并成一条 33 字段记录。"""
        merged_fields: dict[str, Any] = {}
        collected_at: Any = ""
        for row_index, row in enumerate(rows, start=1):
            if not collected_at and row.get("采集时间"):
                collected_at = row["采集时间"]
            platform_fields = row.get("飞书字段", {})
            if not isinstance(platform_fields, dict):
                LOGGER.warning("[飞书][TK打包] 第 %s 条爬虫结果的 飞书字段 不是字典，已跳过", row_index)
                continue
            for field_name, value in platform_fields.items():
                previous_value = merged_fields.get(field_name, "")
                if previous_value not in ("", None) and value not in ("", None) and previous_value != value:
                    LOGGER.warning(
                        "[飞书][TK字段冲突] 字段=%s，前一模块值=%r，后一模块值=%r；采用后一模块值",
                        field_name,
                        previous_value,
                        value,
                    )
                if value not in ("", None) or field_name not in merged_fields:
                    merged_fields[field_name] = value

        if not collected_at:
            collected_at = datetime.now(timezone.utc).isoformat()

        known_fields = set(TIKTOK_TABLE_FIELD_ORDER)
        unknown_fields = sorted(set(merged_fields) - known_fields)
        if unknown_fields:
            LOGGER.warning("[飞书][TK未知字段] 以下字段不在 33 字段定义中，不写入多维表：%s", unknown_fields)

        formula_values = {
            field_name: merged_fields[field_name]
            for field_name in TIKTOK_FORMULA_FIELDS
            if merged_fields.get(field_name) not in ("", None)
        }
        if formula_values:
            LOGGER.info("[飞书][TK公式字段] 公式字段只读，不写入多维表；抓取值仍保留到电子表和机器人：%s", formula_values)

        # 多维表只发送店铺名、毫秒时间戳和非空的可写业务字段。
        bitable_record: dict[str, Any] = {
            "店铺名": task.store_name,
            "采集时间": self._to_feishu_timestamp_ms(collected_at),
        }
        for field_name in TIKTOK_TABLE_FIELD_ORDER:
            if field_name in {"店铺名", "采集时间"} or field_name in TIKTOK_FORMULA_FIELDS:
                continue
            value = merged_fields.get(field_name, "")
            if value not in ("", None):
                bitable_record[field_name] = value

        missing_fields = [
            field_name
            for field_name in TIKTOK_TABLE_FIELD_ORDER
            if field_name not in {"店铺名", "采集时间"}
            and field_name not in TIKTOK_FORMULA_FIELDS
            and merged_fields.get(field_name, "") in ("", None)
        ]
        if missing_fields:
            LOGGER.warning("[飞书][TK空字段] 以下可写字段本次无数据，不放入多维表请求体：%s", missing_fields)

        # 历史电子表保留完整 33 列。公式字段使用本次爬虫读到的值，未抓到的字段写空单元格。
        spreadsheet_values = dict(merged_fields)
        spreadsheet_values["店铺名"] = task.store_name
        spreadsheet_values["采集时间"] = collected_at
        spreadsheet_row = [spreadsheet_values.get(field_name, "") for field_name in TIKTOK_SPREADSHEET_FIELD_ORDER]
        LOGGER.info("[飞书][TK电子表顺序] 字段顺序=%s，行数据=%s", TIKTOK_SPREADSHEET_FIELD_ORDER, spreadsheet_row)
        return [bitable_record], [spreadsheet_row]

    def _build_shopee_feishu_rows(
        self,
        task: StoreTask,
        rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[list[Any]]]:
        """把 Shopee 的 24 项指标合并成一条严格匹配 26 字段表结构的记录。"""
        merged_fields: dict[str, Any] = {}
        collected_at: Any = ""
        known_fields = set(SHOPEE_TABLE_FIELD_ORDER)

        for row_index, row in enumerate(rows, start=1):
            if not collected_at and row.get("采集时间"):
                collected_at = row["采集时间"]

            # SP_auto.py 当前按“一项指标一行”返回，通过“指标”和“数值”合并。
            metric_name = str(row.get("指标") or "").strip()
            if metric_name:
                if metric_name in known_fields:
                    value = row.get("数值", "")
                    if value not in ("", None) or metric_name not in merged_fields:
                        merged_fields[metric_name] = value
                else:
                    LOGGER.warning("[飞书][SP未知字段] 第 %s 条指标=%s 不在 26 字段定义中，已忽略", row_index, metric_name)

            # 同时兼容以后 SP_auto.py 直接返回“飞书字段”字典的写法。
            platform_fields = row.get("飞书字段", {})
            if not isinstance(platform_fields, dict):
                LOGGER.warning("[飞书][SP打包] 第 %s 条爬虫结果的 飞书字段 不是字典，已忽略该属性", row_index)
                continue
            for field_name, value in platform_fields.items():
                if field_name not in known_fields:
                    LOGGER.warning("[飞书][SP未知字段] 字段=%s 不在 26 字段定义中，已忽略", field_name)
                    continue
                if value not in ("", None) or field_name not in merged_fields:
                    merged_fields[field_name] = value

        if not collected_at:
            collected_at = datetime.now(timezone.utc).isoformat()

        # 飞书金额和数字字段不能接收空字符串，因此只把非空业务指标加入请求 JSON。
        # 点击率和加购率在飞书端是文本字段，发送前统一转换成 "3.3%" 字符串。
        bitable_record: dict[str, Any] = {
            "店铺名": task.store_name,
            "采集时间": self._to_feishu_timestamp_ms(collected_at),
        }
        for field_name in SHOPEE_TABLE_FIELD_ORDER:
            if field_name in {"店铺名", "采集时间"}:
                continue
            value = merged_fields.get(field_name, "")
            if value not in ("", None):
                if field_name in SHOPEE_PERCENT_TEXT_FIELDS:
                    value = self._format_percent_text(value, platform_log_name="SP")
                bitable_record[field_name] = value

        missing_fields = [
            field_name
            for field_name in SHOPEE_TABLE_FIELD_ORDER
            if field_name not in {"店铺名", "采集时间"}
            and merged_fields.get(field_name, "") in ("", None)
        ]
        if missing_fields:
            LOGGER.warning("[飞书][SP空字段] 以下字段本次无数据，不放入多维表请求体：%s", missing_fields)

        # 历史电子表固定保留 26 列，按“昨日数据 -> 7天数据”排列；空指标写空单元格。
        spreadsheet_values = dict(merged_fields)
        spreadsheet_values["店铺名"] = task.store_name
        spreadsheet_values["采集时间"] = collected_at
        for field_name in SHOPEE_PERCENT_TEXT_FIELDS:
            value = spreadsheet_values.get(field_name, "")
            if value not in ("", None):
                spreadsheet_values[field_name] = self._format_percent_text(value, platform_log_name="SP")
        spreadsheet_row = [spreadsheet_values.get(field_name, "") for field_name in SHOPEE_SPREADSHEET_FIELD_ORDER]
        LOGGER.info("[飞书][SP电子表顺序] 字段顺序=%s，行数据=%s", SHOPEE_SPREADSHEET_FIELD_ORDER, spreadsheet_row)
        return [bitable_record], [spreadsheet_row]

    def _build_mercado_feishu_rows(
        self,
        task: StoreTask,
        rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[list[Any]]]:
        """把美客多爬虫结果转换成一条严格匹配 32 字段表结构的记录。"""
        merged_fields: dict[str, Any] = {}
        collected_at: Any = ""
        for row_index, row in enumerate(rows, start=1):
            if not collected_at and row.get("采集时间"):
                collected_at = row["采集时间"]
            platform_fields = row.get("飞书字段", {})
            if not isinstance(platform_fields, dict):
                LOGGER.warning("[飞书][MKD打包] 第 %s 条爬虫结果的 飞书字段 不是字典，已跳过", row_index)
                continue
            for field_name, value in platform_fields.items():
                if value not in ("", None) or field_name not in merged_fields:
                    merged_fields[field_name] = value

        if not collected_at:
            collected_at = datetime.now(timezone.utc).isoformat()

        known_fields = set(MERCADO_TABLE_FIELD_ORDER)
        unknown_fields = sorted(set(merged_fields) - known_fields)
        if unknown_fields:
            LOGGER.warning("[飞书][MKD未知字段] 以下字段不在 32 字段定义中，不写入多维表：%s", unknown_fields)

        # 数字和货币字段不能发送空字符串；空指标直接从 JSON 中省略。
        # 转换率字段在飞书端已经改成文本类型，因此发送前统一转换成 "33.3%" 字符串。
        bitable_record: dict[str, Any] = {
            "店铺名": task.store_name,
            "采集时间": self._to_feishu_timestamp_ms(collected_at),
        }
        for field_name in MERCADO_TABLE_FIELD_ORDER:
            if field_name in {"店铺名", "采集时间"}:
                continue
            value = merged_fields.get(field_name, "")
            if value not in ("", None):
                if field_name in MERCADO_PROGRESS_FIELDS:
                    value = self._format_percent_text(value, platform_log_name="MKD")
                bitable_record[field_name] = value

        missing_fields = [
            field_name
            for field_name in MERCADO_TABLE_FIELD_ORDER
            if field_name not in {"店铺名", "采集时间"}
            and merged_fields.get(field_name, "") in ("", None)
        ]
        if missing_fields:
            LOGGER.warning("[飞书][MKD空字段] 以下字段本次无数据，不放入多维表请求体：%s", missing_fields)

        # 电子表固定追加 32 列，并保留 ISO 采集时间方便人工查看。
        spreadsheet_values = dict(merged_fields)
        spreadsheet_values["店铺名"] = task.store_name
        spreadsheet_values["采集时间"] = collected_at
        for field_name in MERCADO_PROGRESS_FIELDS:
            value = spreadsheet_values.get(field_name, "")
            if value not in ("", None):
                spreadsheet_values[field_name] = self._format_percent_text(value, platform_log_name="MKD")
        spreadsheet_row = [spreadsheet_values.get(field_name, "") for field_name in MERCADO_TABLE_FIELD_ORDER]
        return [bitable_record], [spreadsheet_row]

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

    @staticmethod
    def _format_tiktok_notification(store_name: str, rows: list[dict[str, Any]]) -> tuple[str, str]:
        """整理 TK 机器人 Markdown 标题和正文，固定按四个业务分组输出。"""
        modules: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        collected_at = ""
        for row in rows:
            collected_at = collected_at or str(row.get("采集时间") or "")
            module_name = "概览" if "概览" in str(row.get("指标") or "") else "广告"
            fields = row.get("飞书字段", {})
            fields = dict(fields) if isinstance(fields, dict) else {}
            raw_values: dict[str, Any] = {}
            try:
                parsed_raw = json.loads(str(row.get("原始数据") or "{}"))
                if isinstance(parsed_raw, dict):
                    raw_values = parsed_raw
            except (TypeError, ValueError, json.JSONDecodeError):
                LOGGER.warning("[飞书][TK消息原始数据解析失败] 模块=%s，原始数据=%r", module_name, row.get("原始数据"))
            modules[module_name] = (fields, raw_values)

        date_text = ""
        try:
            timestamp = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
            date_text = f"{timestamp.month}月{timestamp.day}日"
        except (TypeError, ValueError):
            date_text = collected_at[:10] if collected_at else "未知日期"

        title = f"{store_name} tiktok 广告数据 - {date_text}"
        lines: list[str] = []
        for group_name, metric_specs in TIKTOK_MESSAGE_GROUPS:
            source_name = "概览" if "概览" in group_name else "广告"
            fields, raw_values = modules.get(source_name, ({}, {}))
            lines.append(f"### {group_name}")
            for label, field_name in metric_specs:
                value = fields.get(field_name, "")
                raw_text = str(raw_values.get(field_name) or "")
                lines.append(f"- **{label}**：{DailyStoreCheck._format_tiktok_display_value(field_name, value, raw_text)}")
            lines.append("")
        return title, "\n".join(lines).strip()

    @staticmethod
    def _format_shopee_notification(store_name: str, rows: list[dict[str, Any]]) -> tuple[str, str]:
        """整理 Shopee 机器人 Markdown，固定按昨日和最近7天两组输出。"""
        collected_at = ""
        display_values: dict[str, Any] = {}
        for row in rows:
            collected_at = collected_at or str(row.get("采集时间") or "")
            metric_name = str(row.get("指标") or "").strip()
            if metric_name:
                # SP_auto.py 的显示值已经包含 R$ 或 %；没有显示值时再使用内部数值补充格式。
                value = row.get("显示值", "")
                if value in ("", None):
                    value = row.get("数值", "")
                if value not in ("", None):
                    display_values[metric_name] = value

            # 兼容以后 SP_auto.py 改成一次返回“飞书字段”字典的结果结构。
            platform_fields = row.get("飞书字段", {})
            if isinstance(platform_fields, dict):
                for field_name, field_value in platform_fields.items():
                    if field_name not in display_values and field_value not in ("", None):
                        display_values[field_name] = field_value

        try:
            timestamp = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
            date_text = f"{timestamp.month}月{timestamp.day}日"
        except (TypeError, ValueError):
            date_text = collected_at[:10] if collected_at else "未知日期"

        title = f"{store_name} shopee 推送数据 - {date_text}"
        lines: list[str] = []
        for group_name, metric_specs in SHOPEE_MESSAGE_GROUPS:
            lines.append(f"### {group_name}")
            for label, field_name in metric_specs:
                value = display_values.get(field_name, "")
                lines.append(f"- **{label}**：{DailyStoreCheck._format_shopee_display_value(field_name, value)}")
            # 组间保留一个空行，防止两段数据在飞书卡片中连成一块。
            lines.append("")
        return title, "\n".join(lines).strip()

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
            merged_fields: dict[str, Any] = {}
            for row in rows:
                platform_fields = row.get("飞书字段", {})
                if isinstance(platform_fields, dict):
                    for field_name, value in platform_fields.items():
                        if value not in ("", None):
                            merged_fields[field_name] = value
            lines: list[str] = []
            for field_name in MERCADO_TABLE_FIELD_ORDER:
                if field_name not in merged_fields:
                    continue
                value = merged_fields[field_name]
                if field_name in MERCADO_PROGRESS_FIELDS and isinstance(value, (int, float)):
                    lines.append(f"{field_name}: {value * 100:.1f}%")
                else:
                    lines.append(f"{field_name}: {value}")
            return "\n".join(lines) if lines else "本次未抓取到有效指标，空值不会写入飞书数值字段。"
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
