"""美客多广告概览页的日期操作与页面指标兜底。"""

from __future__ import annotations

import logging
import math
import random
import re
import time
import unicodedata
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from daily_store_check.human_interaction import HumanInteraction


LOGGER = logging.getLogger(__name__)


class MercadoAdPopupError(RuntimeError):
    """广告弹窗未能安全关闭，不能当成 chart 接口异常触发另一套日期流程。"""

PERIOD_XPATH = "//button[@id='DateRange-trigger']"
MENU_XPATH = "//ul[@id='DateRange-menu-list']"
CUSTOM_XPATH = "//li[@id='DateRange-menu-list-option-custom' and @data-key='custom']"
CALENDAR_XPATH = "//div[@id='DateRange-custom-datepicker']"

# 预算推荐弹窗的动态 id 和按钮文字会随店铺、浏览器翻译改变。
# 以预算选项和 mute 拒绝按钮限定目标，绝不点击 loud 的“调整预算”。
AD_TEMPLATE_DIALOG_XPATH = (
    "//div[@role='dialog' and @aria-modal='true' "
    "and contains(concat(' ', normalize-space(@class), ' '), ' modal-template ')]"
)
BUDGET_POPUP_XPATH = (
    AD_TEMPLATE_DIALOG_XPATH
    + "[.//*[@data-testid='modal-template-content']//input[@type='radio' and @name='capability_selection']]"
)
BUDGET_POPUP_DISMISS_XPATH = (
    BUDGET_POPUP_XPATH
    + "//*[@data-testid='modal-template-content'][.//input[@type='radio' and @name='capability_selection']]"
    "//button[@type='button' and @data-andes-button-hierarchy='mute' "
    "and not(@disabled) and not(@aria-disabled='true')]"
)

# 金额对应收入和投资额；中文界面的“销售额”卡片实际对应接口中的销量。
DOM_METRICS: tuple[tuple[str, str, str], ...] = (
    ("investment", "今天广告成本", "currency"),
    ("income", "今天广告销售额", "currency"),
    ("sales", "今天广告销售量", "integer"),
    ("followers", "今天广告新粉丝数", "integer"),
    ("visits", "今天广告访客数", "integer"),
)


class MercadoAdPage:
    """只操作浏览器页面，不主动发送接口请求。"""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        human_config: dict[str, Any] | None = None,
        parser: Callable[[str, str], Any] | None = None,
    ):
        values = config if isinstance(config, dict) else {}
        self.human = HumanInteraction(human_config or values.get("human_interaction", {}))
        self.parser = parser
        self.ui_timeout_seconds = self._number(values.get("ui_timeout_seconds"), 20.0, 0.1)
        self.dom_timeout_seconds = self._number(values.get("dom_timeout_seconds"), 60.0, 0.1)
        self.popup_close_timeout_seconds = self._number(values.get("popup_close_timeout_seconds"), 5.0, 0.1)
        self.double_click_min_ms = self._number(values.get("fallback_double_click_min_interval_ms"), 80.0)
        self.double_click_max_ms = self._number(values.get("fallback_double_click_max_interval_ms"), 180.0)
        if self.double_click_min_ms > self.double_click_max_ms:
            self.double_click_min_ms, self.double_click_max_ms = self.double_click_max_ms, self.double_click_min_ms

    @staticmethod
    def _number(value: Any, default: float, minimum: float = 0.0) -> float:
        try:
            result = float(value)
            return max(minimum, result) if math.isfinite(result) else default
        except (TypeError, ValueError):
            return default

    @classmethod
    def is_overview_page(cls, tab: Any) -> bool:
        """使用概览页专属控件识别新页面，避免误操作旧活动列表的周期按钮。"""
        try:
            current = urlsplit(str(tab.url or ""))
            if current.hostname == "vendedores.mercadolivre.com.br" and current.path.rstrip("/") == "/publicidade/resumo-anunciante":
                # 控件可能尚未渲染，具体可见等待交给 select_today，避免瞬时读取误判页面类型。
                return True
        except Exception:
            pass
        return bool(cls._elements(tab, PERIOD_XPATH)) or bool(
            cls._elements(tab, "//*[@data-testid='general-metrics-chart']")
        )

    def select_today(
        self,
        tab: Any,
        store_name: str,
        today: str,
        before_date_click: Callable[[], Any],
    ) -> None:
        """选择巴西今天；点击日期之前由调用者清空并重新准备网络监听。"""
        date.fromisoformat(today)
        self._click(tab, PERIOD_XPATH, store_name, "打开广告概览周期菜单")
        self._wait_unique(tab, MENU_XPATH, "广告概览周期菜单")
        self._click(tab, CUSTOM_XPATH, store_name, "选择广告概览自定义周期")
        self._wait_unique(tab, CALENDAR_XPATH, "广告概览日期选择面板")
        date_xpath = self._today_xpath(today)
        self._navigate_month(tab, store_name, today, date_xpath)
        self._wait_unique(tab, date_xpath, f"巴西今天日期 {today}")
        LOGGER.info("[美客多][广告概览日期] 店铺=%s，准备监听后双击巴西日期=%s", store_name, today)
        before_date_click()
        self._double_click_date(tab, date_xpath, store_name)
        self._wait_today_range(tab, store_name, today)

    def close_budget_popup(self, tab: Any, store_name: str, *, wait_seconds: float = 0.0) -> bool:
        """仅拒绝广告预算推荐，确认整个弹窗消失；不操作浏览器网络监听。"""
        appearance_deadline = time.monotonic() + max(0.0, wait_seconds)
        query_deadline = max(appearance_deadline, time.monotonic() + self.popup_close_timeout_seconds)
        while True:
            dialogs = self._popup_elements(tab, BUDGET_POPUP_XPATH)
            if dialogs is None:
                if time.monotonic() >= query_deadline:
                    raise MercadoAdPopupError("美客多广告弹窗查询持续失败，无法确认页面是否被弹窗遮挡")
                time.sleep(0.2)
                continue
            if dialogs:
                if len(dialogs) != 1:
                    raise MercadoAdPopupError(f"美客多预算推荐弹窗匹配到 {len(dialogs)} 个可见目标，停止点击")
                break
            if time.monotonic() >= appearance_deadline:
                return False
            time.sleep(min(0.2, max(0.0, appearance_deadline - time.monotonic())))

        LOGGER.info("[美客多][广告弹窗] 店铺=%s，发现预算推荐弹窗，准备点击“错过这个机会”", store_name)
        last_error = "弹窗未消失"
        for attempt in range(1, 4):
            buttons = self._popup_elements(tab, BUDGET_POPUP_DISMISS_XPATH)
            if buttons is not None and len(buttons) > 1:
                raise MercadoAdPopupError(f"美客多预算推荐弹窗拒绝按钮匹配到 {len(buttons)} 个可见目标，停止点击")
            if buttons:
                try:
                    self.human.click_element(tab, buttons[0])
                    LOGGER.info("[美客多][广告弹窗点击] 店铺=%s，第%d/3次，已派发拒绝按钮点击，等待整个弹窗关闭", store_name, attempt)
                except Exception as exc:
                    last_error = f"点击异常={type(exc).__name__}"
                    LOGGER.warning("[美客多][广告弹窗点击重试] 店铺=%s，第%d/3次，%s", store_name, attempt, last_error)
            else:
                last_error = "拒绝按钮暂不可用，继续核对弹窗是否已关闭"

            deadline = time.monotonic() + self.popup_close_timeout_seconds
            absent_checks = 0
            while time.monotonic() < deadline:
                # 不能只等按钮消失：按钮可能先被替换，但模态框与遮罩仍挡住页面。
                # 查询异常也不能当作关闭成功；只有连续两次确认无可见模态框才继续。
                visible_dialogs = self._popup_elements(tab, AD_TEMPLATE_DIALOG_XPATH)
                absent_checks = absent_checks + 1 if visible_dialogs == [] else 0
                if absent_checks >= 2:
                    LOGGER.info("[美客多][广告弹窗已关闭] 店铺=%s，第%d/3次，已确认弹窗消失", store_name, attempt)
                    return True
                time.sleep(0.2)
            LOGGER.warning("[美客多][广告弹窗关闭重试] 店铺=%s，第%d/3次，%.1f秒内未确认关闭，%s", store_name, attempt, self.popup_close_timeout_seconds, last_error)
        raise MercadoAdPopupError("美客多广告预算推荐弹窗连续3次未确认关闭，停止后续页面操作")

    @staticmethod
    def _popup_elements(tab: Any, xpath: str) -> list[Any] | None:
        """None 表示页面重绘等查询异常，不能用来确认弹窗已消失。"""
        try:
            visible = []
            for element in tab.eles(f"xpath:{xpath}", timeout=0):
                if not element.states.is_displayed:
                    continue
                geometry_check = getattr(element, "run_js", None)
                # DrissionPage 的 is_displayed 不检查 display:none 的祖先。
                # 用真实布局确认，避免把保留在隐藏父容器内的弹窗当成仍可见。
                if callable(geometry_check) and geometry_check("""
                    const rect = this.getBoundingClientRect();
                    const style = getComputedStyle(this);
                    return rect.width > 0 && rect.height > 0 && this.getClientRects().length > 0
                        && style.display !== 'none' && style.visibility !== 'hidden'
                        && style.visibility !== 'collapse';
                """) is False:
                    continue
                visible.append(element)
            return visible
        except Exception:
            return None

    @staticmethod
    def _today_xpath(today: str) -> str:
        # 完整日期由调用者按巴西时区计算，不使用浏览器或本机的“今天”标记。
        return (
            f"{CALENDAR_XPATH}//td[@data-day='{today}' and not(@data-disabled='true') "
            "and not(@data-hidden='true')]//button[not(@disabled)]"
        )

    def _navigate_month(self, tab: Any, store_name: str, today: str, date_xpath: str) -> None:
        """根据真实日历单元格的完整日期判断月份，按需点击前后月按钮。"""
        deadline = time.monotonic() + self.ui_timeout_seconds
        target_month = today[:7]
        previous_month: str | None = None
        while time.monotonic() < deadline:
            if len(self._elements(tab, date_xpath)) == 1:
                return
            cells = self._elements(
                tab, f"{CALENDAR_XPATH}//td[@data-day and not(@data-hidden='true') and not(@data-outside='true')]"
            )
            months = {str(cell.attr("data-day") or "")[:7] for cell in cells}
            months = {month for month in months if re.fullmatch(r"\d{4}-\d{2}", month)}
            if not months:
                time.sleep(0.25)
                continue
            if target_month in months:
                # 当前月份已有目标日期；若今天不可点击，继续等待控件加载，不改点其他日期。
                time.sleep(0.25)
                continue
            current_month = min(months)
            if current_month == previous_month:
                time.sleep(0.25)
                continue
            previous_month = current_month
            direction = "previous" if target_month < current_month else "next"
            xpath = (
                f"{CALENDAR_XPATH}//button[contains(@class,'andes-datepicker__button--{direction}') "
                "and not(@disabled)]"
            )
            self._click(tab, xpath, store_name, f"日期面板切换月份至 {target_month}")
            time.sleep(0.25)
        raise TimeoutError(f"美客多广告概览日期面板在 {self.ui_timeout_seconds:.1f} 秒内未出现可点击的 {today}")

    def _click(self, tab: Any, xpath: str, store_name: str, step: str) -> None:
        """点击前重新定位，避免日期菜单重绘造成旧节点失效。"""
        last_error: Exception | None = None
        for attempt in range(1, 4):
            self.close_budget_popup(tab, store_name)
            element = self._wait_unique(tab, xpath, step)
            try:
                element.scroll.to_see(center=True)
            except Exception:
                pass
            try:
                self.human.click_element(tab, element)
                LOGGER.info("[美客多][广告概览点击] 店铺=%s，步骤=%s，第%d次已点击", store_name, step, attempt)
                return
            except Exception as exc:
                last_error = exc
                LOGGER.warning("[美客多][广告概览点击重试] 店铺=%s，步骤=%s，第%d次异常=%s", store_name, step, attempt, exc)
        raise RuntimeError(f"美客多广告概览{step}点击失败：{last_error}")

    def _double_click_date(self, tab: Any, xpath: str, store_name: str) -> None:
        """先完成仿真人移入，再以短间隔点击两次；第二次重新获取节点。"""
        self.close_budget_popup(tab, store_name)
        first = self._wait_unique(tab, xpath, "广告概览今天日期")
        point = self._click_point(first)
        self.human.move_to_point(tab, *point)
        if self.human.enabled:
            time.sleep(random.uniform(self.human.settings.click_pause_min_seconds, self.human.settings.click_pause_max_seconds))
        self._dispatch_click(tab, point, 1)
        first_click_finished = time.monotonic()
        interval_ms = random.uniform(self.double_click_min_ms, self.double_click_max_ms)
        # 首次点击会重绘日期单元格，不能沿用第一次的元素对象。
        matches = self._elements(tab, xpath)
        if len(matches) != 1:
            raise RuntimeError(f"美客多广告概览双击时未找到唯一今天日期节点，可见匹配数={len(matches)}")
        second = matches[0]
        current_point = self._click_point(second, preferred=point)
        if current_point != point:
            # 日期重绘时通常坐标不变；若布局确实改变，停止本轮，避免把双击发到错误位置。
            raise RuntimeError("美客多广告概览双击时今天日期按钮位置发生变化，请重新选择日期")
        # 节点重定位也会耗时，只补足剩余间隔，避免在 80～180ms 外再叠加完整等待。
        remaining = interval_ms / 1000.0 - (time.monotonic() - first_click_finished)
        if remaining > 0:
            time.sleep(remaining)
        self._dispatch_click(tab, current_point, 2)
        LOGGER.info("[美客多][广告概览双击] 店铺=%s，两次点击完成，配置随机间隔=%.1fms", store_name, interval_ms)

    @staticmethod
    def _click_point(element: Any, preferred: tuple[float, float] | None = None) -> tuple[float, float]:
        left, top = (float(value) for value in element.rect.viewport_location)
        width, height = (float(value) for value in element.rect.size)
        if width <= 0 or height <= 0:
            raise RuntimeError("美客多广告日期按钮没有可点击的尺寸")
        if preferred and left + 1 <= preferred[0] <= left + width - 1 and top + 1 <= preferred[1] <= top + height - 1:
            return preferred
        return left + width * random.uniform(0.4, 0.6), top + height * random.uniform(0.4, 0.6)

    @staticmethod
    def _dispatch_click(tab: Any, point: tuple[float, float], click_count: int) -> None:
        tab.run_cdp("Input.dispatchMouseEvent", type="mousePressed", x=point[0], y=point[1], button="left", buttons=1, clickCount=click_count)
        tab.run_cdp("Input.dispatchMouseEvent", type="mouseReleased", x=point[0], y=point[1], button="left", buttons=0, clickCount=click_count)

    @staticmethod
    def _elements(tab: Any, xpath: str) -> list[Any]:
        try:
            return [element for element in tab.eles(f"xpath:{xpath}", timeout=0) if element.states.is_displayed]
        except Exception:
            return []

    def _wait_unique(self, tab: Any, xpath: str, name: str, timeout_seconds: float | None = None) -> Any:
        timeout = self.ui_timeout_seconds if timeout_seconds is None else timeout_seconds
        deadline = time.monotonic() + timeout
        last_count = 0
        while time.monotonic() < deadline:
            elements = self._elements(tab, xpath)
            last_count = len(elements)
            if last_count == 1:
                return elements[0]
            time.sleep(0.1)
        raise TimeoutError(f"美客多在 {timeout:.1f} 秒内未找到唯一可见的{name}，可见匹配数={last_count}")

    def _wait_today_range(self, tab: Any, store_name: str, today: str) -> None:
        deadline = time.monotonic() + self.ui_timeout_seconds
        last_range = ""
        while time.monotonic() < deadline:
            last_range = self._range_text(tab)
            if self._is_today_range(last_range, today):
                LOGGER.info("[美客多][广告概览日期确认] 店铺=%s，已确认巴西单日=%s，标签=%r", store_name, today, last_range)
                return
            time.sleep(0.25)
        raise TimeoutError(f"美客多广告概览在 {self.ui_timeout_seconds:.1f} 秒内未确认 {today} 单日范围，实际标签={last_range!r}")

    @classmethod
    def _range_text(cls, tab: Any) -> str:
        elements = cls._elements(tab, "//span[@id='DateRange-display-values']")
        if len(elements) == 1:
            return str(elements[0].text or "").strip()
        return ""

    @staticmethod
    def _is_today_range(text: str, today: str) -> bool:
        """只承认起止日期均为今天；支持页面实际使用的葡语、英语与中文日期。"""
        normalized = unicodedata.normalize("NFKD", str(text).lower())
        normalized = "".join(char for char in normalized if not unicodedata.combining(char))
        iso_dates = re.findall(r"\d{4}-\d{2}-\d{2}", normalized)
        if iso_dates:
            return len(iso_dates) == 2 and iso_dates == [today, today]
        chinese_dates = re.findall(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", normalized)
        if chinese_dates:
            parsed = [f"{int(year):04d}-{int(month):02d}-{int(day):02d}" for year, month, day in chinese_dates]
            return len(parsed) == 2 and parsed == [today, today]
        months = {"jan": 1, "feb": 2, "fev": 2, "mar": 3, "apr": 4, "abr": 4, "may": 5, "mai": 5, "jun": 6, "jul": 7, "aug": 8, "ago": 8, "sep": 9, "set": 9, "oct": 10, "out": 10, "nov": 11, "dec": 12, "dez": 12}
        pieces = re.findall(r"(\d{1,2})\s+(?:de\s+)?([a-z]+)\.?\s+(?:de\s+)?(\d{4})", normalized)
        parsed = []
        for day, month_name, year in pieces:
            month = months.get(month_name[:3])
            if month is None:
                return False
            parsed.append(f"{int(year):04d}-{month:02d}-{int(day):02d}")
        return len(parsed) == 2 and parsed == [today, today]

    def read_today_metrics(self, tab: Any, store_name: str, today: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """单日范围确认后等待五项值稳定，只读取业务卡片自身文本。"""
        date.fromisoformat(today)
        deadline = time.monotonic() + self.dom_timeout_seconds
        previous: tuple[tuple[str, str], ...] | None = None
        stable = 0
        stable_since: float | None = None
        raw_values: dict[str, str] = {}
        fields: dict[str, Any] = {}
        while time.monotonic() < deadline:
            self.close_budget_popup(tab, store_name)
            snapshot = self._snapshot(tab)
            raw_values = snapshot.get("values", {}) if isinstance(snapshot, dict) else {}
            range_text = snapshot.get("range", "") if isinstance(snapshot, dict) else ""
            fields = self._convert(raw_values)
            ready = (
                isinstance(snapshot, dict)
                and snapshot.get("ready_state") == "complete"
                and not snapshot.get("loading", True)
                and self._is_today_range(str(range_text), today)
                and len(fields) == len(DOM_METRICS)
            )
            current = tuple(sorted(raw_values.items()))
            stable = stable + 1 if ready and current == previous else (1 if ready else 0)
            if stable == 1:
                stable_since = time.monotonic()
            elif not ready:
                stable_since = None
            previous = current if ready else None
            LOGGER.info("[美客多][广告页面等待] 店铺=%s，巴西日期=%s，有效指标=%d/5，加载中=%s，稳定次数=%d/3", store_name, today, len(fields), snapshot.get("loading", True), stable)
            if stable >= 3 and stable_since is not None and time.monotonic() - stable_since >= 3.0:
                LOGGER.info("[美客多][广告页面读取完成] 店铺=%s，单日标签=%r，指标=%s", store_name, range_text, fields)
                return fields, {
                    "source": "page", "date": today, "currency": "BRL", "today_brazil": today,
                    "date_from": today, "date_to": today, "range_label": range_text,
                    "raw_metrics": raw_values, "metrics": fields,
                    "missing_metrics": ["prints", "clicks"],
                    "metric_units": {"investment": "BRL", "income": "BRL", "sales": "件", "followers": "人", "visits": "次"},
                }
            time.sleep(1.0)
        raise TimeoutError(f"美客多广告页面在 {self.dom_timeout_seconds:.1f} 秒内未确认今天五项稳定指标，已读取 {len(fields)}/5 项")

    @staticmethod
    def _snapshot(tab: Any) -> dict[str, Any]:
        """在同一轮页面脚本中读取日期、加载状态和五个原始值，避免跨轮混合。"""
        try:
            result = tab.run_js("""
                const shown = node => !!node && node.getClientRects().length > 0
                    && getComputedStyle(node).visibility !== 'hidden'
                    && getComputedStyle(node).display !== 'none';
                const root = document.querySelector('[data-testid="general-metrics-chart"]');
                const range = document.getElementById('DateRange-display-values');
                const values = {};
                for (const key of ['investment', 'income', 'sales', 'followers', 'visits']) {
                    const nodes = root ? [...root.querySelectorAll('.' + key + '-chart-metric .adv-charts-summary-cards__summary-value')].filter(shown) : [];
                    if (nodes.length !== 1) continue;
                    // 排除子元素中的涨跌百分比，只取指标自身的文本节点。
                    const text = [...nodes[0].childNodes].filter(node => node.nodeType === Node.TEXT_NODE)
                        .map(node => node.textContent).join(' ').replace(/\\s+/g, ' ').trim();
                    if (text) values[key] = text;
                }
                const loading = !root || [...root.querySelectorAll('[aria-busy="true"], .andes-spinner, [class*="skeleton"], [data-testid*="loading"]')].some(shown);
                return {ready_state: document.readyState, loading, range: shown(range) ? range.textContent.trim() : '', values};
            """)
            return result if isinstance(result, dict) else {"loading": True, "values": {}}
        except Exception:
            return {"loading": True, "values": {}}

    def _convert(self, raw_values: dict[str, str]) -> dict[str, Any]:
        parser = self.parser
        if parser is None:
            # 在读取时延迟导入，避免爬虫与广告采集模块互相导入形成循环。
            from mercado.MKD_auto import MercadoAuto

            parser = MercadoAuto._format_value
        fields: dict[str, Any] = {}
        for key, field, kind in DOM_METRICS:
            raw = str(raw_values.get(key, "") or "").strip()
            if not raw or not re.search(r"\d", raw):
                continue
            value = parser(raw, kind)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                continue
            fields[field] = value
        return fields
