"""Shopee 店铺广告自动化和数据采集。

本文件独立维护 Shopee 的 DrissionPage 连接、URL 登录判断、广告页跳转、URL周期切换、
指标读取、数值转换、日志和结果组装。程序只接管紫鸟已经打开的当前标签页。

尚未提供的 XPath 统一留在本文件顶部。XPath 为空、元素不存在或转换失败时，
对应指标返回空值并记录日志，不会影响其他指标继续执行。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from DrissionPage import Chromium


LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 一、Shopee 广告页面 URL、等待参数和弹窗 XPath
# ---------------------------------------------------------------------------

# Shopee 登录页和卖家中心域名。URL 查询参数不参与判断。
# 如果当前 URL 中包含 login，视为需要执行登录按钮点击流程。
SHOPEE_LOGIN_PAGE_URL = "https://accounts.shopee.com.br/seller/login"
SHOPEE_SELLER_HOST = "seller.shopee.com.br"

# Shopee 登录页的“Entrar”按钮。按钮点击后等待 URL 回到 seller.shopee.com.br，
# 再等待首页 document.readyState=complete，才继续进入广告页。
SHOPEE_LOGIN_BUTTON_XPATH = "//form/button"

# 已确认登录后直接打开广告页，不再点击首页的营销中心或 Shopee 广告菜单。
SHOPEE_AD_PAGE_URL = "https://seller.shopee.com.br/portal/marketing/pas/index"

# 商业分析四个页面入口。页面中的 API 和 DOM 可能随 Shopee 前端版本变化，
# 这里只固定路由，指标由页面当前渲染的业务标题和表格列动态读取。
SHOPEE_DATA_CENTER_URLS: dict[str, str] = {
    "商业分析概述": "https://seller.shopee.com.br/datacenter/overview",
    "商品概述": "https://seller.shopee.com.br/datacenter/product/overview",
    "商品流量": "https://seller.shopee.com.br/datacenter/product/traffic",
    "流量概述": "https://seller.shopee.com.br/datacenter/traffic/overview",
}
EXTRA_PERIODS: tuple[str, ...] = ("昨天", "7天", "今天")
EXTRA_PERIOD_LABELS: dict[str, str] = {"昨天": "昨天", "7天": "过去7 天", "今天": "今日实时"}
EXTRA_PAGE_WAIT_SECONDS = 5
# 商业分析是单页应用，document.readyState 完成后仍会异步请求和渲染数据。
DATA_CENTER_RENDER_TIMEOUT_SECONDS = 120
DATA_CENTER_RENDER_POLL_SECONDS = 1
DATA_CENTER_RENDER_EXTRA_WAIT_SECONDS = 3
DATA_CENTER_RENDER_SELECTORS: dict[str, tuple[str, int]] = {
    "商业分析概述": (".dashboard-key-metric-group .key-metric", 7),
    "商品概述": (".product-overview .key-metric", 20),
    "商品流量": (".traffic-sources-list tbody tr", 1),
    "流量概述": (".metric-item", 8),
}

# 广告页单次等待 document.readyState=complete 的最长时间，单位为秒。
# 超过 120 秒仍未完成时，本次加载判定失败，刷新当前广告页后重新尝试。
AD_PAGE_LOAD_TIMEOUT_SECONDS = 120

# 广告页加载失败后允许刷新的次数。值为 4 表示首次打开 1 次，失败后最多再刷新 4 次，
# 因此广告入口最多执行 5 轮加载尝试。
AD_PAGE_LOAD_RETRY_TIMES = 4

# 昨日和最近7天页面的数据加载成功标志。document.readyState=complete 后还必须读取到
# 该元素的非空文本，才能确认 Shopee 广告数据已经渲染。
AD_PAGE_READY_METRIC_XPATH = '//div[@class="line-metrics"]/div[1]//div[@class="content"]//span'

# 广告入口文档加载完成后，每隔2秒读取一次当前网址，最多读取40次。
# 只有 URL 的 group 参数变成已知时间范围后，才使用该完整 URL 生成昨日和最近7天地址。
AD_GROUP_URL_CHECK_TIMES = 40
AD_GROUP_URL_CHECK_INTERVAL_SECONDS = 2
AD_KNOWN_GROUP_VALUES: frozenset[str] = frozenset({"today", "yesterday", "last_week", "last_month"})

# 昨日/最近7天页面首次加载失败后允许刷新的次数。值为5表示首次打开1次加刷新5次，
# 每个时间范围最多进行6轮“文档完成 + 首个指标文本验证”。
PERIOD_PAGE_REFRESH_RETRY_TIMES = 5

# Shopee 流量验证错误页。只比较域名和路径，忽略 home_url、tracking_id 等动态参数。
SHOPEE_TRAFFIC_ERROR_URL = "https://shopee.com.br/verify/traffic/error"

# 内部周期名称与 Shopee URL group 参数的固定映射，不再点击页面日期按钮。
PERIOD_GROUP_VALUES: dict[str, str] = {
    "昨天": "yesterday",
    "7天": "last_week",
    "今天": "today",
}

# 紫鸟刚打开店铺时 URL 可能还在 chrome://newtab 或重定向中。
# 最长观察 60 秒，期间一旦进入登录页或 seller.shopee.com.br 就立即作出判断。
SHOPEE_URL_STATE_TIMEOUT_SECONDS = 60

# 登录按钮点击后等待回到卖家中心首页的最长时间，单位为秒。
# 页面可能先提交表单、再经过多次重定向，因此这里单独保留一个可调整参数。
SHOPEE_LOGIN_HOME_TIMEOUT_SECONDS = 60

# 登录按钮首次点击失败后允许再次尝试的次数。总尝试次数为 1 + 此值。
SHOPEE_LOGIN_CLICK_RETRY_TIMES = 3

# Shopee 广告弹窗关闭按钮按顺序检查：先使用现有奖励弹窗定位，找不到时再检查广告升级通知弹窗。
# 后续如果出现更多类型，只需在列表末尾追加 XPath，不需要修改关闭函数。
AD_POPUP_CLOSE_XPATHS: list[str] = [
    (
        '//div[contains(@class,"eds-modal__mask") and not(contains(@style,"display: none"))]//i[contains(@class,"eds-modal__close")]'
    ),
    (
        '//div[contains(@class,"eds-modal__box") and contains(@class,"rewards-homepage-prompt")]'
        '//i[contains(@class,"eds-modal__close")]'
    ),
    (
        '//div[contains(@class,"shop-ads-upgrade-pre-notice-modal")]'
        '/ancestor::div[contains(@class,"eds-modal__box")]'
        '//i[contains(@class,"eds-modal__close")]'
    )
]

# 关闭一层广告弹窗后继续观察的时间，防止第二层弹窗稍晚渲染而被误判为全部关闭。
AD_POPUP_CHAIN_WAIT_SECONDS = 2

# 页面主文档加载完成后额外等待的时间，单位为秒。
# 这 10 秒用于等待 Shopee 广告数据、弹窗和异步页面内容继续渲染。
AFTER_PAGE_READY_WAIT_SECONDS = 10

# 普通按钮首次点击失败后允许再次重试的次数。
# 当前值为 3，表示“首次点击 1 次 + 失败后重试 3 次”。这里只用于广告弹窗关闭。
CLICK_RETRY_TIMES = 3

# 同一个按钮前后两次点击尝试之间的最短间隔，单位为秒。
# 实际重试时会在 2 秒基础上增加 0～1 秒随机等待，避免连续机械点击。
CLICK_RETRY_INTERVAL_SECONDS = 2

# ---------------------------------------------------------------------------
# 二、Shopee 广告 ALL 行指标
# ---------------------------------------------------------------------------

# kind 可选值：
# integer=整数；percent=页面百分数转换成 0~1 的数值比例；
# currency=巴西雷亚尔两位小数；decimal=普通两位小数。
# 同一指标在昨天和 7 天页面通常使用相同 XPath，但仍分别保留，方便页面差异化维护。
# 用户补充 XPath 时，金额使用 kind=currency，数量使用 kind=integer，百分比使用 kind=percent。
METRIC_SPECS: list[dict[str, str]] = [
    {"period": "昨天", "field": "昨天ALL展示次数", "xpath": '//div[@class="line-metrics"]/div[1]//div[@class="content"]//span', "kind": "integer"},
    {"period": "昨天", "field": "昨天ALL点击数", "xpath": '//div[@class="line-metrics"]/div[2]//div[@class="content"]//span', "kind": "integer"},
    {"period": "昨天", "field": "昨天ALL点击率", "xpath": '//div[@class="line-metrics"]/div[3]//div[@class="content"]//span', "kind": "percent"},
    {"period": "昨天", "field": "昨天ALL订单量", "xpath": '//div[@class="line-metrics"]/div[4]//div[@class="content"]//span', "kind": "integer"},
    {"period": "昨天", "field": "昨天ALL商品已出售", "xpath": '//div[@class="line-metrics"]/div[5]//div[@class="content"]//span', "kind": "integer"},
    {"period": "昨天", "field": "昨天ALL销售额", "xpath": '//div[@class="line-metrics"]/div[6]//div[@class="content"]//span', "kind": "currency", "currency_code": "BRL"},
    {"period": "昨天", "field": "昨天ALL优惠价金额", "xpath": '//div[@class="line-metrics"]/div[9]//div[@class="content"]//span', "kind": "currency", "currency_code": "BRL"},
    {"period": "昨天", "field": "昨天ALL优惠劵带来销售额", "xpath": '//div[@class="line-metrics"]/div[10]//div[@class="content"]//span', "kind": "currency", "currency_code": "BRL"},
    {"period": "昨天", "field": "昨天ALL加购次数", "xpath": '//div[@class="line-metrics"]/div[11]//div[@class="content"]//span', "kind": "integer"},
    {"period": "昨天", "field": "昨天ALL加购率", "xpath": '//div[@class="line-metrics"]/div[12]//div[@class="content"]//span', "kind": "percent"},
    {"period": "昨天", "field": "昨天ALL花费", "xpath": '//div[@class="line-metrics"]/div[7]//div[@class="content"]//span', "kind": "currency", "currency_code": "BRL"},
    {"period": "昨天", "field": "昨天ALL广告支出回报率", "xpath": '//div[@class="line-metrics"]/div[8]//div[@class="content"]//span', "kind": "decimal"},
    {"period": "7天", "field": "7天ALL展示次数", "xpath": '//div[@class="line-metrics"]/div[1]//div[@class="content"]//span', "kind": "integer"},
    {"period": "7天", "field": "7天ALL点击数", "xpath": '//div[@class="line-metrics"]/div[2]//div[@class="content"]//span', "kind": "integer"},
    {"period": "7天", "field": "7天ALL点击率", "xpath": '//div[@class="line-metrics"]/div[3]//div[@class="content"]//span', "kind": "percent"},
    {"period": "7天", "field": "7天ALL订单量", "xpath": '//div[@class="line-metrics"]/div[4]//div[@class="content"]//span', "kind": "integer"},
    {"period": "7天", "field": "7天ALL商品已出售", "xpath": '//div[@class="line-metrics"]/div[5]//div[@class="content"]//span', "kind": "integer"},
    {"period": "7天", "field": "7天ALL销售额", "xpath": '//div[@class="line-metrics"]/div[6]//div[@class="content"]//span', "kind": "currency", "currency_code": "BRL"},
    {"period": "7天", "field": "7天ALL优惠价金额", "xpath": '//div[@class="line-metrics"]/div[9]//div[@class="content"]//span', "kind": "currency", "currency_code": "BRL"},
    {"period": "7天", "field": "7天ALL优惠劵带来销售额", "xpath": '//div[@class="line-metrics"]/div[10]//div[@class="content"]//span', "kind": "currency", "currency_code": "BRL"},
    {"period": "7天", "field": "7天ALL加购次数", "xpath": '//div[@class="line-metrics"]/div[11]//div[@class="content"]//span', "kind": "integer"},
    {"period": "7天", "field": "7天ALL加购率", "xpath": '//div[@class="line-metrics"]/div[12]//div[@class="content"]//span', "kind": "percent"},
    {"period": "7天", "field": "7天ALL花费", "xpath": '//div[@class="line-metrics"]/div[7]//div[@class="content"]//span', "kind": "currency", "currency_code": "BRL"},
    {"period": "7天", "field": "7天ALL广告支出回报率", "xpath": '//div[@class="line-metrics"]/div[8]//div[@class="content"]//span', "kind": "decimal"}
]
METRIC_SPECS.extend(
    {**spec, "period": "今天", "field": spec["field"].replace("7天", "今天")}
    for spec in tuple(METRIC_SPECS)
    if spec["period"] == "7天"
)


class ShopeeAuto:
    """Shopee 广告后台昨天、最近 7 天和今天数据自动化。"""

    def __init__(self, config: dict[str, Any] | None = None):
        """保存 Shopee 独立配置；所有 XPath 集中维护在本文件顶部。"""
        self.config = config or {}

    def collect(
        self,
        store_name: str,
        download_path: str = "",
        debugging_port: int | str | None = None,
    ) -> list[dict[str, Any]]:
        """接管紫鸟当前标签页，采集昨天、最近 7 天和今天广告指标。"""
        if not debugging_port:
            raise RuntimeError("紫鸟没有返回 debuggingPort，无法接管 Shopee 店铺")

        LOGGER.info("[Shopee][开始] 店铺=%s，准备接管紫鸟浏览器，debugging_port=%s", store_name, debugging_port)

        # 只连接紫鸟已经打开的 Chromium，不创建普通浏览器；确认登录后在当前标签页打开广告页。
        browser = Chromium(f"127.0.0.1:{debugging_port}")
        tab = browser.latest_tab
        collected_at = datetime.now(timezone.utc).isoformat()

        # 首页先通过 URL 判断登录状态。若 URL 中含 login，点击登录页的 Entrar 按钮，
        # 等待回到卖家中心首页后再进入广告页；已登录状态则直接继续。
        self._confirm_login_state_by_url(tab, store_name)
        template_url = self._open_ad_page(tab, store_name)

        rows: list[dict[str, Any]] = []
        for period in ("昨天", "7天", "今天"):
            group_value = PERIOD_GROUP_VALUES[period]
            period_url = self._replace_group_in_url(template_url, group_value)
            LOGGER.info(
                "[Shopee][周期URL] 时间范围=%s，group=%s，模板url=%s，目标url=%s",
                period,
                group_value,
                template_url,
                period_url,
            )
            self._open_period_page(tab, store_name, period, group_value, period_url)

            for spec in METRIC_SPECS:
                if spec["period"] != period:
                    continue
                field_name = spec["field"]
                xpath = spec["xpath"]
                value_kind = spec["kind"]
                currency_code = spec.get("currency_code", "")
                LOGGER.info(
                    "[Shopee][指标] 准备读取：时间范围=%s，字段=%s，配置类型=%s，币种=%s，xpath=%s",
                    period,
                    field_name,
                    value_kind,
                    currency_code or "无",
                    xpath or "<空 XPath>",
                )
                raw_text = self._read_xpath(tab, xpath, field_name)
                converted_value = self._format_value(raw_text, value_kind)
                display_value = self._format_display_value(converted_value, value_kind)
                LOGGER.info(
                    "[Shopee][指标结果] 字段=%s，原始值=%r，原始类型=%s，内部数值=%r，数值类型=%s，显示值=%r，配置类型=%s，币种=%s",
                    field_name,
                    raw_text,
                    type(raw_text).__name__,
                    converted_value,
                    type(converted_value).__name__,
                    display_value,
                    value_kind,
                    currency_code or "无",
                )
                if raw_text == "":
                    LOGGER.warning("[Shopee][指标失败] 字段=%s，XPath 未抓到有效文本，最终按空值处理", field_name)
                elif converted_value == "":
                    LOGGER.warning(
                        "[Shopee][转换失败] 字段=%s，原始值=%r 无法按类型=%s 转换，最终按空值处理",
                        field_name,
                        raw_text,
                        value_kind,
                    )

                # 爬虫按“一项指标一行”返回，编排器随后合并为一条 26 字段飞书记录。
                row = {
                    "店铺名": store_name,
                    "平台": "shopee",
                    "采集时间": collected_at,
                    "指标": field_name,
                    # 数值保留为 int/float，百分比用 0~1 的比例保存，便于计算和 DeepSeek 分析。
                    # 编排器写入飞书时，才会把 0.0379 转换成 "3.8%" 文本。
                    "数值": converted_value,
                    # 显示值用于机器人和 ALL_info，保留人能直接识别的货币/百分比单位。
                    "显示值": display_value,
                    "原始数据": json.dumps(
                        {
                            "时间范围": period,
                            "数据行": "ALL",
                            "字段": field_name,
                            "原始值": raw_text,
                            "XPath": xpath,
                        },
                        ensure_ascii=False,
                    ),
                }
                rows.append(row)

        # 广告数据完成后依次进入商业分析页面，三种周期均读取当前 DOM。
        rows.extend(self._collect_data_center_pages(tab, store_name, collected_at))

        valid_count = sum(row["数值"] != "" for row in rows)
        LOGGER.info("[Shopee][结果打包] rows=%s", json.dumps(rows, ensure_ascii=False, default=str))
        LOGGER.info("[Shopee][完成] 店铺=%s，有效指标=%s/%s", store_name, valid_count, len(rows))
        return rows

    def _collect_data_center_pages(self, tab: Any, store_name: str, collected_at: str) -> list[dict[str, Any]]:
        """读取商业分析页面的卡片和表格，返回可并入现有结果的 JSON 字段。"""
        rows: list[dict[str, Any]] = []
        for page_name, page_url in SHOPEE_DATA_CENTER_URLS.items():
            try:
                if page_name == "商业分析概述":
                    # 广告数据完成后先点击页面导航中的“商业分析”，保持与人工操作一致。
                    analytics_link_xpath = '//a[contains(@href,"/datacenter") and normalize-space()="商业分析"]'
                    analytics_link = self._find_visible_element(tab, analytics_link_xpath, timeout=3)
                    if analytics_link:
                        self._click_element_with_fallback(tab, analytics_link, analytics_link_xpath, "打开商业分析")
                        time.sleep(1)
                # 先带一个明确的 group 进入页面，避免前端沿用上一个广告页的周期状态。
                initial_url = self._replace_group_in_url(page_url, PERIOD_GROUP_VALUES["昨天"])
                current_url = self._read_current_url(tab)
                if page_name != "商业分析概述" or "/datacenter" not in current_url:
                    result = tab.get(initial_url, timeout=AD_PAGE_LOAD_TIMEOUT_SECONDS)
                else:
                    result = True
                if result is False or not self._wait_for_page_ready(tab, AD_PAGE_LOAD_TIMEOUT_SECONDS):
                    raise TimeoutError("页面未完成加载")
                self._wait_for_data_center_render(tab, page_name)
                for period in EXTRA_PERIODS:
                    try:
                        self._select_data_center_period(tab, period, page_name)
                        # 日期点击后页面会重新请求数据；必须等完整业务节点并留出稳定时间再读取。
                        notice_detected = self._wait_for_data_center_render(tab, page_name)
                        if not notice_detected:
                            time.sleep(EXTRA_PAGE_WAIT_SECONDS)
                        raw_payload = self._read_data_center_payload(tab, page_name)
                        payload = self._clean_data_center_payload(page_name, raw_payload)
                        field_name = f"Shopee{page_name}_{period}"
                        rows.append({
                            "店铺名": store_name,
                            "平台": "shopee",
                            "采集时间": collected_at,
                            "指标": field_name,
                            "数值": json.dumps(payload, ensure_ascii=False),
                            "显示值": json.dumps(payload, ensure_ascii=False),
                            "原始数据": json.dumps({"页面": page_name, "时间范围": period, "数据": payload}, ensure_ascii=False),
                        })
                    except Exception as period_exc:
                        LOGGER.warning(
                            "[Shopee][商业分析周期失败] 店铺=%s，页面=%s，时间范围=%s，异常=%s",
                            store_name,
                            page_name,
                            period,
                            period_exc,
                        )
                        rows.append({
                            "店铺名": store_name, "平台": "shopee", "采集时间": collected_at,
                            "指标": f"Shopee{page_name}_{period}_失败", "数值": "", "显示值": str(period_exc),
                            "原始数据": json.dumps(
                                {"页面": page_name, "时间范围": period, "错误": str(period_exc)},
                                ensure_ascii=False,
                            ),
                        })
            except Exception as exc:
                LOGGER.warning("[Shopee][商业分析页面失败] 店铺=%s，页面=%s，异常=%s", store_name, page_name, exc)
                rows.append({
                    "店铺名": store_name, "平台": "shopee", "采集时间": collected_at,
                    "指标": f"Shopee{page_name}_失败", "数值": "", "显示值": str(exc),
                    "原始数据": json.dumps({"页面": page_name, "错误": str(exc)}, ensure_ascii=False),
                })
        return rows

    def _select_data_center_period(self, tab: Any, period: str, page_name: str) -> None:
        """使用页面内原子操作打开日期菜单并点击最新快捷项，避免 Vue 重渲染导致元素失效。"""
        label = EXTRA_PERIOD_LABELS[period]
        label_literal = json.dumps(label, ensure_ascii=False)
        picker_result = tab.run_js(
            "const picker = [...document.querySelectorAll('.bi-date-input')]"
            ".find((el) => { const rect = el.getBoundingClientRect(); "
            "return rect.width > 0 && rect.height > 0; });"
            "if (!picker) return {clicked: false};"
            "picker.click(); return {clicked: true};"
        )
        if not isinstance(picker_result, dict) or not picker_result.get("clicked"):
            raise RuntimeError(f"{page_name} 未找到可点击的统计时间选择器")

        option_clicked = False
        clicked_text = ""
        for option_check_index in range(1, 21):
            option_result = tab.run_js(
                rf"""
                const wanted = {label_literal}.replace(/\s+/g, '');
                const options = [...document.querySelectorAll('li.eds-date-shortcut-item')];
                const option = options.find((item) => {{
                    const textNode = item.querySelector('.eds-date-shortcut-item__text') || item;
                    const text = (textNode.innerText || '').replace(/\s+/g, '');
                    const rect = item.getBoundingClientRect();
                    return text === wanted && rect.width > 0 && rect.height > 0;
                }});
                if (!option) return {{clicked: false, count: options.length}};
                const textNode = option.querySelector('.eds-date-shortcut-item__text') || option;
                const text = (textNode.innerText || '').trim();
                option.scrollIntoView({{block: 'center'}});
                option.click();
                return {{clicked: true, text: text, count: options.length}};
                """
            )
            if isinstance(option_result, dict) and option_result.get("clicked"):
                option_clicked = True
                clicked_text = str(option_result.get("text") or "")
                LOGGER.info(
                    "[Shopee][商业分析日期点击] 页面=%s，时间范围=%s，目标文本=%r，第%s次命中最新元素",
                    page_name,
                    period,
                    clicked_text,
                    option_check_index,
                )
                break
            time.sleep(0.25)
        if not option_clicked:
            raise RuntimeError(f"{page_name} 打开日期菜单后未找到时间范围={period}快捷项")

        expected_label = EXTRA_PERIOD_LABELS[period]
        for check_index in range(1, 21):
            try:
                current_label = str(
                    tab.run_js(
                        "return (document.querySelector('.bi-date-input .label') || {}).innerText || '';"
                    )
                    or ""
                ).strip()
            except Exception:
                current_label = ""
            if expected_label.replace(" ", "") in current_label.replace(" ", ""):
                LOGGER.info(
                    "[Shopee][商业分析日期确认] 页面=%s，时间范围=%s，页面标签=%r，第%s次确认成功",
                    page_name,
                    period,
                    current_label,
                    check_index,
                )
                return
            time.sleep(1)
        raise TimeoutError(f"{page_name} 选择{period}后 20 秒内未确认日期标签，期望={expected_label}")

    def _wait_for_data_center_render(self, tab: Any, page_name: str) -> bool:
        """等待业务节点出现；发现“数据尚未准备好”提示时立即返回 True。"""
        selector, minimum = DATA_CENTER_RENDER_SELECTORS[page_name]
        deadline = time.monotonic() + DATA_CENTER_RENDER_TIMEOUT_SECONDS
        last_count = 0
        check_index = 0
        last_signature = ""
        stable_checks = 0
        while time.monotonic() < deadline:
            check_index += 1
            try:
                result = tab.run_js(
                    "const nodes = [...document.querySelectorAll(%s)];"
                    "const notice = [...document.querySelectorAll('.bi-notice-bar .eds-alert--warning .eds-alert-title')]"
                    ".map((node) => (node.innerText || '').trim()).find(Boolean) || '';"
                    "return {ready: document.readyState, count: nodes.length, notice: notice, "
                    "signature: nodes.map((node) => (node.innerText || '').trim()).join('||')};"
                    % json.dumps(selector, ensure_ascii=False)
                )
                if isinstance(result, dict):
                    last_count = int(result.get("count") or 0)
                    ready_state = str(result.get("ready") or "").lower()
                    signature = str(result.get("signature") or "")
                    notice = str(result.get("notice") or "").strip()
                else:
                    ready_state = ""
                    signature = ""
                    notice = ""
            except Exception:
                ready_state = ""
                signature = ""
                notice = ""
            if notice:
                LOGGER.warning(
                    "[Shopee][商业分析数据未准备好] 页面=%s，提示=%s",
                    page_name,
                    notice,
                )
                return True
            if last_count >= minimum and signature:
                stable_checks = stable_checks + 1 if signature == last_signature else 1
            else:
                stable_checks = 0
            last_signature = signature
            if check_index == 1 or check_index % 5 == 0 or last_count >= minimum:
                LOGGER.info(
                    "[Shopee][商业分析等待] 页面=%s，第%s次，readyState=%s，业务节点=%s/%s，稳定次数=%s/3",
                    page_name,
                    check_index,
                    ready_state or "未知",
                    last_count,
                    minimum,
                    stable_checks,
                )
            if last_count >= minimum and stable_checks >= 3:
                LOGGER.info(
                    "[Shopee][商业分析加载完成] 页面=%s，业务节点=%s，额外等待=%.1f秒",
                    page_name,
                    last_count,
                    DATA_CENTER_RENDER_EXTRA_WAIT_SECONDS,
                )
                time.sleep(DATA_CENTER_RENDER_EXTRA_WAIT_SECONDS)
                return False
            time.sleep(DATA_CENTER_RENDER_POLL_SECONDS)
        raise TimeoutError(
            f"Shopee {page_name} 在 {DATA_CENTER_RENDER_TIMEOUT_SECONDS} 秒内未出现业务节点，"
            f"selector={selector}，最后数量={last_count}"
        )

    @staticmethod
    def _read_data_center_payload(tab: Any, page_name: str) -> dict[str, Any]:
        """在浏览器端按业务 class 读取卡片与表格，避免依赖随机 data-v 属性。"""
        page_name_literal = json.dumps(page_name, ensure_ascii=False)
        script = rf"""
        const pageName = {page_name_literal};
        const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
        const text = (el) => clean(el ? el.innerText : '');
        const selectors = {{
          '商业分析概述': '.dashboard-key-metric-group .key-metric',
          '商品概述': '.product-overview .key-metric',
          '商品流量': '.eds-metrics-card',
          '流量概述': '.metric-item'
        }};
        let cardElements = [...document.querySelectorAll(selectors[pageName] || '.key-metric, .metric-item')];
        if (pageName === '商业分析概述') cardElements = cardElements.slice(0, 7);
        const cards = cardElements.map((el) => ({{
          title: text(el.querySelector('.title-text, .eds-metrics-card__name .title, .total-sales-title__text, .title')),
          value: text(el.querySelector('.value .number, .value, .eds-metrics-card__value, .currency-value')),
          values: [...el.querySelectorAll('.metric-data .value .number, .metric-data .value')].map(text).filter(Boolean),
          className: el.className || ''
        }})).filter((x) => x.title || x.value);
        const tableSignatures = new Set();
        const tables = [...document.querySelectorAll('table')].map((table) => {{
          const headers = [...table.querySelectorAll('thead th')].map(text);
          const rows = [...table.querySelectorAll('tbody tr')].map((tr) => [...tr.querySelectorAll('td')].map(text));
          return {{headers, rows}};
        }}).filter((item) => {{
          if (!item.headers.length && !item.rows.length) return false;
          const signature = JSON.stringify(item);
          if (tableSignatures.has(signature)) return false;
          tableSignatures.add(signature);
          return true;
        }});
        const notice = [...document.querySelectorAll('.bi-notice-bar .eds-alert--warning .eds-alert-title')]
          .map((node) => text(node)).find(Boolean) || '';
        return {{notice, cards, tables}};
        """
        payload = tab.run_js(script)
        return payload if isinstance(payload, dict) else {"raw": str(payload or "")}

    @staticmethod
    def _clean_data_center_payload(page_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """按页面业务范围清洗卡片和表格，去除 DOM class、趋势值、隐藏表及操作按钮。"""
        raw_cards = payload.get("cards", [])
        cards = raw_cards if isinstance(raw_cards, list) else []

        def clean_text(value: Any) -> str:
            return re.sub(r"\s+", " ", str(value or "")).strip()

        def clean_title(value: Any) -> str:
            title = clean_text(value)
            return re.sub(r"\s*Definition updated\s+\d{4}\s*", "", title, flags=re.IGNORECASE).strip()

        def clean_value(value: Any) -> str:
            text = clean_text(value)
            if not text or text == "-":
                return "-"
            if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", text):
                return text
            number = ShopeeAuto._parse_brazilian_number(text)
            if number is None:
                return text
            if "R$" in text:
                return f"R${number:.2f}"
            if "%" in text:
                return f"{number:.2f}%"
            if re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", text):
                return str(int(text.replace(".", "")))
            if re.fullmatch(r"-?\d+", text):
                return str(int(number))
            if re.fullmatch(r"-?[\d.]+,\d+", text):
                return f"{number:.2f}"
            return text

        notice = clean_text(payload.get("notice"))
        if notice:
            return {"状态": "数据尚未准备好", "提示": notice}

        if page_name == "商业分析概述":
            metrics = [
                {"名称": clean_title(card.get("title")), "值": clean_value(card.get("value"))}
                for card in cards[:7]
                if isinstance(card, dict) and clean_title(card.get("title"))
            ]
            return {"指标": metrics}

        if page_name == "商品概述":
            group_by_key = {
                "uv": "访问", "pv": "访问", "iv": "访问", "bounce_visitors": "访问",
                "bounce_rate": "访问", "search_clicks": "访问", "like_unit_num": "访问",
                "atc_uv": "加入购物车", "atc_unit_num": "加入购物车", "atc_rate": "加入购物车",
                "placed_buyers": "已下订单", "placed_unit_num": "已下订单",
                "placed_items": "已下订单", "placed_gmv": "已下订单",
                "uv_to_placed_buyers_rate": "已下订单",
                "paid_buyers": "已付款订单", "paid_unit_num": "已付款订单",
                "paid_items": "已付款订单", "paid_gmv": "已付款订单",
                "uv_to_paid_buyers_rate": "已付款订单",
                "repeat_paid_order_rate": "已付款订单",
                "average_days_to_repeat_paid_order": "已付款订单",
            }
            metrics: list[dict[str, str]] = []
            for card in cards:
                if not isinstance(card, dict):
                    continue
                class_name = clean_text(card.get("className"))
                key_match = re.search(r"product-overview__section-list__([a-z_]+)", class_name)
                key = key_match.group(1) if key_match else ""
                metrics.append({
                    "分组": group_by_key.get(key, "其他"),
                    "名称": clean_title(card.get("title")),
                    "值": clean_value(card.get("value")),
                })
            return {"指标": [metric for metric in metrics if metric["名称"]]}

        if page_name == "流量概述":
            metrics: list[dict[str, str]] = []
            for card in cards:
                if not isinstance(card, dict):
                    continue
                raw_values = card.get("values", [])
                values = [clean_value(value) for value in raw_values] if isinstance(raw_values, list) else []
                # 当前 DOM 每列会同时命中父 value 和内部 number，因此六项按每两项取一项。
                if len(values) >= 6:
                    values = values[::2]
                while len(values) < 3:
                    values.append(clean_value(card.get("value")))
                metrics.append({
                    "名称": clean_title(card.get("title")),
                    "全部": values[0],
                    "APP": values[1],
                    "PC": values[2],
                })
            return {"指标": [metric for metric in metrics if metric["名称"]]}

        summary_metrics = [
            {"名称": clean_title(card.get("title")), "值": clean_value(card.get("value"))}
            for card in cards
            if isinstance(card, dict) and clean_title(card.get("title"))
        ]
        tables = payload.get("tables", [])
        table_items = tables if isinstance(tables, list) else []
        headers: list[str] = []
        for table in table_items:
            candidate = table.get("headers", []) if isinstance(table, dict) else []
            candidate = [clean_text(header) for header in candidate]
            if candidate and candidate[0] == "流量来源":
                headers = candidate
                break

        row_values: list[list[str]] = []
        if headers:
            for table in table_items:
                candidate_rows = table.get("rows", []) if isinstance(table, dict) else []
                if not isinstance(candidate_rows, list):
                    continue
                matched_rows = [row for row in candidate_rows if isinstance(row, list) and len(row) == len(headers)]
                if matched_rows:
                    row_values = matched_rows
                    break

        ignored_columns = {"购买", "操作"}

        def clean_table_value(header: str, value: Any) -> str:
            text = clean_text(value)
            if header == "流量来源":
                return text
            currency_match = re.search(r"R\$\s*[\d.]+(?:,\d+)?", text)
            if currency_match:
                return clean_value(currency_match.group(0).replace("R$ ", "R$"))
            if any(keyword in header for keyword in ("率", "占比")):
                percent_match = re.search(r"-?[\d.]+(?:,\d+)?%", text)
                if percent_match:
                    return clean_value(percent_match.group(0))
            number_match = re.search(r"-?[\d.]+(?:,\d+)?", text)
            return clean_value(number_match.group(0)) if number_match else (text or "-")

        source_distribution: list[dict[str, str]] = []
        for row in row_values:
            cleaned_row = {
                header: clean_table_value(header, value)
                for header, value in zip(headers, row)
                if header not in ignored_columns
            }
            if cleaned_row.get("流量来源"):
                source_distribution.append(cleaned_row)
        return {"指标": summary_metrics, "来源分布": source_distribution}

    def _confirm_login_state_by_url(self, tab: Any, store_name: str) -> None:
        """根据当前 URL 判断登录状态；登录页点击 Entrar 后等待卖家中心首页。"""
        started_at = time.monotonic()
        deadline = started_at + SHOPEE_URL_STATE_TIMEOUT_SECONDS
        last_url = ""
        LOGGER.info(
            "[Shopee][URL登录判断] 店铺=%s，最长等待=%.1f秒，登录页=%s，已登录域名=%s",
            store_name,
            SHOPEE_URL_STATE_TIMEOUT_SECONDS,
            SHOPEE_LOGIN_PAGE_URL,
            SHOPEE_SELLER_HOST,
        )
        while time.monotonic() < deadline:
            current_url = self._read_current_url(tab)
            if current_url != last_url:
                LOGGER.info("[Shopee][URL变化] 店铺=%s，当前url=%s", store_name, current_url or "<空>")
                last_url = current_url

            url_state = self._classify_login_url(current_url)
            if url_state == "not_logged_in":
                LOGGER.warning(
                    "[Shopee][URL确认未登录] 店铺=%s，url=%s；准备点击登录按钮 xpath=%s",
                    store_name,
                    current_url,
                    SHOPEE_LOGIN_BUTTON_XPATH,
                )
                self._login_from_login_page(tab, store_name)
                return
            if url_state == "logged_in":
                LOGGER.info(
                    "[Shopee][URL确认已登录] 店铺=%s，url=%s，耗时=%.2f秒",
                    store_name,
                    current_url,
                    time.monotonic() - started_at,
                )
                return
            time.sleep(1)

        raise RuntimeError(
            f"Shopee 店铺 {store_name} 在 {SHOPEE_URL_STATE_TIMEOUT_SECONDS} 秒内未进入登录页或卖家中心，"
            f"无法确认登录状态，最后网址={last_url or '<空>'}；已停止本店铺采集。"
        )

    def _login_from_login_page(self, tab: Any, store_name: str) -> None:
        """点击 Shopee 登录页的 Entrar 按钮，并等待页面回到卖家中心首页。"""
        max_attempts = SHOPEE_LOGIN_CLICK_RETRY_TIMES + 1
        last_error = ""

        for attempt in range(max_attempts):
            attempt_number = attempt + 1
            if attempt > 0:
                interval = CLICK_RETRY_INTERVAL_SECONDS + random.uniform(0, 1)
                LOGGER.warning(
                    "[Shopee][登录按钮重试] 店铺=%s，第 %s/%s 次点击前等待 %.2f 秒，xpath=%s",
                    store_name,
                    attempt_number,
                    max_attempts,
                    interval,
                    SHOPEE_LOGIN_BUTTON_XPATH,
                )
                time.sleep(interval)

            try:
                LOGGER.info(
                    "[Shopee][登录按钮查找] 店铺=%s，第 %s/%s 次尝试，xpath=%s",
                    store_name,
                    attempt_number,
                    max_attempts,
                    SHOPEE_LOGIN_BUTTON_XPATH,
                )
                login_button = self._find_action_element(
                    tab,
                    SHOPEE_LOGIN_BUTTON_XPATH,
                    timeout=3,
                    target_name="Shopee登录Entrar按钮",
                )
                if not login_button:
                    raise RuntimeError("未找到可点击的 Entrar 登录按钮")

                self._click_element_with_fallback(
                    tab,
                    login_button,
                    SHOPEE_LOGIN_BUTTON_XPATH,
                    "Shopee登录Entrar",
                )
                LOGGER.info(
                    "[Shopee][登录按钮点击成功] 店铺=%s，第 %s/%s 次点击已提交，等待首页",
                    store_name,
                    attempt_number,
                    max_attempts,
                )
                self._wait_for_login_home(tab, store_name)
                return
            except Exception as exc:
                last_error = str(exc)
                LOGGER.warning(
                    "[Shopee][登录按钮失败] 店铺=%s，第 %s/%s 次失败，异常=%s",
                    store_name,
                    attempt_number,
                    max_attempts,
                    exc,
                )

                # 登录按钮点击、页面跳转或首页等待出现异常后，先复核一次 URL。
                # 有些 Shopee 登录请求实际已经成功，但按钮点击返回异常或首页加载等待超时；
                # 只要地址已经离开登录页，就直接确认登录，避免再次点击 Entrar 造成重复提交。
                current_url = self._read_current_url(tab)
                if current_url and self._classify_login_url(current_url) != "not_logged_in":
                    LOGGER.warning(
                        "[Shopee][登录失败后URL确认已登录] 店铺=%s，第 %s/%s 次失败后，"
                        "当前url已不是未登录地址，直接确认已登录，url=%s",
                        store_name,
                        attempt_number,
                        max_attempts,
                        current_url,
                    )
                    return
                LOGGER.info(
                    "[Shopee][登录失败后URL复核仍未登录] 店铺=%s，第 %s/%s 次失败后，继续重试，url=%s",
                    store_name,
                    attempt_number,
                    max_attempts,
                    current_url or "<空>",
                )

        raise RuntimeError(
            f"Shopee 店铺 {store_name} 登录按钮连续 {max_attempts} 次未能完成，最后错误={last_error}；"
            "已停止本店铺采集。"
        )

    def _wait_for_login_home(self, tab: Any, store_name: str) -> None:
        """等待登录后 URL 回到卖家中心，并确认首页主文档加载完成。"""
        started_at = time.monotonic()
        deadline = started_at + SHOPEE_LOGIN_HOME_TIMEOUT_SECONDS
        last_url = ""
        LOGGER.info(
            "[Shopee][登录后首页等待] 店铺=%s，最长等待=%.1f秒，目标域名=%s",
            store_name,
            SHOPEE_LOGIN_HOME_TIMEOUT_SECONDS,
            SHOPEE_SELLER_HOST,
        )

        while time.monotonic() < deadline:
            current_url = self._read_current_url(tab)
            if current_url != last_url:
                LOGGER.info("[Shopee][登录后URL变化] 店铺=%s，当前url=%s", store_name, current_url or "<空>")
                last_url = current_url

            if self._classify_login_url(current_url) == "logged_in":
                remaining = max(1.0, deadline - time.monotonic())
                page_ready = self._wait_for_page_ready(tab, min(remaining, AD_PAGE_LOAD_TIMEOUT_SECONDS))
                if page_ready:
                    LOGGER.info(
                        "[Shopee][登录成功] 店铺=%s，已回到卖家中心首页，耗时=%.2f秒，url=%s",
                        store_name,
                        time.monotonic() - started_at,
                        current_url,
                    )
                    return
                LOGGER.warning(
                    "[Shopee][登录后首页未就绪] 店铺=%s，当前url=%s，继续等待重定向/页面加载",
                    store_name,
                    current_url,
                )
            time.sleep(1)

        raise TimeoutError(
            f"Shopee 店铺 {store_name} 点击 Entrar 后在 {SHOPEE_LOGIN_HOME_TIMEOUT_SECONDS} 秒内未确认首页加载完成，"
            f"最后网址={last_url or '<空>'}"
        )

    def _open_ad_page(self, tab: Any, store_name: str) -> str:
        """打开广告入口并取得包含 Shopee 动态参数和 group 的完整模板 URL。"""
        max_attempts = AD_PAGE_LOAD_RETRY_TIMES + 1
        last_failure_reason = ""

        for attempt in range(max_attempts):
            attempt_number = attempt + 1
            try:
                if attempt == 0:
                    LOGGER.info(
                        "[Shopee][广告入口跳转] 店铺=%s，第 %s/%s 轮，url=%s，单轮超时=%.1f秒",
                        store_name,
                        attempt_number,
                        max_attempts,
                        SHOPEE_AD_PAGE_URL,
                        AD_PAGE_LOAD_TIMEOUT_SECONDS,
                    )
                    navigation_result = tab.get(SHOPEE_AD_PAGE_URL, timeout=AD_PAGE_LOAD_TIMEOUT_SECONDS)
                else:
                    retry_current_url = self._read_current_url(tab)
                    if self._is_traffic_error_url(retry_current_url):
                        LOGGER.warning(
                            "[Shopee][广告入口重新跳转重试] 店铺=%s，第 %s/%s 轮，"
                            "当前仍是流量错误页，重新跳转广告入口，不执行刷新，url=%s",
                            store_name,
                            attempt_number,
                            max_attempts,
                            retry_current_url,
                        )
                        navigation_result = tab.get(SHOPEE_AD_PAGE_URL, timeout=AD_PAGE_LOAD_TIMEOUT_SECONDS)
                    else:
                        LOGGER.warning(
                            "[Shopee][广告入口刷新重试] 店铺=%s，第 %s/%s 轮，上轮失败原因=%s",
                            store_name,
                            attempt_number,
                            max_attempts,
                            last_failure_reason,
                        )
                        navigation_result = tab.refresh()
                if navigation_result is False:
                    current_url = self._read_current_url(tab)
                    if self._is_traffic_error_url(current_url):
                        LOGGER.warning(
                            "[Shopee][广告入口导航超时后命中流量错误页] 店铺=%s，url=%s；"
                            "重新跳转广告入口，不执行刷新",
                            store_name,
                            current_url,
                        )
                        self._reopen_ad_entry_after_traffic_error(tab, store_name)
                    else:
                        last_failure_reason = f"导航在 {AD_PAGE_LOAD_TIMEOUT_SECONDS} 秒内未完成"
                        continue
            except Exception as exc:
                last_failure_reason = f"打开或刷新广告入口异常: {exc}"
                LOGGER.warning(
                    "[Shopee][广告入口导航异常] 店铺=%s，第 %s/%s 轮，异常=%s",
                    store_name,
                    attempt_number,
                    max_attempts,
                    exc,
                )
                continue

            current_url = self._read_current_url(tab)
            if self._classify_login_url(current_url) == "not_logged_in":
                raise RuntimeError(
                    f"Shopee 店铺 {store_name} 跳转广告入口后被重定向到登录页；"
                    "已停止本店铺采集，关闭店铺后继续下一店铺。"
                )

            if not self._wait_for_page_ready(tab, AD_PAGE_LOAD_TIMEOUT_SECONDS):
                last_failure_reason = f"document.readyState 在 {AD_PAGE_LOAD_TIMEOUT_SECONDS} 秒内未达到 complete"
                continue

            template_url = self._wait_for_group_url(tab, store_name)
            if template_url:
                LOGGER.info(
                    "[Shopee][广告入口URL确认] 店铺=%s，第 %s/%s 轮，完整模板url=%s，group=%s",
                    store_name,
                    attempt_number,
                    max_attempts,
                    template_url,
                    self._extract_group_from_url(template_url),
                )
                return template_url

            last_failure_reason = (
                f"每隔 {AD_GROUP_URL_CHECK_INTERVAL_SECONDS} 秒查询 {AD_GROUP_URL_CHECK_TIMES} 次后，"
                "URL仍没有已知group参数"
            )

        raise TimeoutError(
            f"Shopee 广告入口首次打开并刷新 {AD_PAGE_LOAD_RETRY_TIMES} 次后仍未取得完整group URL；"
            f"最后原因={last_failure_reason or '未知'}"
        )

    def _wait_for_group_url(self, tab: Any, store_name: str) -> str:
        """每隔2秒读取URL；流量错误页必须重新跳转广告入口，不能刷新错误页。"""
        last_url = ""
        for check_index in range(AD_GROUP_URL_CHECK_TIMES):
            current_url = self._read_current_url(tab)
            last_url = current_url or last_url

            if self._classify_login_url(current_url) == "not_logged_in":
                raise RuntimeError(
                    f"Shopee 店铺 {store_name} 等待广告URL时进入登录页；"
                    "已停止本店铺采集，关闭店铺后继续下一店铺。"
                )

            if self._is_traffic_error_url(current_url):
                LOGGER.warning(
                    "[Shopee][流量验证错误页] 店铺=%s，第 %s/%s 次查询命中url=%s；"
                    "按要求重新跳转广告入口，不刷新当前错误页",
                    store_name,
                    check_index + 1,
                    AD_GROUP_URL_CHECK_TIMES,
                    current_url,
                )
                self._reopen_ad_entry_after_traffic_error(tab, store_name)
            else:
                group_value = self._extract_group_from_url(current_url)
                LOGGER.info(
                    "[Shopee][广告URL查询] 店铺=%s，第 %s/%s 次，group=%s，url=%s",
                    store_name,
                    check_index + 1,
                    AD_GROUP_URL_CHECK_TIMES,
                    group_value or "<未出现>",
                    current_url or "<空>",
                )
                if group_value in AD_KNOWN_GROUP_VALUES:
                    return current_url

            if check_index < AD_GROUP_URL_CHECK_TIMES - 1:
                time.sleep(AD_GROUP_URL_CHECK_INTERVAL_SECONDS)

        LOGGER.error(
            "[Shopee][广告URL查询超时] 店铺=%s，查询次数=%s，间隔=%.1f秒，最后url=%s",
            store_name,
            AD_GROUP_URL_CHECK_TIMES,
            AD_GROUP_URL_CHECK_INTERVAL_SECONDS,
            last_url or "<空>",
        )
        return ""

    def _open_period_page(
        self,
        tab: Any,
        store_name: str,
        period: str,
        expected_group: str,
        period_url: str,
    ) -> None:
        """打开指定周期 URL；数据抓不到时刷新当前周期页，最多额外重试5次。"""
        max_attempts = PERIOD_PAGE_REFRESH_RETRY_TIMES + 1
        last_failure_reason = ""

        for attempt in range(max_attempts):
            attempt_number = attempt + 1
            try:
                if attempt == 0:
                    LOGGER.info(
                        "[Shopee][周期页跳转] 店铺=%s，时间范围=%s，第 %s/%s 轮，url=%s",
                        store_name,
                        period,
                        attempt_number,
                        max_attempts,
                        period_url,
                    )
                    navigation_result = tab.get(period_url, timeout=AD_PAGE_LOAD_TIMEOUT_SECONDS)
                else:
                    retry_current_url = self._read_current_url(tab)
                    if self._is_traffic_error_url(retry_current_url):
                        LOGGER.warning(
                            "[Shopee][周期页重新跳转重试] 店铺=%s，时间范围=%s，第 %s/%s 轮，"
                            "当前仍是流量错误页，先重新跳转广告入口，url=%s",
                            store_name,
                            period,
                            attempt_number,
                            max_attempts,
                            retry_current_url,
                        )
                        navigation_result = self._reopen_period_after_traffic_error(
                            tab,
                            store_name,
                            period,
                            period_url,
                        )
                    else:
                        LOGGER.warning(
                            "[Shopee][周期页刷新重试] 店铺=%s，时间范围=%s，第 %s/%s 轮，上轮失败原因=%s",
                            store_name,
                            period,
                            attempt_number,
                            max_attempts,
                            last_failure_reason,
                        )
                        navigation_result = tab.refresh()
                if navigation_result is False:
                    current_url = self._read_current_url(tab)
                    if self._is_traffic_error_url(current_url):
                        LOGGER.warning(
                            "[Shopee][周期页导航超时后命中流量错误页] 店铺=%s，时间范围=%s，url=%s；"
                            "先重新跳转广告入口",
                            store_name,
                            period,
                            current_url,
                        )
                        navigation_result = self._reopen_period_after_traffic_error(
                            tab,
                            store_name,
                            period,
                            period_url,
                        )
                    else:
                        last_failure_reason = f"导航在 {AD_PAGE_LOAD_TIMEOUT_SECONDS} 秒内未完成"
                        continue
            except Exception as exc:
                last_failure_reason = f"打开或刷新{period}页面异常: {exc}"
                LOGGER.warning(
                    "[Shopee][周期页导航异常] 店铺=%s，时间范围=%s，第 %s/%s 轮，异常=%s",
                    store_name,
                    period,
                    attempt_number,
                    max_attempts,
                    exc,
                )
                continue

            current_url = self._read_current_url(tab)
            if self._classify_login_url(current_url) == "not_logged_in":
                raise RuntimeError(f"Shopee 店铺 {store_name} 打开{period}数据页后被重定向到登录页")
            if self._is_traffic_error_url(current_url):
                LOGGER.warning(
                    "[Shopee][周期页流量验证] 店铺=%s，时间范围=%s，url=%s；先重新跳转广告入口",
                    store_name,
                    period,
                    current_url,
                )
                navigation_result = self._reopen_period_after_traffic_error(
                    tab,
                    store_name,
                    period,
                    period_url,
                )
                if navigation_result is False:
                    last_failure_reason = f"流量验证恢复后重新打开{period}页面超时"
                    continue

            if not self._wait_for_page_ready(tab, AD_PAGE_LOAD_TIMEOUT_SECONDS):
                last_failure_reason = f"document.readyState 在 {AD_PAGE_LOAD_TIMEOUT_SECONDS} 秒内未达到 complete"
                continue

            current_url = self._read_current_url(tab)
            current_group = self._extract_group_from_url(current_url)
            if current_group != expected_group:
                last_failure_reason = f"当前URL的group={current_group or '<空>'}，期望={expected_group}"
                LOGGER.warning(
                    "[Shopee][周期URL验证失败] 店铺=%s，时间范围=%s，第 %s/%s 轮，%s，url=%s",
                    store_name,
                    period,
                    attempt_number,
                    max_attempts,
                    last_failure_reason,
                    current_url,
                )
                continue

            LOGGER.info(
                "[Shopee][周期页文档完成] 店铺=%s，时间范围=%s，第 %s/%s 轮，额外等待 %.1f 秒",
                store_name,
                period,
                attempt_number,
                max_attempts,
                AFTER_PAGE_READY_WAIT_SECONDS,
            )
            time.sleep(AFTER_PAGE_READY_WAIT_SECONDS)
            self._close_ad_popup(tab, f"{period}周期页第{attempt_number}轮加载完成后")

            ready_metric_text = self._read_xpath(tab, AD_PAGE_READY_METRIC_XPATH, f"{period}页面加载验证指标")
            if not ready_metric_text:
                last_failure_reason = f"首个指标未抓到非空文本，xpath={AD_PAGE_READY_METRIC_XPATH}"
                LOGGER.warning(
                    "[Shopee][周期数据验证失败] 店铺=%s，时间范围=%s，第 %s/%s 轮，原因=%s",
                    store_name,
                    period,
                    attempt_number,
                    max_attempts,
                    last_failure_reason,
                )
                continue

            LOGGER.info(
                "[Shopee][周期数据验证成功] 店铺=%s，时间范围=%s，group=%s，xpath=%s，原始文本=%r",
                store_name,
                period,
                current_group,
                AD_PAGE_READY_METRIC_XPATH,
                ready_metric_text,
            )
            return

        raise TimeoutError(
            f"Shopee 店铺 {store_name} 的{period}页面首次打开并刷新 "
            f"{PERIOD_PAGE_REFRESH_RETRY_TIMES} 次后仍无法抓取数据；最后原因={last_failure_reason or '未知'}"
        )

    def _reopen_ad_entry_after_traffic_error(self, tab: Any, store_name: str) -> None:
        """流量错误页只能重新跳转广告入口，禁止在错误页上执行 refresh。"""
        LOGGER.info("[Shopee][流量验证恢复] 店铺=%s，重新跳转url=%s", store_name, SHOPEE_AD_PAGE_URL)
        try:
            navigation_result = tab.get(SHOPEE_AD_PAGE_URL, timeout=AD_PAGE_LOAD_TIMEOUT_SECONDS)
        except Exception as exc:
            raise RuntimeError(f"Shopee 流量错误页重新跳转广告入口失败: {exc}") from exc
        if navigation_result is False:
            raise TimeoutError(f"Shopee 流量错误页重新跳转广告入口超过 {AD_PAGE_LOAD_TIMEOUT_SECONDS} 秒")
        if not self._wait_for_page_ready(tab, AD_PAGE_LOAD_TIMEOUT_SECONDS):
            raise TimeoutError("Shopee 流量错误页重新跳转广告入口后，页面未加载完成")

    def _reopen_period_after_traffic_error(
        self,
        tab: Any,
        store_name: str,
        period: str,
        period_url: str,
    ) -> Any:
        """从错误页先回广告入口，再重新跳指定周期；调用顺序不能改成刷新错误页。"""
        self._reopen_ad_entry_after_traffic_error(tab, store_name)
        LOGGER.info(
            "[Shopee][流量验证恢复] 店铺=%s，重新跳转%s周期url=%s",
            store_name,
            period,
            period_url,
        )
        return tab.get(period_url, timeout=AD_PAGE_LOAD_TIMEOUT_SECONDS)

    @staticmethod
    def _extract_group_from_url(current_url: str) -> str:
        """使用查询参数解析器读取 group，避免用字符串切割误伤其他参数。"""
        try:
            query_pairs = parse_qsl(urlsplit(str(current_url or "").strip()).query, keep_blank_values=True)
        except (TypeError, ValueError):
            return ""
        for key, value in query_pairs:
            if key.casefold() == "group":
                return value.strip().casefold()
        return ""

    @staticmethod
    def _replace_group_in_url(source_url: str, group_value: str) -> str:
        """只替换完整 URL 的 group 参数，保留 Shopee 生成的 from、to、type 等参数。"""
        parsed = urlsplit(str(source_url or "").strip())
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        replaced = False
        updated_pairs: list[tuple[str, str]] = []
        for key, value in query_pairs:
            if key.casefold() == "group":
                if not replaced:
                    updated_pairs.append((key, group_value))
                    replaced = True
                continue
            updated_pairs.append((key, value))
        if not replaced:
            updated_pairs.append(("group", group_value))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(updated_pairs), parsed.fragment))

    @staticmethod
    def _is_traffic_error_url(current_url: str) -> bool:
        """判断是否为 shopee.com.br/verify/traffic/error，忽略动态查询参数。"""
        try:
            current = urlsplit(str(current_url or "").strip())
            target = urlsplit(SHOPEE_TRAFFIC_ERROR_URL)
        except (TypeError, ValueError):
            return False
        return (
            current.netloc.casefold().split(":", 1)[0] == target.netloc.casefold()
            and current.path.rstrip("/").casefold() == target.path.rstrip("/").casefold()
        )

    @staticmethod
    def _read_current_url(tab: Any) -> str:
        """优先读取 DrissionPage 的 tab.url，失败时再读取 window.location.href。"""
        try:
            current_url = str(tab.url or "").strip()
            if current_url:
                return current_url
        except Exception as exc:
            LOGGER.warning("[Shopee][URL读取异常] tab.url 读取失败=%s，尝试读取 location.href", exc)
        try:
            return str(tab.run_js("return window.location.href;") or "").strip()
        except Exception as exc:
            LOGGER.warning("[Shopee][URL读取失败] location.href 读取失败=%s", exc)
            return ""

    @staticmethod
    def _classify_login_url(current_url: str) -> str:
        """返回 not_logged_in、logged_in 或 unknown；URL 中出现 login 即视为登录页。"""
        url_text = str(current_url or "").strip()
        if "login" in url_text.casefold():
            return "not_logged_in"
        try:
            parsed = urlparse(url_text)
        except (TypeError, ValueError):
            return "unknown"
        host = parsed.netloc.casefold().split(":", 1)[0]
        path = parsed.path.rstrip("/").casefold()
        login_parsed = urlparse(SHOPEE_LOGIN_PAGE_URL)
        if host == login_parsed.netloc.casefold() and path == login_parsed.path.rstrip("/").casefold():
            return "not_logged_in"
        if host == SHOPEE_SELLER_HOST:
            return "logged_in"
        return "unknown"

    def _close_ad_popup(self, tab: Any, check_position: str = "当前步骤") -> bool:
        """发现奖励广告弹窗时关闭；首次失败后最多重试 3 次，并验证关闭按钮已经消失。"""
        configured_xpaths = [str(xpath or "").strip() for xpath in AD_POPUP_CLOSE_XPATHS if str(xpath or "").strip()]
        if not configured_xpaths:
            LOGGER.warning("[Shopee][广告弹窗跳过] 检查位置=%s，关闭按钮 XPath 列表为空", check_position)
            return False

        max_attempts = CLICK_RETRY_TIMES + 1
        detected = False
        for attempt in range(max_attempts):
            active_xpath, close_element = self._find_ad_popup_close_element(
                tab,
                configured_xpaths,
                timeout=0.5,
                require_action_element=True,
            )
            if not close_element:
                if detected:
                    LOGGER.info(
                        "[Shopee][广告弹窗关闭成功] 检查位置=%s，关闭按钮已经消失",
                        check_position,
                    )
                    return True
                return False

            if not detected:
                LOGGER.warning(
                    "[Shopee][发现广告弹窗] 检查位置=%s，准备关闭，xpath=%s",
                    check_position,
                    active_xpath,
                )
                detected = True

            if attempt > 0:
                interval = CLICK_RETRY_INTERVAL_SECONDS + random.uniform(0, 1)
                LOGGER.warning(
                    "[Shopee][广告弹窗重试] 检查位置=%s，第 %s/%s 次点击前等待 %.2f 秒",
                    check_position,
                    attempt + 1,
                    max_attempts,
                    interval,
                )
                time.sleep(interval)

            try:
                LOGGER.info(
                    "[Shopee][广告弹窗点击] 检查位置=%s，第 %s/%s 次尝试，xpath=%s",
                    check_position,
                    attempt + 1,
                    max_attempts,
                    active_xpath,
                )
                self._click_element_with_fallback(
                    tab,
                    close_element,
                    active_xpath,
                    "关闭Shopee广告弹窗",
                )
                time.sleep(0.5)
                visible_xpath, visible_close_element = self._find_ad_popup_close_element(
                    tab,
                    configured_xpaths,
                    timeout=0.5,
                    require_action_element=False,
                )
                if not visible_close_element:
                    LOGGER.info(
                        "[Shopee][广告弹窗单层已关闭] 检查位置=%s，第 %s/%s 次点击后关闭按钮消失，继续观察 %.1f 秒",
                        check_position,
                        attempt + 1,
                        max_attempts,
                        AD_POPUP_CHAIN_WAIT_SECONDS,
                    )
                    followup_deadline = time.monotonic() + AD_POPUP_CHAIN_WAIT_SECONDS
                    followup_found = False
                    followup_xpath = ""
                    while time.monotonic() < followup_deadline:
                        followup_xpath, followup_element = self._find_ad_popup_close_element(
                            tab,
                            configured_xpaths,
                            timeout=0.25,
                            require_action_element=False,
                        )
                        if followup_element:
                            followup_found = True
                            break
                        time.sleep(0.25)
                    if followup_found:
                        LOGGER.warning(
                            "[Shopee][发现连续广告弹窗] 检查位置=%s，第一层关闭后又发现关闭按钮，继续关闭下一层，xpath=%s",
                            check_position,
                            followup_xpath,
                        )
                        continue
                    LOGGER.info(
                        "[Shopee][广告弹窗关闭成功] 检查位置=%s，第 %s/%s 次点击后连续 %.1f 秒未出现下一层弹窗",
                        check_position,
                        attempt + 1,
                        max_attempts,
                        AD_POPUP_CHAIN_WAIT_SECONDS,
                    )
                    return True
                LOGGER.warning(
                    "[Shopee][广告弹窗验证失败] 检查位置=%s，第 %s/%s 次点击后仍发现弹窗关闭按钮，xpath=%s",
                    check_position,
                    attempt + 1,
                    max_attempts,
                    visible_xpath,
                )
            except Exception as exc:
                LOGGER.warning(
                    "[Shopee][广告弹窗关闭异常] 检查位置=%s，第 %s/%s 次失败，异常=%s",
                    check_position,
                    attempt + 1,
                    max_attempts,
                    exc,
                )

        LOGGER.error(
            "[Shopee][广告弹窗关闭失败] 检查位置=%s，连续 %s 次点击后弹窗仍未确认关闭，xpaths=%s",
            check_position,
            max_attempts,
            configured_xpaths,
        )
        return False

    def _find_ad_popup_close_element(
        self,
        tab: Any,
        xpaths: list[str],
        timeout: float,
        require_action_element: bool,
    ) -> tuple[str, Any]:
        """按配置顺序查找弹窗关闭按钮，并返回实际命中的 XPath 和元素。"""
        for xpath_index, xpath in enumerate(xpaths, start=1):
            if require_action_element:
                element = self._find_action_element(
                    tab,
                    xpath,
                    timeout=timeout,
                    target_name=f"第{xpath_index}个广告弹窗关闭按钮",
                )
            else:
                element = self._find_visible_element(tab, xpath, timeout=timeout)
            if element:
                return xpath, element
        return "", None

    def _click_element_with_fallback(self, tab: Any, element: Any, xpath: str, step_name: str) -> None:
        """点击元素；无尺寸时依次尝试可点击父节点和 JavaScript click。"""
        try:
            element.click()
            return
        except Exception as first_error:
            LOGGER.warning(
                "[Shopee][按钮原始点击失败] 步骤=%s，xpath=%s，异常=%s，准备尝试父节点/JS回退",
                step_name,
                xpath,
                first_error,
            )

        # 日期菜单通常把文字放在 span 中，真正有尺寸和点击事件的是外层 li。
        for tag_name in ("li", "button", "a", "div"):
            ancestor_xpath = f"({xpath})/ancestor::{tag_name}[1]"
            ancestor = self._find_action_element(tab, ancestor_xpath, timeout=1, target_name=f"{step_name}的{tag_name}父节点")
            if not ancestor:
                continue
            try:
                ancestor.click()
                LOGGER.info("[Shopee][按钮回退成功] 步骤=%s，已点击 %s 父节点，xpath=%s", step_name, tag_name, ancestor_xpath)
                return
            except Exception as ancestor_error:
                LOGGER.warning(
                    "[Shopee][按钮父节点回退失败] 步骤=%s，父节点=%s，异常=%s",
                    step_name,
                    ancestor_xpath,
                    ancestor_error,
                )

        # 最后一层使用 DOM click，不依赖元素的屏幕坐标，适合无尺寸但有事件绑定的 span。
        try:
            xpath_literal = json.dumps(xpath, ensure_ascii=False)
            result = tab.run_js(
                f"""
                const target = document.evaluate(
                    {xpath_literal},
                    document,
                    null,
                    XPathResult.FIRST_ORDERED_NODE_TYPE,
                    null
                ).singleNodeValue;
                if (!target) return false;
                target.click();
                return true;
                """
            )
            if result is not False:
                LOGGER.info("[Shopee][按钮JS回退成功] 步骤=%s，xpath=%s", step_name, xpath)
                return
        except Exception as js_error:
            LOGGER.warning("[Shopee][按钮JS回退失败] 步骤=%s，异常=%s，xpath=%s", step_name, js_error, xpath)

        raise RuntimeError(f"原始点击和父节点/JS回退均失败：{first_error}")

    def _wait_for_page_ready(self, tab: Any, timeout_seconds: float) -> bool:
        """等待 Shopee 主文档加载完成。"""
        started_at = time.monotonic()
        deadline = started_at + timeout_seconds
        last_state = ""
        LOGGER.info("[Shopee][页面等待] 等待 document.readyState=complete，最长 %.1f 秒", timeout_seconds)
        while time.monotonic() < deadline:
            try:
                state = str(tab.run_js("return document.readyState;") or "").lower()
                if state != last_state:
                    LOGGER.info("[Shopee][页面状态] document.readyState=%s", state)
                    last_state = state
                if state == "complete":
                    LOGGER.info("[Shopee][页面成功] 页面加载完成，耗时 %.2f 秒", time.monotonic() - started_at)
                    return True
            except Exception as exc:
                LOGGER.warning("[Shopee][页面异常] 读取 document.readyState 失败：%s", exc)
            time.sleep(1)
        LOGGER.error("[Shopee][页面超时] 等待 %.1f 秒仍未加载完成，本轮页面加载判定失败", timeout_seconds)
        return False

    def _read_xpath(self, tab: Any, xpath: str, field_name: str) -> str:
        """读取指标两次；首次失败后等待 2 秒再读，最终失败返回空字符串。"""
        if not xpath:
            LOGGER.warning("[Shopee][指标跳过] 字段=%s，原因=XPath 为空", field_name)
            return ""
        for attempt in range(2):
            self._close_ad_popup(tab, f"读取指标{field_name}前")
            if attempt > 0:
                LOGGER.warning("[Shopee][指标重试] 字段=%s，第 2/2 次读取前等待 %s 秒，xpath=%s", field_name, CLICK_RETRY_INTERVAL_SECONDS, xpath)
                time.sleep(CLICK_RETRY_INTERVAL_SECONDS)
            try:
                LOGGER.info("[Shopee][指标查找] 字段=%s，第 %s/2 次读取，xpath=%s", field_name, attempt + 1, xpath)
                element = tab.ele(f"xpath:{xpath}", timeout=3)
                if element:
                    raw_text = str(element.text or "").strip()
                    if raw_text:
                        LOGGER.info("[Shopee][指标抓取成功] 字段=%s，原始文本=%r", field_name, raw_text)
                        return raw_text
                    LOGGER.warning("[Shopee][指标文本为空] 字段=%s，已找到元素但 text 为空", field_name)
                else:
                    LOGGER.warning("[Shopee][指标未找到] 字段=%s，第 %s/2 次未找到元素", field_name, attempt + 1)
            except Exception as exc:
                LOGGER.warning("[Shopee][指标异常] 字段=%s，第 %s/2 次失败，异常=%s，xpath=%s", field_name, attempt + 1, exc, xpath)
        LOGGER.error("[Shopee][指标最终失败] 字段=%s，两次读取均未得到文本，返回空字符串", field_name)
        return ""

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
            return element
        except Exception:
            return None

    def _find_action_element(self, tab: Any, xpath: str, timeout: float = 1, target_name: str = "") -> Any:
        """查找真正有位置和尺寸的元素；无尺寸时回退到最近可点击父节点。"""
        element = self._find_visible_element(tab, xpath, timeout=timeout)
        if not element:
            return None
        if self._element_has_geometry(element) and self._element_is_really_visible(tab, xpath, element):
            return element

        # 原 XPath 可能命中仅用于包裹文字的 span/div，优先选择菜单常见的可点击父节点。
        for tag_name in ("li", "button", "a", "div"):
            ancestor_xpath = f"({xpath})/ancestor::{tag_name}[1]"
            ancestor = self._find_visible_element(tab, ancestor_xpath, timeout=timeout)
            if ancestor and self._element_has_geometry(ancestor) and self._element_is_really_visible(tab, ancestor_xpath, ancestor):
                LOGGER.info(
                    "[Shopee][元素父节点回退] 目标=%s，原 XPath 无尺寸，改用 %s 父节点：%s",
                    target_name or xpath,
                    tag_name,
                    ancestor_xpath,
                )
                return ancestor
        return None

    @staticmethod
    def _element_has_geometry(element: Any) -> bool:
        """判断 DrissionPage 元素是否有可用于鼠标操作的位置和尺寸。"""
        try:
            rect = getattr(element, "rect", None)
            size = getattr(rect, "size", None) if rect is not None else None
            if size is None:
                # 某些 DrissionPage 版本没有暴露 rect.size，交给 click() 自己判断。
                return True
            width, height = float(size[0]), float(size[1])
            return width > 0 and height > 0
        except Exception:
            return False

    @staticmethod
    def _element_is_really_visible(tab: Any, xpath: str, element: Any) -> bool:
        """检查元素没有被 CSS 隐藏，并且至少有一部分位于当前浏览器视口内。"""
        try:
            xpath_literal = json.dumps(xpath, ensure_ascii=False)
            result = tab.run_js(
                f"""
                const target = document.evaluate(
                    {xpath_literal},
                    document,
                    null,
                    XPathResult.FIRST_ORDERED_NODE_TYPE,
                    null
                ).singleNodeValue;
                if (!target) return {{visible: false}};
                const rect = target.getBoundingClientRect();
                let current = target;
                while (current && current.nodeType === 1) {{
                    const style = window.getComputedStyle(current);
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {{
                        return {{visible: false, reason: 'css-hidden'}};
                    }}
                    current = current.parentElement;
                }}
                const inViewport = rect.width > 0 && rect.height > 0 &&
                    rect.bottom > 0 && rect.right > 0 &&
                    rect.top < window.innerHeight && rect.left < window.innerWidth;
                return {{visible: inViewport, width: rect.width, height: rect.height}};
                """
            )
            if isinstance(result, dict) and "visible" in result:
                return bool(result["visible"])
        except Exception:
            # run_js 不同版本返回值不同，回退到 DrissionPage 的尺寸判断。
            pass
        return ShopeeAuto._element_has_geometry(element)

    @staticmethod
    def _format_value(raw_text: str, kind: str) -> Any:
        """转换 Shopee 数值：金额/小数保留两位，百分比转为 0~1 比例，数量转为整数。"""
        if not raw_text:
            return ""
        if kind == "integer":
            return ShopeeAuto._parse_integer(raw_text)

        number = ShopeeAuto._parse_brazilian_number(raw_text)
        if number is None:
            return ""
        if kind == "currency":
            return round(number, 2)
        if kind == "decimal":
            return round(number, 2)
        if kind == "percent":
            # 页面 3,79% 转为内部比例 0.0379；发送飞书时再转换成 "3.8%" 字符串。
            ratio = number / 100 if "%" in str(raw_text) or abs(number) > 1 else number
            return round(ratio, 4)
        return raw_text

    @staticmethod
    def _format_display_value(value: Any, kind: str) -> str:
        """为机器人消息格式化单位；百分比内部比例在此转换成人可读文本。"""
        if value in ("", None):
            return ""
        try:
            if kind == "currency":
                # 巴西原始格式 R$17.490,26 规范显示为 R$17490.26。
                return f"R${float(value):.2f}"
            if kind == "percent":
                # 内部比例 0.0379 规范显示为 3.8%，与飞书文本字段保持一致。
                return f"{float(value) * 100:.1f}%"
            if kind == "decimal":
                return f"{float(value):.2f}"
            if kind == "integer":
                return str(int(value))
        except (TypeError, ValueError):
            return ""
        return str(value)

    @staticmethod
    def _parse_integer(raw_text: str) -> int | str:
        """解析整数和 Shopee 缩写数量，例如 10.7k -> 10700、1,2 mil -> 1200。"""
        text = str(raw_text or "").strip().lower()
        if not text or text == "-":
            return ""

        # k/m 是英文缩写，mil/mi 是巴西葡萄牙语页面可能使用的千/百万缩写。
        multiplier = 1
        suffix_match = re.search(r"\s*(k|mil|m|mi)\s*$", text, flags=re.IGNORECASE)
        if suffix_match:
            suffix = suffix_match.group(1).lower()
            multiplier = 1_000 if suffix in {"k", "mil"} else 1_000_000
            number_text = text[:suffix_match.start()].strip()
            number = ShopeeAuto._parse_brazilian_number(number_text)
            if number is None:
                return ""
            return int(round(number * multiplier))

        # 没有单位缩写时，点号和逗号按数量字段的千位分隔符处理。
        integer_text = re.sub(r"[^0-9-]", "", text)
        if not integer_text or integer_text == "-":
            return ""
        try:
            return int(integer_text)
        except ValueError:
            return ""

    @staticmethod
    def _parse_brazilian_number(raw_text: str) -> float | None:
        """解析巴西格式，例如 R$18.558,26 -> 18558.26，7,61 -> 7.61。"""
        text = str(raw_text).strip().replace("%", "")
        text = re.sub(r"[^0-9,.-]", "", text)
        if not text or text in {"-", ".", ","}:
            return None
        try:
            if "." in text and "," in text:
                text = text.replace(".", "").replace(",", ".")
            elif "," in text:
                text = text.replace(",", ".")
            return float(text)
        except (TypeError, ValueError):
            return None


def collect_shopee_ad(
    store_name: str,
    download_path: str = "",
    debugging_port: int | str | None = None,
) -> list[dict[str, Any]]:
    """提供一个可直接调用的 Shopee 函数入口。"""
    return ShopeeAuto().collect(store_name, download_path, debugging_port)
