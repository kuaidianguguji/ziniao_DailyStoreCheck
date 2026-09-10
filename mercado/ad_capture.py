"""美客多广告页面的精准网络监听与今日数据提取。"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo


LOGGER = logging.getLogger(__name__)

DEFAULT_AD_PAGE_URL = "https://vendedores.mercadolivre.com.br/publicidade/resumo-anunciante"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
DEFAULT_FALLBACK_REQUEST_TIMEOUT_SECONDS = 60.0
TARGET_PATH_RE = re.compile(r"^/advertiser-hub/api/general-metrics/([^/]+)/chart$")
FALLBACK_PATH_RE = re.compile(r"^/pa/api/admin-pads/ajax/campaigns/search$")
PERIOD_BUTTON_XPATH = '//div[@class="andes-floating-menu"]//button[@aria-label="Período" or @aria-label="周期"]'
CUSTOM_PERIOD_XPATH = '//div[@data-tippy-root]//ul/li[@data-key="custom"]'
TODAY_CALENDAR_XPATH = '//div[@class="campaigns-period-picker"]//tbody//td[@data-today="true"]'

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

    def __init__(self, config: dict[str, Any] | None = None):
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

    def collect_today(self, tab: Any, store_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """跳转广告页并返回飞书字段、原始响应摘要。"""
        if not self.enabled:
            LOGGER.info("[美客多][广告] 店铺=%s，广告监听已按配置关闭", store_name)
            return {}, {"enabled": False}

        listener = getattr(tab, "listen", None)
        if listener is None:
            raise RuntimeError("当前 DrissionPage 标签页不支持网络监听")

        # 只订阅 GET 的 XHR/Fetch，并用正则先缩小范围；后面还会按完整路径和 query 再次校验。
        target_pattern = r"/advertiser-hub/api/general-metrics/[^/]+/chart\?"
        LOGGER.info(
            "[美客多][广告监听] 店铺=%s，先启动监听再跳转广告页，页面=%s，目标正则=%s",
            store_name,
            self.page_url,
            target_pattern,
        )
        listener.start(
            targets=target_pattern,
            is_regex=True,
            method="GET",
            res_type=("XHR", "Fetch"),
        )
        try:
            tab.get(self.page_url)
            LOGGER.info("[美客多][广告监听] 店铺=%s，已跳转广告页，等待精准 chart 响应，最长 %.1f 秒", store_name, self.request_timeout_seconds)
            packet = self._wait_for_target_packet(listener, store_name)
            payload = self._packet_json(packet, store_name)
            fields, summary = self._extract_today(payload, store_name)
            LOGGER.info(
                "[美客多][广告完成] 店铺=%s，广告字段=%s，响应 advertiser_id=%s，响应日期=%s",
                store_name,
                fields,
                payload.get("advertiser_id", ""),
                payload.get("date_to", ""),
            )
            return fields, summary
        except TimeoutError as exc:
            LOGGER.warning("[美客多][广告主接口超时] 店铺=%s，%s，进入周期选择备用监听流程", store_name, exc)
            try:
                listener.stop()
            except Exception as stop_exc:
                LOGGER.warning("[美客多][广告监听] 店铺=%s，主监听停止失败，仍尝试备用流程：%s", store_name, stop_exc)
            return self._collect_fallback(tab, store_name)
        finally:
            try:
                listener.stop()
            except Exception as exc:
                LOGGER.warning("[美客多][广告监听] 店铺=%s，停止监听失败：%s", store_name, exc)

    def _collect_fallback(self, tab: Any, store_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """主 chart 接口没有返回时，通过页面周期选择触发 campaigns/search 请求。"""
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
        listener.start(targets=target_pattern, is_regex=True, method="GET", res_type=("XHR", "Fetch"))
        try:
            self._click_element(period_button, tab, "打开广告周期菜单")
            custom = self._wait_element(tab, CUSTOM_PERIOD_XPATH, 10.0)
            if custom is None:
                raise TimeoutError("美客多广告备用流程未找到自定义时间按钮")
            self._click_element(custom, tab, "选择自定义时间")
            today = self._wait_element(tab, TODAY_CALENDAR_XPATH, 10.0)
            if today is None:
                raise TimeoutError("美客多广告备用流程未找到今天日期按钮")
            self._click_element(today, tab, "选择今天日期")
            packet = self._wait_for_fallback_packet(listener, store_name)
            payload = self._packet_json(packet, store_name)
            fields, summary = self._extract_fallback(payload, store_name)
            LOGGER.info("[美客多][广告备用完成] 店铺=%s，活动字段=%s", store_name, fields)
            return fields, summary
        finally:
            try:
                listener.stop()
            except Exception as exc:
                LOGGER.warning("[美客多][广告备用监听] 店铺=%s，停止监听失败：%s", store_name, exc)

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

    def _wait_for_fallback_packet(self, listener: Any, store_name: str) -> Any:
        deadline = time.monotonic() + self.fallback_request_timeout_seconds
        while time.monotonic() < deadline:
            packet = listener.wait(timeout=min(2.0, max(0.2, deadline - time.monotonic())), raise_err=False)
            if not packet:
                continue
            url = str(getattr(packet, "url", "") or "")
            if self._matches_fallback_url(url):
                LOGGER.info("[美客多][广告备用命中] 店铺=%s，已命中当天 campaigns/search 响应", store_name)
                return packet
            LOGGER.debug("[美客多][广告备用忽略] 店铺=%s，非当天目标请求：%s", store_name, self._safe_url(url))
        raise TimeoutError(f"美客多广告备用接口在 {self.fallback_request_timeout_seconds:.1f} 秒内未返回")

    @classmethod
    def _matches_fallback_url(cls, url: str) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc.casefold() != "pa.mercadolivre.com.br" or not FALLBACK_PATH_RE.match(parsed.path):
            return False
        query = parse_qs(parsed.query, keep_blank_values=True)
        today = datetime.now(ZoneInfo("America/Sao_Paulo")).date().isoformat()
        return (
            query.get("dateFrom", [""])[0] == today
            and query.get("dateTo", [""])[0] == today
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

    def _wait_for_target_packet(self, listener: Any, store_name: str) -> Any:
        deadline = time.monotonic() + self.request_timeout_seconds
        while time.monotonic() < deadline:
            remaining = max(0.2, deadline - time.monotonic())
            packet = listener.wait(timeout=min(2.0, remaining), raise_err=False)
            if not packet:
                continue
            url = str(getattr(packet, "url", "") or "")
            if self._matches_target_url(url):
                LOGGER.info("[美客多][广告命中] 店铺=%s，已命中目标 chart 接口，host/path/query 已校验", store_name)
                return packet
            LOGGER.debug("[美客多][广告忽略] 店铺=%s，监听到非目标请求：%s", store_name, self._safe_url(url))
        raise TimeoutError(f"美客多广告目标 chart 接口在 {self.request_timeout_seconds:.1f} 秒内未返回")

    @staticmethod
    def _matches_target_url(url: str) -> bool:
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
        products = query.get("products", [""])[0].replace("%2C", ",").upper()
        return {part.strip() for part in products.split(",")} == {"PADS", "DADS"}

    @staticmethod
    def _packet_json(packet: Any, store_name: str) -> dict[str, Any]:
        response = getattr(packet, "response", None)
        if response is None:
            raise RuntimeError(f"美客多广告目标请求没有响应体：{store_name}")
        status = getattr(response, "status", None)
        if status is not None:
            try:
                if not 200 <= int(status) < 300:
                    raise RuntimeError(f"美客多广告目标请求返回 HTTP {status}")
            except (TypeError, ValueError):
                LOGGER.warning("[美客多][广告响应] 无法解析 HTTP 状态码=%r，继续尝试读取 JSON", status)
        body = getattr(response, "body", None)
        if isinstance(body, dict):
            return body
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8", errors="replace")
        if not isinstance(body, str) or not body.strip():
            raw_body = getattr(response, "raw_body", b"")
            if isinstance(raw_body, (bytes, bytearray)):
                body = raw_body.decode("utf-8", errors="replace")
            else:
                body = str(raw_body or "")
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"美客多广告目标响应不是有效 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("美客多广告目标响应 JSON 根节点不是对象")
        return payload

    @classmethod
    def _extract_today(cls, payload: dict[str, Any], store_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        today = datetime.now(ZoneInfo("America/Sao_Paulo")).date().isoformat()
        data = payload.get("data")
        summary_by_date = data.get("summary_by_date") if isinstance(data, dict) else None
        if not isinstance(summary_by_date, dict):
            raise RuntimeError("美客多广告响应缺少 data.summary_by_date")

        fields: dict[str, Any] = {}
        summary: dict[str, Any] = {"advertiser_id": payload.get("advertiser_id", ""), "date": today}
        for response_key, field_name in AD_METRIC_FIELDS:
            entries = summary_by_date.get(response_key)
            if not isinstance(entries, list) or not entries:
                LOGGER.warning("[美客多][广告字段缺失] 店铺=%s，接口字段=%s，无日期数组", store_name, response_key)
                continue
            # 接口按日期升序返回；取最后一条，并记录日期，符合当前接口 dateTo=巴西今天的约定。
            selected = entries[-1] if isinstance(entries[-1], dict) else {}
            selected_date = str(selected.get("date") or "")
            if selected_date != today:
                LOGGER.warning("[美客多][广告日期] 店铺=%s，字段=%s，最后日期=%s，巴西今天=%s，仍按接口最后一条读取", store_name, response_key, selected_date, today)
            value = selected.get("total", "")
            fields[field_name] = value
            summary[response_key] = {"date": selected_date, "total": value}
        return fields, summary

    @staticmethod
    def _safe_url(url: str) -> str:
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.netloc else "<空网址>"
