"""Mercado Libre（美客多）店铺数据自动化和爬虫。

本文件独立维护美客多的 DrissionPage 连接、XPath、数值解析和飞书字段组装。
程序先接管紫鸟已经打开的当前标签页，确认店铺首页加载完成后，
再根据当前网址选择对应的美客多经营指标页面，不会创建普通浏览器。

下面各指标 XPath 集中维护在本文件中；当前已填写的 XPath 可以直接调整。
如果某个 XPath 为空、元素不存在或数值解析失败，该指标默认写入空值，其他指标继续执行。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any

from DrissionPage import Chromium


# 当前模块日志会由 run_daily_store_check.py 同时输出到控制台和日志文件。
LOGGER = logging.getLogger(__name__)


# 美客多首页广告弹窗的关闭按钮
HOME_AD_CLOSE_XPATH = '//button[@class="andes-modal__close-button"]'

# 紫鸟当前网址包含 vendedores 时，使用卖家后台专用的经营指标页面。
METRICS_PAGE_URL = "https://vendedores.mercadolivre.com.br/metricas/negocio/visao-geral#from=seller-menu"
# 紫鸟当前网址不包含 vendedores、为空或读取失败时，使用普通美客多域名的指标页面。
GENERAL_METRICS_PAGE_URL = "https://www.mercadolivre.com.br/metricas#sc-menu"

# 页面和按钮操作参数。重试次数 3 表示首次点击失败后再重试 3 次。
PAGE_READY_TIMEOUT_SECONDS = 60
AFTER_PAGE_READY_WAIT_SECONDS = 10
CLICK_RETRY_TIMES = 3
CLICK_RETRY_INTERVAL_SECONDS = 2
NEXT_ELEMENT_TIMEOUT_SECONDS = 30


# 日期切换按钮及两个快捷日期选项。日期选项使用稳定的 id 片段定位。
DATE_SWITCH_BUTTON_XPATH = '(//button[@class="andes-dropdown__trigger"])[1]'
LAST_7_DAYS_OPTION_XPATH = '//li[contains(@id, "option-lastSevenDays")]'
LAST_30_DAYS_OPTION_XPATH = '//li[contains(@id, "option-lastMonth")]'


# 统计页面中的时间范围切换步骤。
# success_state="visible"：目标出现后才算当前按钮点击成功。
# success_state="hidden"：目标连续两次不可见后才算当前按钮点击成功。
PERIOD_CLICK_STEPS: dict[str, list[dict[str, Any]]] = {
    "7天": [
        {
            "name": "打开日期切换按钮（7天）",
            "xpath": DATE_SWITCH_BUTTON_XPATH,
            "wait_seconds": 1,
            "success_xpath": LAST_7_DAYS_OPTION_XPATH,
            "success_state": "visible",
            "success_name": "最近7天选项出现",
        },
        {
            "name": "选择最近7天",
            "xpath": LAST_7_DAYS_OPTION_XPATH,
            "wait_seconds": 2,
            "success_xpath": LAST_7_DAYS_OPTION_XPATH,
            "success_state": "hidden",
            "success_name": "最近7天选项消失",
        },
    ],
    "30天": [
        {
            "name": "打开日期切换按钮（30天）",
            "xpath": DATE_SWITCH_BUTTON_XPATH,
            "wait_seconds": 1,
            "success_xpath": LAST_30_DAYS_OPTION_XPATH,
            "success_state": "visible",
            "success_name": "最近30天选项出现",
        },
        {
            "name": "选择最近30天",
            "xpath": LAST_30_DAYS_OPTION_XPATH,
            "wait_seconds": 2,
            "success_xpath": LAST_30_DAYS_OPTION_XPATH,
            "success_state": "hidden",
            "success_name": "最近30天选项消失",
        },
    ],
}


# 每个指标单独配置 XPath 和数据类型。字段名必须与美客多飞书 32 字段保持一致。
# kind 可选：currency=货币、integer=整数、percent=百分数。
# currency_code 按用户要求固定为巴西雷亚尔 BRL。
METRIC_SPECS: list[dict[str, str]] = [
    {"period": "7天", "field": "7天总销售额", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][1]//p[@class="metrics-amount-container__value"]', "kind": "currency", "currency_code": "BRL"},
    {"period": "7天", "field": "7天已售件数", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][2]//p[@class="metrics-amount-container__value"]', "kind": "integer"},
    {"period": "7天", "field": "7天平均单价", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][3]//p[@class="metrics-amount-container__value"]', "kind": "currency", "currency_code": "BRL"},
    {"period": "7天", "field": "7天访问", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][4]//p[@class="metrics-amount-container__value"]', "kind": "integer"},
    {"period": "7天", "field": "7天销售量", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][5]//p[@class="metrics-amount-container__value"]', "kind": "integer"},
    {"period": "7天", "field": "7天转换率", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][6]//p[@class="metrics-amount-container__value"]', "kind": "percent"},
    {"period": "7天", "field": "7天取消的销售数量", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][8]//p[@class="metrics-amount-container__value"]', "kind": "integer"},
    {"period": "7天", "field": "7天取消的销售价值", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][9]//p[@class="metrics-amount-container__value"]', "kind": "currency", "currency_code": "BRL"},
    {"period": "7天", "field": "7天退货数量", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][10]//p[@class="metrics-amount-container__value"]', "kind": "integer"},
    {"period": "7天", "field": "7天退货价值", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][11]//p[@class="metrics-amount-container__value"]', "kind": "currency", "currency_code": "BRL"},
    {"period": "7天", "field": "7天独特的参观", "xpath": '(//div[contains(@class,"metrics-funnel__series-circles")])[1]//span[contains(@class,"andes-typography--color-primary")]', "kind": "integer"},
    {"period": "7天", "field": "7天购买意向", "xpath": '(//div[contains(@class,"metrics-funnel__series-circles")])[2]//span[contains(@class,"andes-typography--color-primary")]', "kind": "integer"},
    {"period": "7天", "field": "7天总转换率", "xpath": '(//div[@class="metrics-funnel__pills-section"]//p)[1]', "kind": "percent"},
    {"period": "7天", "field": "7天独立意向转换率", "xpath": '(//div[@class="metrics-funnel__pills-section"]//p)[2]', "kind": "percent"},
    {"period": "7天", "field": "7天意向购买转换率", "xpath": '(//div[@class="metrics-funnel__pills-section"]//p)[3]', "kind": "percent"},
    {"period": "30天", "field": "30天总销售额", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][1]//p[@class="metrics-amount-container__value"]', "kind": "currency", "currency_code": "BRL"},
    {"period": "30天", "field": "30天已售件数", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][2]//p[@class="metrics-amount-container__value"]', "kind": "integer"},
    {"period": "30天", "field": "30天平均单价", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][3]//p[@class="metrics-amount-container__value"]', "kind": "currency", "currency_code": "BRL"},
    {"period": "30天", "field": "30天访问", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][4]//p[@class="metrics-amount-container__value"]', "kind": "integer"},
    {"period": "30天", "field": "30天销售量", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][5]//p[@class="metrics-amount-container__value"]', "kind": "integer"},
    {"period": "30天", "field": "30天转换率", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][6]//p[@class="metrics-amount-container__value"]', "kind": "percent"},
    {"period": "30天", "field": "30天取消的销售数量", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][8]//p[@class="metrics-amount-container__value"]', "kind": "integer"},
    {"period": "30天", "field": "30天取消的销售价值", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][9]//p[@class="metrics-amount-container__value"]', "kind": "currency", "currency_code": "BRL"},
    {"period": "30天", "field": "30天退货数量", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][10]//p[@class="metrics-amount-container__value"]', "kind": "integer"},
    {"period": "30天", "field": "30天退货价值", "xpath": '//div[@id="performance_summary_amount_expandible-expandable-section-content"]//div[contains(@class,"metrics-amount-container") and contains(@class,"metrics-amount-container--medium") and contains(@class,"metrics-amount-container--button")][11]//p[@class="metrics-amount-container__value"]', "kind": "currency", "currency_code": "BRL"},
    {"period": "30天", "field": "30天独特的参观", "xpath": '(//div[contains(@class,"metrics-funnel__series-circles")])[1]//span[contains(@class,"andes-typography--color-primary")]', "kind": "integer"},
    {"period": "30天", "field": "30天购买意向", "xpath": '(//div[contains(@class,"metrics-funnel__series-circles")])[2]//span[contains(@class,"andes-typography--color-primary")]', "kind": "integer"},
    {"period": "30天", "field": "30天总转换率", "xpath": '(//div[@class="metrics-funnel__pills-section"]//p)[1]', "kind": "percent"},
    {"period": "30天", "field": "30天独立意向转换率", "xpath": '(//div[@class="metrics-funnel__pills-section"]//p)[2]', "kind": "percent"},
    {"period": "30天", "field": "30天意向购买转换率", "xpath": '(//div[@class="metrics-funnel__pills-section"]//p)[3]', "kind": "percent"}
]


class MercadoAuto:
    """美客多 7 天/30 天经营指标自动化。"""

    def __init__(self, config: dict[str, Any] | None = None):
        """保存美客多独立配置；指标 XPath 直接维护在本文件的 METRIC_SPECS。"""
        self.config = config or {}

    def collect(self, store_name: str, download_path: str = "", debugging_port: int | str | None = None) -> list[dict[str, Any]]:
        """接管紫鸟当前标签页，读取 7 天和 30 天共 30 个指标。"""
        if not debugging_port:
            raise RuntimeError("紫鸟没有返回 debuggingPort，无法接管美客多店铺")

        LOGGER.info("[美客多][开始] 店铺=%s，准备接管紫鸟浏览器，debugging_port=%s", store_name, debugging_port)

        # 连接紫鸟已经打开的 Chromium，不创建普通浏览器；后续只在这个标签页中打开指标网址。
        browser = Chromium(f"127.0.0.1:{debugging_port}")
        tab = browser.latest_tab
        collected_at = datetime.now(timezone.utc).isoformat()
        feishu_fields: dict[str, Any] = {}
        raw_values: dict[str, str] = {}

        # 先确认紫鸟打开的美客多初始店铺页已经完整加载，避免登录状态尚未建立就跳转。
        if not self._wait_for_page_ready(tab, PAGE_READY_TIMEOUT_SECONDS, "紫鸟初始店铺页"):
            raise TimeoutError("美客多初始店铺页在 60 秒内未加载完成，停止本店铺采集")

        # 使用当前已登录的紫鸟标签页直接进入经营指标页；新页面必须再次加载完成后才允许操作。
        self._open_metrics_page(tab)

        # 严格按 7 天 -> 读取全部 7 天指标 -> 30 天 -> 读取全部 30 天指标执行。
        for period in ("7天", "30天"):
            LOGGER.info("[美客多][指标] 开始切换并采集时间范围=%s", period)
            # 最后一个日期选项点击后不使用仍存在的旧指标元素判断刷新状态，
            # final_next_xpath 留空会触发固定等待 30 秒，确保该时间范围的数据加载完成。
            self._run_click_steps(tab, PERIOD_CLICK_STEPS.get(period, []), final_next_xpath="")
            for spec in METRIC_SPECS:
                if spec["period"] != period:
                    continue
                field_name = spec["field"]
                xpath = spec["xpath"]
                value_kind = spec["kind"]
                currency_code = spec.get("currency_code", "")
                LOGGER.info(
                    "[美客多][指标] 准备读取：时间范围=%s，字段=%s，配置类型=%s，币种=%s，xpath=%s",
                    period,
                    field_name,
                    value_kind,
                    currency_code or "无",
                    xpath or "<空 XPath>",
                )
                raw_text = self._read_xpath(tab, xpath, field_name)
                converted_value = self._format_value(raw_text, value_kind)
                raw_values[field_name] = raw_text
                feishu_fields[field_name] = converted_value
                LOGGER.info(
                    "[美客多][指标结果] 字段=%s，原始值=%r，原始类型=%s，转换值=%r，转换后类型=%s，配置类型=%s，币种=%s",
                    field_name,
                    raw_text,
                    type(raw_text).__name__,
                    converted_value,
                    type(converted_value).__name__,
                    value_kind,
                    currency_code or "无",
                )
                if raw_text == "":
                    LOGGER.warning("[美客多][指标失败] 字段=%s，XPath 未抓到有效文本，最终按空值处理", field_name)
                elif converted_value == "":
                    LOGGER.warning(
                        "[美客多][转换失败] 字段=%s，已抓到原始值=%r，但无法按配置类型=%s 转换，最终按空值处理",
                        field_name,
                        raw_text,
                        value_kind,
                    )

        # “飞书字段”由 orchestrator 合并进已建立的同名多维表字段。
        # 标准字段仍保留，便于历史电子表和旧版数据表兼容。
        row = {
            "店铺名": store_name,
            "平台": "mercado",
            "采集时间": collected_at,
            "指标": "美客多7天/30天经营指标",
            "数值": "",
            "原始数据": json.dumps(raw_values, ensure_ascii=False),
            "飞书字段": feishu_fields,
        }
        valid_count = sum(value != "" for value in feishu_fields.values())
        LOGGER.info("[美客多][结果打包] row=%s", json.dumps(row, ensure_ascii=False, default=str))
        LOGGER.info("[美客多][完成] 店铺=%s，有效指标=%s/%s", store_name, valid_count, len(feishu_fields))
        return [row]

    def _open_metrics_page(self, tab: Any) -> None:
        """根据紫鸟当前网址选择经营指标页，并等待页面及日期控件完成渲染。"""
        current_url = self._read_current_url(tab)
        metrics_page_url = self._select_metrics_page_url(current_url)
        contains_vendedores = "vendedores" in current_url.casefold()
        LOGGER.info(
            "[美客多][网址判断] 当前网址=%r，是否包含vendedores=%s，选择指标页=%s",
            current_url or "<空网址>",
            "是" if contains_vendedores else "否",
            metrics_page_url,
        )
        LOGGER.info("[美客多][页面跳转] 准备在紫鸟当前标签页打开经营指标页，url=%s", metrics_page_url)
        try:
            tab.get(metrics_page_url)
        except Exception as exc:
            LOGGER.error("[美客多][页面跳转失败] 无法打开经营指标页，url=%s，异常=%s", metrics_page_url, exc)
            raise RuntimeError(f"无法打开美客多经营指标页: {exc}") from exc

        if not self._wait_for_page_ready(tab, PAGE_READY_TIMEOUT_SECONDS, "经营指标页"):
            raise TimeoutError("美客多经营指标页在 60 秒内未加载完成，停止本店铺采集")

        # document.readyState 完成后再等待 10 秒，让前端异步数据、日期控件和广告弹窗完成渲染。
        LOGGER.info("[美客多][经营指标页] 文档加载完成，额外等待 %s 秒", AFTER_PAGE_READY_WAIT_SECONDS)
        time.sleep(AFTER_PAGE_READY_WAIT_SECONDS)
        self._close_home_ad(tab)

        if not self._wait_for_xpath(
            tab,
            DATE_SWITCH_BUTTON_XPATH,
            NEXT_ELEMENT_TIMEOUT_SECONDS,
            "经营指标页日期切换按钮",
        ):
            raise RuntimeError("经营指标页加载后未发现日期切换按钮，停止本店铺采集")

    @staticmethod
    def _read_current_url(tab: Any) -> str:
        """读取紫鸟当前标签页网址；属性读取失败时回退到 JavaScript location.href。"""
        try:
            current_url = getattr(tab, "url", "")
            if callable(current_url):
                current_url = current_url()
            current_url = str(current_url or "").strip()
            if current_url:
                return current_url
        except Exception as exc:
            LOGGER.warning("[美客多][网址读取异常] tab.url 读取失败=%s，尝试读取 location.href", exc)

        try:
            return str(tab.run_js("return window.location.href;") or "").strip()
        except Exception as exc:
            LOGGER.warning("[美客多][网址读取失败] location.href 读取失败=%s，按普通域名指标页处理", exc)
            return ""

    @staticmethod
    def _select_metrics_page_url(current_url: str) -> str:
        """当前网址包含 vendedores 时选卖家后台指标页，否则选普通域名指标页。"""
        if "vendedores" in str(current_url or "").casefold():
            return METRICS_PAGE_URL
        return GENERAL_METRICS_PAGE_URL

    def _close_home_ad(self, tab: Any) -> bool:
        """关闭经营指标页可能出现的广告弹窗；未出现时直接继续。"""
        if not self._find_visible_element(tab, HOME_AD_CLOSE_XPATH, timeout=1):
            LOGGER.info("[美客多][广告弹窗] 经营指标页额外等待 10 秒后未发现关闭按钮，直接继续")
            return False
        LOGGER.info("[美客多][广告弹窗] 发现广告弹窗，准备点击关闭，xpath=%s", HOME_AD_CLOSE_XPATH)
        return self._click_with_retry(tab, HOME_AD_CLOSE_XPATH, "关闭经营指标页广告弹窗")

    def _run_click_steps(self, tab: Any, steps: list[dict[str, Any]], final_next_xpath: str = "") -> None:
        """按顺序点击按钮；每个按钮均重试 3 次，并等待下一元素最多 30 秒。"""
        for index, step in enumerate(steps):
            step_name = str(step.get("name") or f"第 {index + 1} 个未命名按钮")
            xpath = str(step.get("xpath") or "").strip()
            if not xpath:
                # 尚未知道实际 XPath 时，保留步骤但安全跳过。
                LOGGER.info("[美客多][按钮跳过] 步骤=%s，原因=XPath 为空", step_name)
                continue

            success_xpath = str(step.get("success_xpath") or "").strip()
            success_state = str(step.get("success_state") or "").strip().lower()
            success_name = str(step.get("success_name") or "点击后的页面状态")
            if not self._click_with_retry(
                tab,
                xpath,
                step_name,
                success_xpath=success_xpath,
                success_state=success_state,
                success_name=success_name,
            ):
                LOGGER.error("[美客多][按钮失败] 步骤=%s，全部 4 次点击均失败，继续后续流程", step_name)
                continue

            wait_seconds = float(step.get("wait_seconds", 1) or 0)
            if wait_seconds > 0:
                LOGGER.info("[美客多][按钮] 步骤=%s 点击成功，先等待 %.1f 秒", step_name, wait_seconds)
                time.sleep(wait_seconds)

            next_xpath = self._next_step_xpath(steps, index + 1) or final_next_xpath
            self._wait_for_xpath(tab, next_xpath, NEXT_ELEMENT_TIMEOUT_SECONDS, f"{step_name} 后的下一步骤或数据")

    def _click_with_retry(
        self,
        tab: Any,
        xpath: str,
        step_name: str,
        success_xpath: str = "",
        success_state: str = "",
        success_name: str = "点击后的页面状态",
    ) -> bool:
        """点击并验证页面状态；验证失败也会进入最多 3 次的重试流程。"""
        max_attempts = CLICK_RETRY_TIMES + 1
        for attempt in range(max_attempts):
            # 日期菜单可能在上一次点击后延迟完成变化；重试前先检查目标状态，
            # 避免已经成功却再次点击，从而把刚打开的菜单重新关闭。
            if success_xpath and success_state == "visible" and self._element_state_matches(tab, success_xpath, success_state):
                LOGGER.info("[美客多][按钮验证成功] 步骤=%s，%s，无需再次点击", step_name, success_name)
                return True
            if attempt > 0 and success_xpath and success_state == "hidden" and self._element_state_matches(tab, success_xpath, success_state):
                LOGGER.info("[美客多][按钮验证成功] 步骤=%s，%s，在重试前确认上次点击已生效", step_name, success_name)
                return True

            if attempt > 0:
                interval = CLICK_RETRY_INTERVAL_SECONDS + random.uniform(0, 1)
                LOGGER.warning(
                    "[美客多][按钮重试] 步骤=%s，第 %s/%s 次尝试前等待 %.2f 秒，xpath=%s",
                    step_name,
                    attempt + 1,
                    max_attempts,
                    interval,
                    xpath,
                )
                time.sleep(interval)
            try:
                LOGGER.info(
                    "[美客多][按钮查找] 步骤=%s，第 %s/%s 次尝试，xpath=%s",
                    step_name,
                    attempt + 1,
                    max_attempts,
                    xpath,
                )
                element = self._find_visible_element(tab, xpath, timeout=5)
                if not element:
                    LOGGER.warning("[美客多][按钮未找到] 步骤=%s，第 %s/%s 次未找到可见元素", step_name, attempt + 1, max_attempts)
                    continue
                element.click()
                if success_xpath and success_state:
                    LOGGER.info("[美客多][按钮已点击] 步骤=%s，第 %s/%s 次已发送点击，开始验证页面状态", step_name, attempt + 1, max_attempts)
                    verified = self._wait_for_element_state(
                        tab,
                        success_xpath,
                        success_state,
                        NEXT_ELEMENT_TIMEOUT_SECONDS,
                        success_name,
                    )
                    if not verified:
                        LOGGER.warning(
                            "[美客多][按钮验证失败] 步骤=%s，第 %s/%s 次点击后未满足条件=%s，准备重试",
                            step_name,
                            attempt + 1,
                            max_attempts,
                            success_name,
                        )
                        continue
                else:
                    LOGGER.info("[美客多][按钮已点击] 步骤=%s，第 %s/%s 次已发送点击，无额外状态验证", step_name, attempt + 1, max_attempts)
                LOGGER.info("[美客多][按钮成功] 步骤=%s，第 %s/%s 次点击并验证成功", step_name, attempt + 1, max_attempts)
                return True
            except Exception as exc:
                LOGGER.warning(
                    "[美客多][按钮异常] 步骤=%s，第 %s/%s 次失败，异常=%s，xpath=%s",
                    step_name,
                    attempt + 1,
                    max_attempts,
                    exc,
                    xpath,
                )
        LOGGER.error("[美客多][按钮终止] 步骤=%s，全部 %s 次点击均失败，xpath=%s", step_name, max_attempts, xpath)
        return False

    def _wait_for_element_state(
        self,
        tab: Any,
        xpath: str,
        expected_state: str,
        timeout_seconds: float,
        target_name: str,
    ) -> bool:
        """等待元素出现或消失；消失需连续确认两次，避免瞬时查找异常造成误判。"""
        if expected_state not in {"visible", "hidden"}:
            LOGGER.error("[美客多][状态配置错误] 目标=%s，不支持的 expected_state=%s", target_name, expected_state)
            return False

        started_at = time.monotonic()
        deadline = started_at + timeout_seconds
        hidden_checks = 0
        LOGGER.info(
            "[美客多][状态等待] 目标=%s，期望状态=%s，最长等待 %.1f 秒，xpath=%s",
            target_name,
            expected_state,
            timeout_seconds,
            xpath,
        )
        while time.monotonic() < deadline:
            visible = bool(self._find_visible_element(tab, xpath, timeout=1))
            if expected_state == "visible" and visible:
                LOGGER.info("[美客多][状态满足] 目标=%s 已出现，耗时 %.2f 秒", target_name, time.monotonic() - started_at)
                return True
            if expected_state == "hidden":
                if visible:
                    hidden_checks = 0
                else:
                    hidden_checks += 1
                    if hidden_checks >= 2:
                        LOGGER.info("[美客多][状态满足] 目标=%s 已连续两次不可见，确认消失，耗时 %.2f 秒", target_name, time.monotonic() - started_at)
                        return True
            time.sleep(0.5)

        LOGGER.error(
            "[美客多][状态超时] 目标=%s，等待 %.1f 秒仍未达到状态=%s，xpath=%s",
            target_name,
            timeout_seconds,
            expected_state,
            xpath,
        )
        return False

    def _element_state_matches(self, tab: Any, xpath: str, expected_state: str) -> bool:
        """立即检查元素当前状态，用于重试前确认上一次点击是否已经延迟生效。"""
        visible = bool(self._find_visible_element(tab, xpath, timeout=0.5))
        if expected_state == "visible":
            return visible
        if expected_state == "hidden":
            return not visible
        return False

    def _wait_for_page_ready(self, tab: Any, timeout_seconds: float, page_name: str = "当前页面") -> bool:
        """轮询 document.readyState，等待指定页面的主文档加载完成。"""
        started_at = time.monotonic()
        deadline = started_at + timeout_seconds
        last_state = ""
        LOGGER.info(
            "[美客多][页面等待] 页面=%s，等待 document.readyState=complete，最长 %.1f 秒",
            page_name,
            timeout_seconds,
        )
        while time.monotonic() < deadline:
            try:
                state = str(tab.run_js("return document.readyState;") or "").lower()
                if state != last_state:
                    LOGGER.info("[美客多][页面状态] 页面=%s，document.readyState=%s", page_name, state)
                    last_state = state
                if state == "complete":
                    LOGGER.info("[美客多][页面成功] 页面=%s，主文档加载完成，耗时 %.2f 秒", page_name, time.monotonic() - started_at)
                    return True
            except Exception as exc:
                LOGGER.warning("[美客多][页面异常] 页面=%s，读取 document.readyState 失败：%s", page_name, exc)
            time.sleep(1)
        LOGGER.error("[美客多][页面超时] 页面=%s，等待 %.1f 秒仍未加载完成", page_name, timeout_seconds)
        return False

    def _wait_for_xpath(self, tab: Any, xpath: str, timeout_seconds: float, target_name: str) -> bool:
        """等待指定的下一按钮或数据；未配置 XPath 时固定等待完整 30 秒。"""
        if not xpath:
            LOGGER.warning("[美客多][元素等待] 目标=%s 未配置 XPath，固定等待 %.1f 秒", target_name, timeout_seconds)
            time.sleep(timeout_seconds)
            return True
        started_at = time.monotonic()
        deadline = started_at + timeout_seconds
        LOGGER.info("[美客多][元素等待] 目标=%s，最长等待 %.1f 秒，xpath=%s", target_name, timeout_seconds, xpath)
        while time.monotonic() < deadline:
            if self._find_visible_element(tab, xpath, timeout=1):
                LOGGER.info("[美客多][元素出现] 目标=%s，耗时 %.2f 秒", target_name, time.monotonic() - started_at)
                return True
            time.sleep(1)
        LOGGER.error("[美客多][元素超时] 目标=%s，等待 %.1f 秒仍未出现，xpath=%s", target_name, timeout_seconds, xpath)
        return False

    @staticmethod
    def _find_visible_element(tab: Any, xpath: str, timeout: float = 1) -> Any:
        """查找 XPath 元素，并兼容 DrissionPage 不同版本的可见性属性。"""
        if not xpath:
            return None
        try:
            element = tab.ele(f"xpath:{xpath}", timeout=timeout)
            if not element:
                return None
            states = getattr(element, "states", None)
            displayed = getattr(states, "is_displayed", None) if states is not None else None
            if callable(displayed):
                displayed = displayed()
            if displayed is not None:
                return element if bool(displayed) else None
            legacy_displayed = getattr(element, "is_displayed", None)
            if callable(legacy_displayed):
                return element if bool(legacy_displayed()) else None
            if legacy_displayed is not None:
                return element if bool(legacy_displayed) else None
            # 无可见性属性时按已找到处理，兼容简单的 DrissionPage 元素对象和测试对象。
            return element
        except Exception:
            return None

    @staticmethod
    def _next_step_xpath(steps: list[dict[str, Any]], start_index: int) -> str:
        """从后续按钮步骤中返回第一个非空 XPath。"""
        for step in steps[start_index:]:
            xpath = str(step.get("xpath") or "").strip()
            if xpath:
                return xpath
        return ""

    @staticmethod
    def _first_metric_xpath(period: str) -> str:
        """返回指定时间范围的第一个非空指标 XPath，供按钮点击后等待数据。"""
        for spec in METRIC_SPECS:
            if spec["period"] == period and spec.get("xpath"):
                return spec["xpath"]
        return ""

    def _read_xpath(self, tab: Any, xpath: str, field_name: str = "未命名指标") -> str:
        """读取一个 XPath 文本；XPath 为空、元素不存在或异常时返回空字符串。"""
        if not xpath:
            LOGGER.warning("[美客多][指标跳过] 字段=%s，原因=XPath 为空", field_name)
            return ""
        for attempt in range(2):
            if attempt > 0:
                LOGGER.warning(
                    "[美客多][指标重试] 字段=%s，第 2/2 次读取前等待 %s 秒，xpath=%s",
                    field_name,
                    CLICK_RETRY_INTERVAL_SECONDS,
                    xpath,
                )
                time.sleep(CLICK_RETRY_INTERVAL_SECONDS)
            try:
                LOGGER.info("[美客多][指标查找] 字段=%s，第 %s/2 次读取，xpath=%s", field_name, attempt + 1, xpath)
                element = tab.ele(f"xpath:{xpath}", timeout=3)
                if element:
                    raw_text = str(element.text or "").strip()
                    if raw_text:
                        LOGGER.info("[美客多][指标抓取成功] 字段=%s，原始文本=%r", field_name, raw_text)
                        return raw_text
                    LOGGER.warning("[美客多][指标文本为空] 字段=%s，已找到元素但 text 为空", field_name)
                else:
                    LOGGER.warning("[美客多][指标未找到] 字段=%s，第 %s/2 次未找到元素", field_name, attempt + 1)
            except Exception as exc:
                LOGGER.warning(
                    "[美客多][指标异常] 字段=%s，第 %s/2 次读取失败，异常=%s，xpath=%s",
                    field_name,
                    attempt + 1,
                    exc,
                    xpath,
                )
        LOGGER.error("[美客多][指标最终失败] 字段=%s，两次读取均未得到有效文本，返回空字符串", field_name)
        return ""

    @staticmethod
    def _format_value(raw_text: str, kind: str) -> Any:
        """按字段类型转换数据：货币两位小数、整数无小数、进度为数值比例。"""
        if not raw_text:
            return ""
        if kind == "integer":
            # 数量类指标不应有小数；点号和逗号只当千位分隔符处理。
            integer_text = re.sub(r"[^0-9-]", "", str(raw_text))
            if not integer_text or integer_text == "-":
                return ""
            try:
                return int(integer_text)
            except ValueError:
                return ""
        number = MercadoAuto._parse_number(raw_text)
        if number is None:
            return ""
        if kind == "currency":
            return round(number, 2)
        if kind == "percent":
            # 爬虫内部统一保存数值比例，便于后续计算和 DeepSeek 分析。
            # 页面 12.5% 转为 0.125；写入飞书时由 orchestrator 转换成 "12.5%" 文本。
            ratio = number / 100 if "%" in str(raw_text) or abs(number) > 1 else number
            return round(ratio, 4)
        return raw_text

    @staticmethod
    def _parse_number(raw_text: str) -> float | None:
        """解析巴西常见格式，例如 ``R$ 1.234,56`` 或 ``12,5%``。"""
        text = str(raw_text).strip().replace("%", "")
        text = re.sub(r"[^0-9,.-]", "", text)
        if not text or text in {"-", ".", ","}:
            return None
        try:
            # 同时有点和逗号时，按巴西格式把点当千位分隔、逗号当小数点。
            if "." in text and "," in text:
                text = text.replace(".", "").replace(",", ".")
            elif "," in text:
                text = text.replace(",", ".")
            return float(text)
        except (TypeError, ValueError):
            return None


def collect_mercado_ad(store_name: str, download_path: str = "", debugging_port: int | str | None = None) -> list[dict[str, Any]]:
    """提供一个可直接调用的美客多函数入口。"""
    return MercadoAuto().collect(store_name, download_path, debugging_port)
