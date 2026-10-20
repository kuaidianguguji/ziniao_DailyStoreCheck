"""美客多广告页面的精准网络监听与今日数据提取。"""

from __future__ import annotations

import json
import logging
import math
import re
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit
from mercado.ad_page import MercadoAdPage, MercadoAdPopupError


LOGGER = logging.getLogger(__name__)

DEFAULT_AD_PAGE_URL = "https://vendedores.mercadolivre.com.br/publicidade/resumo-anunciante"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
DEFAULT_FALLBACK_REQUEST_TIMEOUT_SECONDS = 60.0
DEFAULT_FALLBACK_DOUBLE_CLICK_MIN_INTERVAL_MS = 80.0
DEFAULT_FALLBACK_DOUBLE_CLICK_MAX_INTERVAL_MS = 180.0
TARGET_PATH_RE = re.compile(r"^/advertiser-hub/api/general-metrics/([^/]+)/chart$")
FALLBACK_PATH_RE = re.compile(r"^/pa/api/admin-pads/ajax/campaigns/search$")
PERIOD_BUTTON_XPATH = '//div[@class="andes-floating-menu"]//button[@aria-label="Período" or @aria-label="周期"]'
CUSTOM_PERIOD_XPATH = '//div[@data-tippy-root]//ul/li[@data-key="custom"]'
TODAY_CALENDAR_XPATH = '//div[@class="campaigns-period-picker"]//tbody//td[@data-today="true"]'
BRAZIL_TIMEZONE = timezone(timedelta(hours=-3), "BRT")
CHART_LISTENER_PATTERN = r"/advertiser-hub/api/general-metrics/[^/]+/chart\?"

# 接口中的数组名称与飞书业务字段一一对应；每个数组只取巴西今天对应的最后一条记录。
AD_METRIC_FIELDS: tuple[tuple[str, str], ...] = (
    ("income", "今天广告销售额"),
    ("prints", "今天广告曝光数"),
    ("visits", "今天广告访客数"),
    ("followers", "今天广告新粉丝数"),
    ("investment", "今天广告成本"),
    ("clicks", "今天广告点击量"),
    ("sales", "今天广告销售量"),
)


class MercadoAdCapture:
    """在当前已登录的 DrissionPage 标签页中监听美客多广告接口。"""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        human_config: dict[str, Any] | None = None,
    ):
        config = config if isinstance(config, dict) else {}
        self.enabled = bool(config.get("enabled", True))
        self.page_url = str(config.get("page_url") or DEFAULT_AD_PAGE_URL).strip()
        self.request_timeout_seconds = max(
            1.0,
            float(config.get("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS) or DEFAULT_REQUEST_TIMEOUT_SECONDS),
        )
        self.fallback_request_timeout_seconds = max(
            1.0,
            float(config.get("fallback_request_timeout_seconds", DEFAULT_FALLBACK_REQUEST_TIMEOUT_SECONDS) or DEFAULT_FALLBACK_REQUEST_TIMEOUT_SECONDS),
        )
        self.fallback_double_click_min_interval_ms = self._read_non_negative_float(
            config.get(
                "fallback_double_click_min_interval_ms",
                DEFAULT_FALLBACK_DOUBLE_CLICK_MIN_INTERVAL_MS,
            ),
            DEFAULT_FALLBACK_DOUBLE_CLICK_MIN_INTERVAL_MS,
        )
        self.fallback_double_click_max_interval_ms = self._read_non_negative_float(
            config.get(
                "fallback_double_click_max_interval_ms",
                DEFAULT_FALLBACK_DOUBLE_CLICK_MAX_INTERVAL_MS,
            ),
            DEFAULT_FALLBACK_DOUBLE_CLICK_MAX_INTERVAL_MS,
        )
        if self.fallback_double_click_min_interval_ms > self.fallback_double_click_max_interval_ms:
            LOGGER.warning(
                "[美客多][广告备用双击配置] 最小间隔 %.1fms 大于最大间隔 %.1fms，已自动交换",
                self.fallback_double_click_min_interval_ms,
                self.fallback_double_click_max_interval_ms,
            )
            (
                self.fallback_double_click_min_interval_ms,
                self.fallback_double_click_max_interval_ms,
            ) = (
                self.fallback_double_click_max_interval_ms,
                self.fallback_double_click_min_interval_ms,
            )
        self._ad_page = MercadoAdPage(config, human_config=human_config)
        self._active_listener: Any = None

    def collect_today(self, tab: Any, store_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """跳转广告页并返回飞书字段、原始响应摘要。"""
        if not self.enabled:
            LOGGER.info("[美客多][广告] 店铺=%s，广告监听已按配置关闭", store_name)
            return {}, {"enabled": False}

        listener = getattr(tab, "listen", None)
        if listener is None:
            raise RuntimeError("当前 DrissionPage 标签页不支持网络监听")

        # 一次采集冻结巴西今天，防止等待期间跨午夜后混用两个日期。
        today = datetime.now(BRAZIL_TIMEZONE).date().isoformat()
        LOGGER.info(
            "[美客多][广告监听] 店铺=%s，先启动监听再跳转广告页，页面=%s，目标正则=%s",
            store_name,
            self.page_url,
            CHART_LISTENER_PATTERN,
        )
        self._start_chart_listener(listener)
        try:
            tab.get(self.page_url)
            self._ad_page.close_budget_popup(tab, store_name)
            LOGGER.info("[美客多][广告监听] 店铺=%s，已跳转广告页，等待精准 chart 响应，最长 %.1f 秒", store_name, self.request_timeout_seconds)
            try:
                packet = self._wait_for_target_packet(listener, store_name, tab=tab)
                payload = self._packet_json(packet, store_name)
                fields, summary = self._extract_today(
                    payload, store_name, today=today,
                    require_single_day=self._matches_target_url(str(packet.url), expected_date=today),
                )
                if not summary["missing_metrics"]:
                    self._ad_page.close_budget_popup(tab, store_name, wait_seconds=2.0)
                    LOGGER.info("[美客多][广告完成] 店铺=%s，来源=chart，日期=%s，广告字段=%s", store_name, today, fields)
                    return fields, summary
                LOGGER.warning(
                    "[美客多][广告数据不完整] 店铺=%s，日期=%s，缺失指标=%s；准备切换今天重新监听",
                    store_name, today, summary["missing_metrics"],
                )
            except MercadoAdPopupError:
                raise
            except (TimeoutError, RuntimeError) as exc:
                LOGGER.warning("[美客多][广告主接口不可用] 店铺=%s，原因=%s；准备日期选择流程", store_name, exc)

            if self._ad_page.is_overview_page(tab):
                return self._collect_overview_today(tab, listener, store_name, today)
            # 旧广告管理页使用 campaigns-period-picker，不能混用概览页的 Andes 日期控件。
            if self._is_campaigns_page(tab):
                return self._collect_fallback(tab, store_name, today=today)
            raise RuntimeError("美客多广告页面未找到概览日期控件，也未进入旧广告活动管理页")
        finally:
            self._stop_listener(listener, store_name)

    def _start_chart_listener(self, listener: Any) -> None:
        """重新开始监听会清空旧数据包，必须在触发今天日期点击之前调用。"""
        self._start_listener(listener, CHART_LISTENER_PATTERN)

    def _start_listener(self, listener: Any, pattern: str) -> None:
        """统一登记实际启动的监听器；重新设定目标时保留同一个浏览器连接。"""
        listener.start(targets=pattern, is_regex=True, method="GET", res_type=("XHR", "Fetch"))
        self._active_listener = listener

    def _stop_listener(self, listener: Any, store_name: str) -> None:
        """同一轮启动只停止一次，即使监听器未提供 listening 属性也不会重复停止。"""
        if self._active_listener is not listener:
            return
        self._active_listener = None
        if getattr(listener, "listening", True) is False:
            return
        try:
            listener.stop()
        except Exception as exc:
            LOGGER.warning("[美客多][广告监听停止失败] 店铺=%s，异常类型=%s", store_name, type(exc).__name__)

    @staticmethod
    def _is_campaigns_page(tab: Any) -> bool:
        """只有真实旧广告管理页面才允许使用 campaigns/search 备用流程。"""
        try:
            if urlsplit(str(tab.url or "")).hostname == "pa.mercadolivre.com.br":
                return True
            element = tab.ele(f"xpath:{PERIOD_BUTTON_XPATH}", timeout=0.2)
            return bool(element and element.states.is_displayed)
        except Exception:
            return False

    def _collect_overview_today(
        self, tab: Any, listener: Any, store_name: str, today: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """概览切至巴西今天后重抓 chart；仍不可用时才读取五项页面指标。"""
        self._ad_page.select_today(
            tab, store_name, today,
            before_date_click=lambda: self._start_chart_listener(listener),
        )
        try:
            packet = self._wait_for_target_packet(listener, store_name, expected_date=today, tab=tab)
            payload = self._packet_json(packet, store_name)
            fields, summary = self._extract_today(payload, store_name, today=today, require_single_day=True)
            if not summary["missing_metrics"]:
                self._ad_page.close_budget_popup(tab, store_name, wait_seconds=2.0)
                LOGGER.info("[美客多][广告今日重监听成功] 店铺=%s，日期=%s，广告字段=%s", store_name, today, fields)
                return fields, summary
            LOGGER.warning(
                "[美客多][广告今日仍缺失] 店铺=%s，指标=%s；读取今日页面五项指标",
                store_name, summary["missing_metrics"],
            )
        except MercadoAdPopupError:
            raise
        except (TimeoutError, RuntimeError) as exc:
            LOGGER.warning("[美客多][广告今日响应不可用] 店铺=%s，原因=%s；读取今日页面五项指标", store_name, exc)
        self._stop_listener(listener, store_name)
        fields, summary = self._ad_page.read_today_metrics(tab, store_name, today)
        LOGGER.info("[美客多][广告页面采集完成] 店铺=%s，日期=%s，字段=%s", store_name, today, fields)
        return fields, summary

    def _collect_fallback(
        self, tab: Any, store_name: str, *, today: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """主 chart 接口没有返回时，通过页面周期选择触发 campaigns/search 请求。"""
        today = today or datetime.now(BRAZIL_TIMEZONE).date().isoformat()
        self._ad_page.close_budget_popup(tab, store_name)
        period_button = self._wait_element(tab, PERIOD_BUTTON_XPATH, self.fallback_request_timeout_seconds)
        if period_button is None:
            raise TimeoutError("美客多广告备用流程未找到周期选择按钮")
        try:
            period_button.scroll.to_see(center=True)
        except Exception as exc:
            LOGGER.warning("[美客多][广告备用] 周期按钮滚动到中心失败，继续点击：%s", exc)

        listener = getattr(tab, "listen", None)
        if listener is None:
            raise RuntimeError("当前 DrissionPage 标签页不支持网络监听")
        target_pattern = r"/pa/api/admin-pads/ajax/campaigns/search\?"
        self._start_listener(listener, target_pattern)
        try:
            self._ad_page.close_budget_popup(tab, store_name)
            self._click_element(period_button, tab, "打开广告周期菜单")
            custom = self._wait_element(tab, CUSTOM_PERIOD_XPATH, 10.0)
            if custom is None:
                raise TimeoutError("美客多广告备用流程未找到自定义时间按钮")
            self._ad_page.close_budget_popup(tab, store_name)
            self._click_element(custom, tab, "选择自定义时间")
            today_element = self._wait_element(tab, TODAY_CALENDAR_XPATH, 20.0)
            if today_element is None:
                raise TimeoutError("美客多广告备用流程未找到今天日期按钮")
            self._ad_page.close_budget_popup(tab, store_name)
            self._double_click_element(today_element, tab, "选择今天日期")
            packet = self._wait_for_fallback_packet(listener, store_name, expected_date=today, tab=tab)
            payload = self._packet_json(packet, store_name)
            fields, summary = self._extract_fallback(payload, store_name)
            summary.update({"date": today, "date_from": today, "date_to": today, "currency": "BRL"})
            self._ad_page.close_budget_popup(tab, store_name, wait_seconds=2.0)
            LOGGER.info("[美客多][广告备用完成] 店铺=%s，活动字段=%s", store_name, fields)
            return fields, summary
        finally:
            self._stop_listener(listener, store_name)

    @staticmethod
    def _wait_element(tab: Any, xpath: str, timeout_seconds: float) -> Any:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                element = tab.ele(f"xpath:{xpath}", timeout=min(1.0, max(0.1, deadline - time.monotonic())))
                if element:
                    return element
            except Exception:
                pass
            time.sleep(0.25)
        return None

    @staticmethod
    def _click_element(element: Any, tab: Any, action_name: str) -> None:
        try:
            element.click()
        except Exception:
            # 备用日期控件可能在重绘后失效，使用当前元素的 JavaScript 点击作为最后回退。
            try:
                element.run_js("this.click()")
            except Exception as exc:
                raise RuntimeError(f"{action_name}失败：{exc}") from exc

    def _double_click_element(self, element: Any, tab: Any, action_name: str) -> None:
        """以随机间隔连续点击两次日期元素，兼容第一次点击后的节点重绘。"""
        self._click_element(element, tab, f"{action_name}第1次点击")
        interval_ms = random.uniform(
            self.fallback_double_click_min_interval_ms,
            self.fallback_double_click_max_interval_ms,
        )
        LOGGER.info(
            "[美客多][广告备用双击] 步骤=%s，第1次点击完成，随机间隔=%.1fms",
            action_name,
            interval_ms,
        )
        time.sleep(interval_ms / 1000.0)

        # 日期控件第一次点击后可能整体重绘，第二次点击前重新查找最新节点。
        second_element = self._wait_element(tab, TODAY_CALENDAR_XPATH, 2.0)
        if second_element is None:
            second_element = element
            LOGGER.warning(
                "[美客多][广告备用双击] 步骤=%s，第二次查找未找到日期节点，回退使用第一次节点",
                action_name,
            )
        else:
            LOGGER.info(
                "[美客多][广告备用双击] 步骤=%s，第二次点击已重新定位日期节点",
                action_name,
            )
        self._click_element(second_element, tab, f"{action_name}第2次点击")
        LOGGER.info(
            "[美客多][广告备用双击] 步骤=%s，第2次点击完成",
            action_name,
        )

    def _wait_for_fallback_packet(
        self, listener: Any, store_name: str, *, expected_date: str | None = None, tab: Any = None,
    ) -> Any:
        expected_date = expected_date or datetime.now(BRAZIL_TIMEZONE).date().isoformat()
        deadline = time.monotonic() + self.fallback_request_timeout_seconds
        invalid_count = 0
        matched_count = 0
        while time.monotonic() < deadline:
            if tab is not None:
                self._ad_page.close_budget_popup(tab, store_name)
            packet = listener.wait(timeout=min(2.0, max(0.2, deadline - time.monotonic())), raise_err=False)
            if not packet:
                continue
            url = str(getattr(packet, "url", "") or "")
            if self._matches_fallback_url(url, expected_date=expected_date):
                matched_count += 1
                try:
                    payload = self._packet_json(packet, store_name)
                    if not isinstance(payload.get("results"), list):
                        raise RuntimeError("campaigns_structure：美客多广告备用响应缺少 results 数组")
                except RuntimeError as exc:
                    invalid_count += 1
                    LOGGER.warning("[美客多][广告备用无效包] 店铺=%s，原因=%s；继续等待有效响应", store_name, exc)
                    continue
                LOGGER.info("[美客多][广告备用命中] 店铺=%s，当天 campaigns/search 的状态、响应 JSON 和结构已校验，日期=%s", store_name, expected_date)
                return packet
            LOGGER.debug("[美客多][广告备用忽略] 店铺=%s，非当天目标请求：%s", store_name, self._safe_url(url))
        raise TimeoutError(
            f"美客多广告备用接口在 {self.fallback_request_timeout_seconds:.1f} 秒内未返回有效响应，"
            f"目标匹配包数={matched_count}，无效包数={invalid_count}"
        )

    @classmethod
    def _matches_fallback_url(cls, url: str, *, expected_date: str | None = None) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc.casefold() != "pa.mercadolivre.com.br" or not FALLBACK_PATH_RE.match(parsed.path):
            return False
        query = parse_qs(parsed.query, keep_blank_values=True)
        today = expected_date or datetime.now(BRAZIL_TIMEZONE).date().isoformat()
        return (
            query.get("dateFrom") == [today]
            and query.get("dateTo") == [today]
            and not query.get("campaignIds")
        )

    @classmethod
    def _extract_fallback(cls, payload: dict[str, Any], store_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        results = payload.get("results")
        if not isinstance(results, list):
            raise RuntimeError("美客多广告备用响应缺少 results 数组")
        active = [item for item in results if isinstance(item, dict) and str(item.get("status", "")).casefold() == "active"]
        totals = {"clicks": 0, "prints": 0, "cost": 0.0, "totalAmount": 0.0, "unitsQuantity": 0}
        for campaign in active:
            metrics = campaign.get("metrics")
            if not isinstance(metrics, dict):
                continue
            for key in totals:
                value = metrics.get(key, 0)
                try:
                    totals[key] += float(value or 0)
                except (TypeError, ValueError):
                    LOGGER.warning("[美客多][广告备用字段异常] 店铺=%s，字段=%s，值=%r", store_name, key, value)
        fields = {
            "今天广告销售额": totals["totalAmount"],
            "今天广告曝光数": int(totals["prints"]),
            "今天广告访客数": "",
            "今天广告新粉丝数": "",
            "今天广告成本": totals["cost"],
            "今天广告点击量": int(totals["clicks"]),
            "今天广告销售量": int(totals["unitsQuantity"]),
        }
        summary = {"source": "campaigns/search", "active_campaign_count": len(active), "metrics": totals}
        return fields, summary

    def _wait_for_target_packet(
        self, listener: Any, store_name: str, *, expected_date: str | None = None, tab: Any = None,
    ) -> Any:
        deadline = time.monotonic() + self.request_timeout_seconds
        matched_count = 0
        invalid_count = 0
        while time.monotonic() < deadline:
            if tab is not None:
                self._ad_page.close_budget_popup(tab, store_name)
            remaining = max(0.2, deadline - time.monotonic())
            packet = listener.wait(timeout=min(2.0, remaining), raise_err=False)
            if not packet:
                continue
            url = str(getattr(packet, "url", "") or "")
            if self._matches_target_url(url, expected_date=expected_date):
                matched_count += 1
                try:
                    payload = self._packet_json(packet, store_name)
                    data = payload.get("data")
                    if not isinstance(data, dict) or not isinstance(data.get("summary_by_date"), dict):
                        raise RuntimeError("chart_structure：美客多广告响应缺少 data.summary_by_date")
                    if expected_date is not None and (
                        payload.get("date_from") != expected_date or payload.get("date_to") != expected_date
                    ):
                        raise RuntimeError("chart_date_mismatch：美客多广告响应日期不匹配")
                except RuntimeError as exc:
                    invalid_count += 1
                    LOGGER.warning("[美客多][广告无效包] 店铺=%s，原因=%s；继续等待有效响应", store_name, exc)
                    continue
                LOGGER.info("[美客多][广告命中] 店铺=%s，目标 chart URL、状态、响应 JSON 和结构已校验，目标日期=%s", store_name, expected_date or "页面当前周期")
                return packet
            LOGGER.debug("[美客多][广告忽略] 店铺=%s，监听到非目标请求：%s", store_name, self._safe_url(url))
        raise TimeoutError(
            f"美客多广告目标 chart 接口在 {self.request_timeout_seconds:.1f} 秒内未返回有效响应，"
            f"目标匹配包数={matched_count}，无效包数={invalid_count}"
        )

    @staticmethod
    def _matches_target_url(url: str, *, expected_date: str | None = None) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc.casefold() != "vendedores.mercadolivre.com.br":
            return False
        match = TARGET_PATH_RE.match(parsed.path)
        if not match:
            return False
        # 该路径精确到 /chart，天然排除 /chart/comparison。
        query = parse_qs(parsed.query, keep_blank_values=True)
        if not query.get("dateFrom") or not query.get("dateTo") or query.get("siteId", [""])[0] != "MLB":
            return False
        if expected_date is not None and (
            query.get("dateFrom") != [expected_date] or query.get("dateTo") != [expected_date]
        ):
            return False
        products = query.get("products", [""])[0].replace("%2C", ",").upper()
        return {part.strip() for part in products.split(",")} == {"PADS", "DADS"}

    @staticmethod
    def _packet_json(packet: Any, store_name: str) -> dict[str, Any]:
        """先记录安全元数据再校验响应；成功正文只解析一次，绝不输出正文和敏感头。"""
        cached = getattr(packet, "_mercado_ad_valid_json", None)
        if isinstance(cached, dict):
            return cached

        def read_attribute(owner: Any, name: str) -> tuple[Any, str]:
            try:
                return getattr(owner, name, None), ""
            except Exception as exc:
                # 异常消息可能含有 URL、Cookie 或正文，只保留异常类型。
                return None, type(exc).__name__

        def byte_length(value: Any) -> int | None:
            if value is None:
                return 0
            if isinstance(value, (bytes, bytearray)):
                return len(value)
            if isinstance(value, str):
                return len(value.encode("utf-8"))
            if isinstance(value, (dict, list)):
                try:
                    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
                except (TypeError, ValueError):
                    return None
            return None

        failed, failed_read_error = read_attribute(packet, "is_failed")
        failed = bool(failed)
        response, response_read_error = read_attribute(packet, "response")
        status_value, status_read_error = read_attribute(response, "status")
        try:
            status = int(status_value) if status_value is not None and not isinstance(status_value, bool) else None
        except (TypeError, ValueError, OverflowError):
            status = None
        headers, headers_read_error = read_attribute(response, "headers")
        content_type = "<未提供>"
        try:
            mime = str((headers or {}).get("Content-Type") or (headers or {}).get("content-type") or "")
            # 只记录标准 MIME 部分，丢弃任意扩展参数与未经校验的值。
            match = re.match(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?=\s*(?:;|$))", mime)
            if match:
                content_type = match.group(0).lower()
        except Exception as exc:
            headers_read_error = type(exc).__name__
        body, body_read_error = read_attribute(response, "body")
        raw_body, raw_read_error = read_attribute(response, "raw_body")
        error_code = "<未提供>"
        canceled = False
        if failed:
            fail_info, _ = read_attribute(packet, "fail_info")
            reason, _ = read_attribute(fail_info, "errorText")
            if isinstance(reason, str):
                match = re.match(r"^net::ERR_[A-Z0-9_]+\b", reason)
                if match:
                    error_code = match.group(0)
            cancel_value, _ = read_attribute(fail_info, "canceled")
            canceled = cancel_value is True

        source = "body"

        def diagnose(code: str, message: str | None = None) -> None:
            LOGGER.log(
                logging.INFO if message is None else logging.WARNING,
                "[美客多][广告响应诊断] 店铺=%s，分类=%s，failed=%s，status=%s，Content-Type=%s，"
                "body类型=%s，body长度字节=%s，raw_body长度字节=%s，正文来源=%s，"
                "浏览器错误码=%s，canceled=%s，元数据读取异常=%s，body读取异常=%s，raw_body读取异常=%s",
                store_name, code, failed, status if status is not None else "<缺失或无效>", content_type,
                type(body).__name__, byte_length(body), byte_length(raw_body), source,
                error_code, canceled,
                "/".join(value for value in (failed_read_error, response_read_error, status_read_error, headers_read_error) if value) or "无",
                body_read_error or "无", raw_read_error or "无",
            )
            if message is not None:
                raise RuntimeError(f"{code}：{message}") from None

        if failed:
            diagnose("request_failed", "美客多广告目标请求在浏览器中加载失败")
        if response is None:
            diagnose("response_unavailable", "美客多广告目标请求没有可读取响应")
        if status is None:
            diagnose("invalid_status", "美客多广告目标请求缺少有效 HTTP 响应状态")
        if not 200 <= status < 300:
            diagnose("http_error", f"美客多广告目标请求返回 HTTP {status}")
        if isinstance(body, dict):
            payload = body
        else:
            candidate = body
            if candidate is None or (isinstance(candidate, (str, bytes, bytearray)) and not candidate.strip()):
                candidate = raw_body
                source = "raw_body"
            if candidate is None or candidate == "" or candidate == b"":
                if body_read_error or raw_read_error:
                    diagnose("body_read_error", "美客多广告响应正文读取失败")
                diagnose("empty_body", "美客多广告目标响应正文为空")
            if isinstance(candidate, (bytes, bytearray)):
                try:
                    candidate = candidate.decode("utf-8-sig")
                except UnicodeDecodeError:
                    diagnose("invalid_encoding", "美客多广告响应正文不是有效 UTF-8")
            if not isinstance(candidate, str):
                diagnose("nonobject_json", "美客多广告目标响应 JSON 根节点不是对象")
            if not candidate.strip():
                diagnose("empty_body", "美客多广告目标响应正文为空")
            try:
                payload = json.loads(candidate.lstrip("\ufeff"))
            except (TypeError, ValueError):
                diagnose("invalid_json", "美客多广告目标响应不是有效 JSON")
            if not isinstance(payload, dict):
                diagnose("nonobject_json", "美客多广告目标响应 JSON 根节点不是对象")
        diagnose("json_valid")
        try:
            setattr(packet, "_mercado_ad_valid_json", payload)
        except (AttributeError, TypeError):
            pass
        return payload

    @classmethod
    def _extract_today(
        cls, payload: dict[str, Any], store_name: str, *, today: str | None = None, require_single_day: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        today = today or datetime.now(BRAZIL_TIMEZONE).date().isoformat()
        is_single_day = payload.get("date_from") == today and payload.get("date_to") == today
        if require_single_day and not is_single_day:
            raise RuntimeError("美客多广告今日请求的响应日期不匹配，拒绝使用其他周期数据")
        data = payload.get("data")
        summary_by_date = data.get("summary_by_date") if isinstance(data, dict) else None
        if not isinstance(summary_by_date, dict):
            raise RuntimeError("美客多广告响应缺少 data.summary_by_date")

        fields: dict[str, Any] = {}
        summary: dict[str, Any] = {
            "source": "chart", "advertiser_id": payload.get("advertiser_id", ""), "date": today,
            "currency": data.get("currency", "BRL"), "missing_metrics": [],
        }
        daily_summary = data.get("summary") if require_single_day and is_single_day else None
        for response_key, field_name in AD_METRIC_FIELDS:
            entries = summary_by_date.get(response_key)
            selected = next(
                (entry for entry in reversed(entries) if isinstance(entry, dict) and entry.get("date") == today),
                {},
            ) if isinstance(entries, list) else {}
            value = selected.get("total")
            source = "summary_by_date"
            if not cls._is_metric_number(value) and isinstance(daily_summary, dict):
                # 只有请求和响应均为巴西今天单日时，汇总值才等同于今天值；合法零值必须保留。
                value = daily_summary.get(response_key)
                source = "summary"
            if not cls._is_metric_number(value):
                summary["missing_metrics"].append(response_key)
                LOGGER.warning("[美客多][广告字段缺失] 店铺=%s，接口字段=%s，没有巴西今天的有效数值", store_name, response_key)
                continue
            fields[field_name] = value
            summary[response_key] = {"date": today, "total": value, "source": source}
        return fields, summary

    @staticmethod
    def _is_metric_number(value: Any) -> bool:
        """缺失、布尔值和无穷值不能冒充业务数值；数值零属于有效数据。"""
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    @staticmethod
    def _safe_url(url: str) -> str:
        """忽略包也只显示已知接口路径，避免 URL 用户信息、查询参数或未知路径泄露。"""
        try:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.username or parsed.password:
                return "<非目标网址>"
            if parsed.hostname == "vendedores.mercadolivre.com.br" and re.match(
                r"^/advertiser-hub/api/general-metrics/[^/]+/chart(?:/comparison)?$", parsed.path,
            ):
                suffix = "/comparison" if parsed.path.endswith("/comparison") else ""
                return f"https://{parsed.hostname}/advertiser-hub/api/general-metrics/<广告主>/chart{suffix}"
            if parsed.hostname == "pa.mercadolivre.com.br" and FALLBACK_PATH_RE.fullmatch(parsed.path):
                return f"https://{parsed.hostname}{parsed.path}"
        except ValueError:
            pass
        return "<非目标网址>"

    @staticmethod
    def _read_non_negative_float(value: Any, default: float) -> float:
        """读取非负浮点配置，非法值回退默认值。"""
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(parsed) or parsed < 0:
            return default
        return parsed
