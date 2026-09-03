"""TikTok 店铺广告和数据分析自动化。

本文件独立维护 TikTok 的 DrissionPage 连接、页面跳转、按钮点击、XPath、数值解析和飞书字段。
程序只接管紫鸟已经打开的当前标签页，并在该标签页中访问固定的广告页和数据概览页，复用紫鸟店铺登录态。

所有按钮和指标 XPath 都集中维护在本文件顶部，方便单独调整 TikTok 流程；
以后新增 XPath 时，如果 XPath 为空、元素不存在或读取失败，仍默认返回空值，不会中断其他步骤和指标。
"""

from __future__ import annotations

import ast
import base64
import io
import json
import logging
import mimetypes
import os
import random
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import requests
from DrissionPage import Chromium
from daily_store_check.config import is_period_enabled
from daily_store_check.human_interaction import HumanInteraction


LOGGER = logging.getLogger(__name__)


# TikTok 未登录页面中的邮箱入口、登录按钮和错误提示。
LOGIN_EMAIL_PANEL_BUTTON_XPATH = '//span[@id="TikTok_Ads_SSO_Login_Email_Panel_Button"]'
LOGIN_EMAIL_SELECTED_XPATH = (
    '//span[@id="TikTok_Ads_SSO_Login_Email_Panel_Button" '
    'and contains(@class,"panel-item") and contains(@class,"selected")]'
)
LOGIN_BUTTON_XPATH = '//button[@id="TikTok_Ads_SSO_Login_Btn"]'
# 只有出现“手机号格式错误”提示时才切换到邮箱登录；没有该提示时直接点击登录。
LOGIN_PHONE_FORMAT_ERROR_XPATH = '//span[contains(@class,"error-msg") and normalize-space(.)="请检查输入的手机号格式"]'

# TikTok 路由级登录状态。登录页可能附带 shop_region 等查询参数，
# 因此后续使用 URL 的 path 判断，不使用完整字符串相等。
TIKTOK_LOGIN_URL = "https://seller-br.tiktok.com/account/login"
TIKTOK_HOME_URL_PREFIX = "https://seller-br.tiktok.com/homepage"

# 登录提交后可能出现的“两个相同 3D 物体”验证码。
CAPTCHA_IMAGE_XPATH = '//img[@alt="captchaOpti_hCaptchaModal1_header"]'
CAPTCHA_CONFIRM_BUTTON_XPATH = '//button[normalize-space(.)="确认"]'
# 单次识别或提交失败后的额外重试次数；值为 2 表示最多处理 3 张验证码图片。
CAPTCHA_SOLVE_RETRY_TIMES = 3
# 验证码识别接口返回 data.res_str 字符串，例如 "[(135, 87), (287, 179)]"。
# 两个元组就是图片上的两个点击坐标，图片左上角为原点；坐标直接使用，不做平均或四舍五入。
# 这是识别接口的 words 参数，不是旧的视觉模型提示词；参考接口脚本要求随请求发送。
CAPTCHA_WORDS = "Select 2 objects that are the same shape"
# 点击验证码确认按钮后，等待业务菜单、验证码消失或验证码图片更新的最长秒数。
CAPTCHA_SUBMIT_RESULT_TIMEOUT_SECONDS = 15
# 登录按钮提交后，等待业务菜单、手机号错误或验证码出现的最长秒数。
LOGIN_SUBMIT_RESULT_TIMEOUT_SECONDS = 30

# 页面和按钮操作参数。
# 当当前路由是登录页时，连续检查多少次 URL；4 次、每次间隔 5 秒，总观察窗口约 20 秒。
LOGIN_URL_CHECK_TIMES = 30
# 登录页 URL 连续检查之间的等待时间；首页 URL 在中途出现时立即结束检查，不会等待完全部次数。
LOGIN_URL_CHECK_INTERVAL_SECONDS = 1
# 登录按钮或验证码处理完成后，等待 URL 进入 homepage 的最长时间。
LOGIN_SUCCESS_URL_TIMEOUT_SECONDS = 60
# 每次点击时间按钮或日期按钮后，最多等待多少秒确认状态；超时才进入重试。
# 用户要求所有按钮点击后的状态等待统一使用这个变量。
TK_STEP_WAIT_SECONDS = 2
# 调用 tab.get() 跳转广告页或概览页后，等待 URL 进入目标路径的最长时间。
BUSINESS_PAGE_URL_TIMEOUT_SECONDS = 120
# 已进入广告页或概览页目标路径后，等待该页面时间按钮出现的最长时间。
BUSINESS_PAGE_ELEMENT_TIMEOUT_SECONDS = 120
# 页面日期切换后，等待 document.readyState=complete 的最长时间。
DATA_PAGE_READY_TIMEOUT_SECONDS = 120
# 单个按钮首次点击失败后的额外重试次数；值为 3 时最多执行 1 次首次点击加 3 次重试。
CLICK_RETRY_TIMES = 20
# 同一按钮相邻两次重试之间的基础等待秒数；实际还会随机增加 0 至 1 秒。
CLICK_RETRY_INTERVAL_SECONDS = 2
# 数据加载完成标志首次未出现后允许的重查次数；值为 5 表示最多额外检查 5 次。
DATA_READY_RETRY_TIMES = 20
# 数据加载完成标志相邻两次检查的等待秒数；5 次重查最长等待约 10 秒。
DATA_READY_RETRY_INTERVAL_SECONDS = 2
# 日期切换后按指标数量等待数据，不满足数量时每秒检查一次，最多检查 60 次。
METRIC_COUNT_READY_CHECK_TIMES = 60
METRIC_COUNT_READY_INTERVAL_SECONDS = 1
# 指标数量满足后再给前端渲染和数值稳定留出的缓冲时间。
METRIC_COUNT_READY_EXTRA_WAIT_SECONDS = 3

# 任意两个真实按钮点击之间保持随机间隔，避免登录、菜单和日期按钮连续点击过快。
# 点击前还会把元素滚动到视口中间、移动鼠标到目标并短暂停留，尽量模拟人工操作。
# 两个真实按钮点击之间随机目标间隔的最小秒数；不足该间隔时程序会补足等待时间。
HUMAN_CLICK_INTERVAL_MIN_SECONDS = 2.5
# 两个真实按钮点击之间随机目标间隔的最大秒数；与上面的最小值共同生成随机间隔。
HUMAN_CLICK_INTERVAL_MAX_SECONDS = 4.5
# 本店铺首次点击前、以及按钮滚动到视口中心后的随机停留最小秒数。
HUMAN_PRE_CLICK_PAUSE_MIN_SECONDS = 0.5
# 本店铺首次点击前、以及按钮滚动到视口中心后的随机停留最大秒数。
HUMAN_PRE_CLICK_PAUSE_MAX_SECONDS = 1.2

# 确认店铺已经登录后，在紫鸟当前标签页中直接访问两个固定业务页面。
# 页面跳转不会创建新浏览器，也不会脱离紫鸟店铺环境。
AD_PAGE_URL = "https://seller-br.tiktok.com/ads-creation/dashboard"
OVERVIEW_PAGE_URL = "https://seller-br.tiktok.com/compass/data-overview"

# ---------------------------------------------------------------------------
# 一、店铺广告流程
# ---------------------------------------------------------------------------

# 广告页日期按钮 XPath。已登录后直接跳转广告页，不再查找或点击“营销/店铺广告”。
AD_TIME_BUTTON_XPATH = '//span[@class="theme-arco-picker-suffix-icon"]'
AD_YESTERDAY_BUTTON_XPATH = '(//button[contains(@class,"theme-arco-btn") and contains(@class,"theme-arco-btn-secondary") and contains(@class,"theme-arco-btn-size-mini") and contains(@class,"theme-arco-btn-shape-square")])[2]'
AD_7_DAYS_BUTTON_XPATH = '(//button[contains(@class,"theme-arco-btn") and contains(@class,"theme-arco-btn-secondary") and contains(@class,"theme-arco-btn-size-mini") and contains(@class,"theme-arco-btn-shape-square")])[3]'
AD_TODAY_BUTTON_XPATH = '(//button[contains(@class,"theme-arco-btn") and contains(@class,"theme-arco-btn-secondary") and contains(@class,"theme-arco-btn-size-mini") and contains(@class,"theme-arco-btn-shape-square")])[1]'
# 广告页面数据加载完成标志。该指标可见才表示广告页或切换后的日期数据已经加载完成。
# 后续页面结构变化时，只需要在这里替换成任意一个可靠的广告指标 XPath。
AD_DATA_READY_XPATH = '(//div[contains(@class,"overview-item")]/div[contains(@class,"overflow-tooltip-item")])[1]//span[starts-with(@class,"overview-item-value-")][contains(.,"USD")]'


# 店铺广告时间范围切换步骤。每个时间范围都预留“打开时间”和“选择范围”两次点击。
AD_PERIOD_CLICK_STEPS: dict[str, list[dict[str, Any]]] = {
    "昨天": [
        {
            "name": "广告-点击时间按钮",
            "xpath": AD_TIME_BUTTON_XPATH,
            "success_xpath": AD_YESTERDAY_BUTTON_XPATH,
            "success_state": "visible",
            "success_name": "昨天按钮已经出现",
        },
        {
            "name": "广告-点击昨天按钮",
            "xpath": AD_YESTERDAY_BUTTON_XPATH,
            "success_xpath": AD_YESTERDAY_BUTTON_XPATH,
            "success_state": "hidden",
            "success_name": "昨天按钮已经消失",
        },
    ],
    "7天": [
        {
            "name": "广告-再次点击时间按钮",
            "xpath": AD_TIME_BUTTON_XPATH,
            "success_xpath": AD_7_DAYS_BUTTON_XPATH,
            "success_state": "visible",
            "success_name": "最近7天按钮已经出现",
        },
        {
            "name": "广告-点击7天按钮",
            "xpath": AD_7_DAYS_BUTTON_XPATH,
            "success_xpath": AD_7_DAYS_BUTTON_XPATH,
            "success_state": "hidden",
            "success_name": "最近7天按钮已经消失",
        },
    ],
    "今天": [
        {
            "name": "广告-点击时间按钮（今天）",
            "xpath": AD_TIME_BUTTON_XPATH,
            "success_xpath": AD_TODAY_BUTTON_XPATH,
            "success_state": "visible",
            "success_name": "今天按钮已经出现",
        },
        {
            "name": "广告-点击今天按钮",
            "xpath": AD_TODAY_BUTTON_XPATH,
            "success_xpath": AD_TODAY_BUTTON_XPATH,
            "success_state": "hidden",
            "success_name": "今天按钮已经消失",
        },
    ],
}


# 广告金额使用美元 USD，不转换为巴西雷亚尔。
AD_METRIC_SPECS: list[dict[str, str]] = [
    {"period": "昨天", "field": "昨天成本", "xpath": '(//div[contains(@class,"overview-item")]/div[contains(@class,"overflow-tooltip-item")])[1]//span[starts-with(@class,"overview-item-value-")]', "kind": "currency", "currency_code": "USD"},
    {"period": "昨天", "field": "昨天SKU订单数", "xpath": '(//div[contains(@class,"overview-item")]/div[contains(@class,"overflow-tooltip-item")])[2]//span[starts-with(@class,"overview-item-value-")]', "kind": "integer"},
    {"period": "昨天", "field": "昨天均单价", "xpath": '(//div[contains(@class,"overview-item")]/div[contains(@class,"overflow-tooltip-item")])[3]//span[starts-with(@class,"overview-item-value-")]', "kind": "currency", "currency_code": "USD"},
    {"period": "昨天", "field": "昨天总收入", "xpath": '(//div[contains(@class,"overview-item")]/div[contains(@class,"overflow-tooltip-item")])[4]//span[starts-with(@class,"overview-item-value-")]', "kind": "currency", "currency_code": "USD"},
    {"period": "昨天", "field": "昨天ROI", "xpath": '(//div[contains(@class,"overview-item")]/div[contains(@class,"overflow-tooltip-item")])[5]//span[starts-with(@class,"overview-item-value-")]', "kind": "decimal"},
    {"period": "7天", "field": "7天成本", "xpath": '(//div[contains(@class,"overview-item")]/div[contains(@class,"overflow-tooltip-item")])[1]//span[starts-with(@class,"overview-item-value-")]', "kind": "currency", "currency_code": "USD"},
    {"period": "7天", "field": "7天SKU订单数", "xpath": '(//div[contains(@class,"overview-item")]/div[contains(@class,"overflow-tooltip-item")])[2]//span[starts-with(@class,"overview-item-value-")]', "kind": "integer"},
    {"period": "7天", "field": "7天均单价", "xpath": '(//div[contains(@class,"overview-item")]/div[contains(@class,"overflow-tooltip-item")])[3]//span[starts-with(@class,"overview-item-value-")]', "kind": "currency", "currency_code": "USD"},
    {"period": "7天", "field": "7天总收入", "xpath": '(//div[contains(@class,"overview-item")]/div[contains(@class,"overflow-tooltip-item")])[4]//span[starts-with(@class,"overview-item-value-")]', "kind": "currency", "currency_code": "USD"},
    {"period": "7天", "field": "7天ROI", "xpath": '(//div[contains(@class,"overview-item")]/div[contains(@class,"overflow-tooltip-item")])[5]//span[starts-with(@class,"overview-item-value-")]', "kind": "decimal"},
]
AD_METRIC_SPECS.extend(
    {**spec, "period": "今天", "field": spec["field"].replace("7天", "今天")}
    for spec in tuple(AD_METRIC_SPECS)
    if spec["period"] == "7天"
)
# AD_METRIC_SPECS: list[dict[str, str]] = [
#     {"period": "昨天", "field": "昨天成本", "xpath": '//div[normalize-space(.)="成本"]/ancestor::div[contains(@class,"overview-item")]//span[starts-with(@class,"overview-item-value-")][contains(.,"USD")]', "kind": "currency", "currency_code": "USD"},
#     {"period": "昨天", "field": "昨天SKU订单数", "xpath": '//div[normalize-space(.)="SKU 订单数"]/ancestor::div[contains(@class,"overview-item")]//span[starts-with(@class,"overview-item-value-")]', "kind": "integer"},
#     {"period": "昨天", "field": "昨天均单价", "xpath": '//div[normalize-space(.)="平均下单成本"]/ancestor::div[contains(@class,"overview-item")]//span[starts-with(@class,"overview-item-value-")][contains(.,"USD")]', "kind": "currency", "currency_code": "USD"},
#     {"period": "昨天", "field": "昨天总收入", "xpath": '//div[normalize-space(.)="总收入"]/ancestor::div[contains(@class,"overview-item")]//span[starts-with(@class,"overview-item-value-")][contains(.,"USD")]', "kind": "currency", "currency_code": "USD"},
#     {"period": "昨天", "field": "昨天ROI", "xpath": '//div[normalize-space(.)="ROI"]/ancestor::div[contains(@class,"overview-item")]//span[starts-with(@class,"overview-item-value-")]', "kind": "decimal"},
#     {"period": "7天", "field": "7天成本", "xpath": '//div[normalize-space(.)="成本"]/ancestor::div[contains(@class,"overview-item")]//span[starts-with(@class,"overview-item-value-")][contains(.,"USD")]', "kind": "currency", "currency_code": "USD"},
#     {"period": "7天", "field": "7天SKU订单数", "xpath": '//div[normalize-space(.)="SKU 订单数"]/ancestor::div[contains(@class,"overview-item")]//span[starts-with(@class,"overview-item-value-")]', "kind": "integer"},
#     {"period": "7天", "field": "7天均单价", "xpath": '//div[normalize-space(.)="平均下单成本"]/ancestor::div[contains(@class,"overview-item")]//span[starts-with(@class,"overview-item-value-")][contains(.,"USD")]', "kind": "currency", "currency_code": "USD"},
#     {"period": "7天", "field": "7天总收入", "xpath": '//div[normalize-space(.)="总收入"]/ancestor::div[contains(@class,"overview-item")]//span[starts-with(@class,"overview-item-value-")][contains(.,"USD")]', "kind": "currency", "currency_code": "USD"},
#     {"period": "7天", "field": "7天ROI", "xpath": '//div[normalize-space(.)="ROI"]/ancestor::div[contains(@class,"overview-item")]//span[starts-with(@class,"overview-item-value-")]', "kind": "decimal"},
# ]


# ---------------------------------------------------------------------------
# 二、数据分析 -> 概览流程
# ---------------------------------------------------------------------------

# 概览流程按钮 XPath。日期选项不可见时，会重新点击时间按钮并等待相应选项出现。
OVERVIEW_TIME_BUTTON_XPATH = '//div[@class="arco-picker-input"]/input'
OVERVIEW_YESTERDAY_BUTTON_XPATH = '//button[@data-testid="time-selector-yesterday"]//div'
OVERVIEW_7_DAYS_BUTTON_XPATH = '//button[@data-testid="time-selector-last-7-days"]//div'
OVERVIEW_TODAY_BUTTON_XPATH = '//button[@data-testid="time-selector-today"]//div'
# 概览页面数据加载完成标志。该指标可见才表示概览页或切换后的日期数据已经加载完成。
# 后续页面结构变化时，只需要在这里替换成任意一个可靠的概览指标 XPath。
OVERVIEW_DATA_READY_XPATH = '(//label[contains(@class,"pcm-smc-card-group-item")])[1]//div[@class="pcm-smc-content"]'


# 数据概览时间范围切换步骤。
OVERVIEW_PERIOD_CLICK_STEPS: dict[str, list[dict[str, Any]]] = {
    "昨天": [
        {
            "name": "概览-点击时间按钮",
            "xpath": OVERVIEW_TIME_BUTTON_XPATH,
            "success_xpath": OVERVIEW_YESTERDAY_BUTTON_XPATH,
            "success_state": "visible",
            "success_name": "概览昨天按钮已经出现",
        },
        {
            "name": "概览-点击昨天按钮",
            "xpath": OVERVIEW_YESTERDAY_BUTTON_XPATH,
            "success_xpath": OVERVIEW_YESTERDAY_BUTTON_XPATH,
            "success_state": "hidden",
            "success_name": "概览昨天按钮已经消失",
        },
    ],
    "7天": [
        {
            "name": "概览-再次点击时间按钮",
            "xpath": OVERVIEW_TIME_BUTTON_XPATH,
            "success_xpath": OVERVIEW_7_DAYS_BUTTON_XPATH,
            "success_state": "visible",
            "success_name": "概览最近7天按钮已经出现",
        },
        {
            "name": "概览-点击7天按钮",
            "xpath": OVERVIEW_7_DAYS_BUTTON_XPATH,
            "success_xpath": OVERVIEW_7_DAYS_BUTTON_XPATH,
            "success_state": "hidden",
            "success_name": "概览最近7天按钮已经消失",
        },
    ],
    "今天": [
        {
            "name": "概览-点击时间按钮（今天）",
            "xpath": OVERVIEW_TIME_BUTTON_XPATH,
            "success_xpath": OVERVIEW_TODAY_BUTTON_XPATH,
            "success_state": "visible",
            "success_name": "概览今天按钮已经出现",
        },
        {
            "name": "概览-点击今天按钮",
            "xpath": OVERVIEW_TODAY_BUTTON_XPATH,
            "success_xpath": OVERVIEW_TODAY_BUTTON_XPATH,
            "success_state": "hidden",
            "success_name": "概览今天按钮已经消失",
        },
    ],
}


# 概览中的 GMV 使用巴西雷亚尔 BRL；数量为整数。
# “直播/视频/商品卡”三个 XPath 实际抓到的是渠道 GMV 金额，不是页面占比。
# 昨天的渠道金额直接使用飞书已有字段名；7 天金额仅作为计算占比的临时值，计算后会从飞书字段中移除。
OVERVIEW_METRIC_SPECS: list[dict[str, str]] = [
    {"period": "昨天", "field": "昨天GMV", "xpath": '(//label[contains(@class,"pcm-smc-card-group-item")])[1]//div[@class="pcm-smc-value-content"]', "kind": "currency", "currency_code": "BRL"},
    {"period": "昨天", "field": "昨天成交件数", "xpath": '(//label[contains(@class,"pcm-smc-card-group-item")])[2]//div[@class="pcm-smc-value-content"]', "kind": "integer"},
    {"period": "昨天", "field": "昨天SKU订单数", "xpath": '(//label[contains(@class,"pcm-smc-card-group-item")])[3]//div[@class="pcm-smc-value-content"]', "kind": "integer"},
    {"period": "昨天", "field": "昨天订单数", "xpath": '(//label[contains(@class,"pcm-smc-card-group-item")])[4]//div[@class="pcm-smc-value-content"]', "kind": "integer"},
    {"period": "昨天", "field": "昨天客户数", "xpath": '(//label[contains(@class,"pcm-smc-card-group-item")])[5]//div[@class="pcm-smc-value-content"]', "kind": "integer"},
    {"period": "昨天", "field": "昨天商品访客数", "xpath": '(//label[contains(@class,"pcm-smc-card-group-item")])[6]//div[@class="pcm-smc-value-content"]', "kind": "integer"},
    {"period": "昨天", "field": "昨天曝光数", "xpath": '(//label[contains(@class,"pcm-smc-card-group-item")])[7]//div[@class="pcm-smc-value-content"]', "kind": "integer"},
    {"period": "昨天", "field": "昨天去重曝光数", "xpath": '(//label[contains(@class,"pcm-smc-card-group-item")])[8]//div[@class="pcm-smc-value-content"]', "kind": "integer"},

    {"period": "昨天", "field": "直播GMV", "xpath": '(//tr[contains(@class,"core-table-tr")])[1]//div[contains(@class,"text-body-m-medium")]', "kind": "currency", "currency_code": "BRL"},
    {"period": "昨天", "field": "短视频GMV", "xpath": '(//tr[contains(@class,"core-table-tr")])[2]//div[contains(@class,"text-body-m-medium")]', "kind": "currency", "currency_code": "BRL"},
    {"period": "昨天", "field": "商品卡GMV", "xpath": '(//tr[contains(@class,"core-table-tr")])[3]//div[contains(@class,"text-body-m-medium")]', "kind": "currency", "currency_code": "BRL"},

    {"period": "7天", "field": "7天GMV", "xpath": '(//label[contains(@class,"pcm-smc-card-group-item")])[1]//div[@class="pcm-smc-value-content"]', "kind": "currency", "currency_code": "BRL"},
    {"period": "7天", "field": "7天成交件数", "xpath": '(//label[contains(@class,"pcm-smc-card-group-item")])[2]//div[@class="pcm-smc-value-content"]', "kind": "integer"},
    {"period": "7天", "field": "7天SKU订单数", "xpath": '(//label[contains(@class,"pcm-smc-card-group-item")])[3]//div[@class="pcm-smc-value-content"]', "kind": "integer"},
    {"period": "7天", "field": "7天订单数", "xpath": '(//label[contains(@class,"pcm-smc-card-group-item")])[4]//div[@class="pcm-smc-value-content"]', "kind": "integer"},
    {"period": "7天", "field": "7天客户数", "xpath": '(//label[contains(@class,"pcm-smc-card-group-item")])[5]//div[@class="pcm-smc-value-content"]', "kind": "integer"},
    {"period": "7天", "field": "7天商品访客数", "xpath": '(//label[contains(@class,"pcm-smc-card-group-item")])[6]//div[@class="pcm-smc-value-content"]', "kind": "integer"},
    {"period": "7天", "field": "7天曝光数", "xpath": '(//label[contains(@class,"pcm-smc-card-group-item")])[7]//div[@class="pcm-smc-value-content"]', "kind": "integer"},
    {"period": "7天", "field": "7天去重曝光数", "xpath": '(//label[contains(@class,"pcm-smc-card-group-item")])[8]//div[@class="pcm-smc-value-content"]', "kind": "integer"},

    {"period": "7天", "field": "_7天直播GMV", "xpath": '(//tr[contains(@class,"core-table-tr")])[1]//div[contains(@class,"text-body-m-medium")]', "kind": "currency", "currency_code": "BRL"},
    {"period": "7天", "field": "_7天短视频GMV", "xpath": '(//tr[contains(@class,"core-table-tr")])[2]//div[contains(@class,"text-body-m-medium")]', "kind": "currency", "currency_code": "BRL"},
    {"period": "7天", "field": "_7天商品卡GMV", "xpath": '(//tr[contains(@class,"core-table-tr")])[3]//div[contains(@class,"text-body-m-medium")]', "kind": "currency", "currency_code": "BRL"},
]

# 今天沿用与最近 7 天相同的指标 XPath，只更换字段前缀，便于消息中单独展示。
OVERVIEW_METRIC_SPECS.extend(
    {**spec, "period": "今天", "field": spec["field"].replace("7天", "今天")}
    for spec in tuple(OVERVIEW_METRIC_SPECS)
    if spec["period"] == "7天"
)
# OVERVIEW_METRIC_SPECS: list[dict[str, str]] = [
#     {"period": "昨天", "field": "昨天GMV", "xpath": '(//label[contains(@class,"core-checkbox")])[1]//div[@class="pcm-smc-content"]', "kind": "currency", "currency_code": "BRL"},
#     {"period": "昨天", "field": "昨天成交件数", "xpath": '//div[@class="pcm-smc"][contains(.,"商品成交件数")]//div[@class="pcm-smc-value-content"]', "kind": "integer"},
#     {"period": "昨天", "field": "昨天SKU订单数", "xpath": '//div[@class="pcm-smc"][contains(.,"SKU 订单数")]//div[@class="pcm-smc-value-content"]', "kind": "integer"},
#     {"period": "昨天", "field": "昨天订单数", "xpath": '//*[normalize-space(.)="订单数"]/ancestor::div[contains(@class,"pcm-smc")][1]//div[contains(@class,"pcm-smc-value-content")]', "kind": "integer"},
#     {"period": "昨天", "field": "昨天客户数", "xpath": '//*[normalize-space(.)="客户数"]/ancestor::div[contains(@class,"pcm-smc")][1]//div[contains(@class,"pcm-smc-value-content")]', "kind": "integer"},
#     {"period": "昨天", "field": "昨天商品访客数", "xpath": '//*[normalize-space(.)="商品访客数"]/ancestor::div[contains(@class,"pcm-smc")][1]//div[contains(@class,"pcm-smc-value-content")]', "kind": "integer"},
#     {"period": "昨天", "field": "昨天曝光数", "xpath": '//*[normalize-space(.)="商品曝光次数"]/ancestor::div[contains(@class,"pcm-smc")][1]//div[contains(@class,"pcm-smc-value-content")]', "kind": "integer"},
#     {"period": "昨天", "field": "昨天去重曝光数", "xpath": '//*[normalize-space(.)="去重商品曝光次数"]/ancestor::div[contains(@class,"pcm-smc")][1]//div[contains(@class,"pcm-smc-value-content")]', "kind": "integer"},
#     {"period": "昨天", "field": "直播GMV", "xpath": '//*[normalize-space(.)="直播"]/ancestor::td[contains(@class,"core-table-td")][1]//div[contains(@class,"text-body-m-medium")]', "kind": "currency", "currency_code": "BRL"},
#     {"period": "昨天", "field": "短视频GMV", "xpath": '//*[normalize-space(.)="视频"]/ancestor::td[contains(@class,"core-table-td")][1]//div[contains(@class,"text-body-m-medium")]', "kind": "currency", "currency_code": "BRL"},
#     {"period": "昨天", "field": "商品卡GMV", "xpath": '//*[normalize-space(.)="商品卡"]/ancestor::td[contains(@class,"core-table-td")][1]//div[contains(@class,"text-body-m-medium")]', "kind": "currency", "currency_code": "BRL"},
#     {"period": "7天", "field": "7天GMV", "xpath": '//div[@class="pcm-smc"][contains(.,"GMV")]//div[@class="pcm-smc-content"]', "kind": "currency", "currency_code": "BRL"},
#     {"period": "7天", "field": "7天成交件数", "xpath": '//div[@class="pcm-smc"][contains(.,"商品成交件数")]//div[@class="pcm-smc-value-content"]', "kind": "integer"},
#     {"period": "7天", "field": "7天SKU订单数", "xpath": '//div[@class="pcm-smc"][contains(.,"SKU 订单数")]//div[@class="pcm-smc-value-content"]', "kind": "integer"},
#     {"period": "7天", "field": "7天订单数", "xpath": '//*[normalize-space(.)="订单数"]/ancestor::div[contains(@class,"pcm-smc")][1]//div[contains(@class,"pcm-smc-value-content")]', "kind": "integer"},
#     {"period": "7天", "field": "7天客户数", "xpath": '//*[normalize-space(.)="客户数"]/ancestor::div[contains(@class,"pcm-smc")][1]//div[contains(@class,"pcm-smc-value-content")]', "kind": "integer"},
#     {"period": "7天", "field": "7天商品访客数", "xpath": '//*[normalize-space(.)="商品访客数"]/ancestor::div[contains(@class,"pcm-smc")][1]//div[contains(@class,"pcm-smc-value-content")]', "kind": "integer"},
#     {"period": "7天", "field": "7天曝光数", "xpath": '//*[normalize-space(.)="商品曝光次数"]/ancestor::div[contains(@class,"pcm-smc")][1]//div[contains(@class,"pcm-smc-value-content")]', "kind": "integer"},
#     {"period": "7天", "field": "7天去重曝光数", "xpath": '//*[normalize-space(.)="去重商品曝光次数"]/ancestor::div[contains(@class,"pcm-smc")][1]//div[contains(@class,"pcm-smc-value-content")]', "kind": "integer"},
#     {"period": "7天", "field": "_7天直播GMV", "xpath": '//*[normalize-space(.)="直播"]/ancestor::td[contains(@class,"core-table-td")][1]//div[contains(@class,"text-body-m-medium")]', "kind": "currency", "currency_code": "BRL"},
#     {"period": "7天", "field": "_7天短视频GMV", "xpath": '//*[normalize-space(.)="视频"]/ancestor::td[contains(@class,"core-table-td")][1]//div[contains(@class,"text-body-m-medium")]', "kind": "currency", "currency_code": "BRL"},
#     {"period": "7天", "field": "_7天商品卡GMV", "xpath": '//*[normalize-space(.)="商品卡"]/ancestor::td[contains(@class,"core-table-td")][1]//div[contains(@class,"text-body-m-medium")]', "kind": "currency", "currency_code": "BRL"},
# ]

# 每个占比都由“同周期渠道 GMV / 同周期总 GMV * 100”计算，结果保留两位小数。
# 7天渠道金额会保留给机器人消息使用，多维表打包时自动忽略这些内部字段。
OVERVIEW_GMV_RATIO_SPECS: dict[str, list[dict[str, Any]]] = {
    "昨天": [
        {"amount_field": "直播GMV", "ratio_field": "昨天GMV直播比"},
        {"amount_field": "短视频GMV", "ratio_field": "昨天GMV视频比"},
        {"amount_field": "商品卡GMV", "ratio_field": "昨天GMV商品卡比"},
    ],
    "7天": [
        {"amount_field": "_7天直播GMV", "ratio_field": "7天GMV直播比"},
        {"amount_field": "_7天短视频GMV", "ratio_field": "7天GMV视频比"},
        {"amount_field": "_7天商品卡GMV", "ratio_field": "7天GMV商品卡比"},
    ],
    "今天": [
        {"amount_field": "_今天直播GMV", "ratio_field": "今天GMV直播比"},
        {"amount_field": "_今天短视频GMV", "ratio_field": "今天GMV视频比"},
        {"amount_field": "_今天商品卡GMV", "ratio_field": "今天GMV商品卡比"},
    ],
}


class TiktokAuto:
    """TikTok 店铺广告和数据概览自动化。"""

    def __init__(self, config: dict[str, Any] | None = None):
        """保存 TikTok 独立配置；按钮和指标 XPath 直接维护在本文件顶部。"""
        self.config = config or {}
        self._human_interaction = HumanInteraction(self.config.get("human_interaction", {}))
        # 记录最近一次真实点击的时间；后续每次点击都会据此补足随机的人类操作间隔。
        self._last_button_click_at: float | None = None

    def collect(self, store_name: str, download_path: str = "", debugging_port: int | str | None = None) -> list[dict[str, Any]]:
        """接管紫鸟当前标签页，依次采集广告数据和数据分析概览。"""
        if not debugging_port:
            raise RuntimeError("紫鸟没有返回 debuggingPort，无法接管 TikTok 店铺")

        # 每间店铺重新计算点击节奏，避免上一间店铺的关闭时间影响当前店铺。
        self._last_button_click_at = None

        LOGGER.info("[TikTok][开始] 店铺=%s，准备接管紫鸟浏览器，debugging_port=%s", store_name, debugging_port)

        # 连接紫鸟已经打开的 Chromium，不创建普通浏览器；后续固定业务页面仍在这个紫鸟标签页中打开。
        browser = Chromium(f"127.0.0.1:{debugging_port}")
        tab = browser.latest_tab
        collected_at = datetime.now(timezone.utc).isoformat()
        LOGGER.info("[TikTok][浏览器] 店铺=%s，已取得紫鸟当前标签页", store_name)

        # 登录判断阶段按配置的分阶段轮询规则确认当前店铺是否已经登录。
        login_required = self._is_login_required(tab)
        if login_required:
            self._perform_login(tab)
        else:
            LOGGER.info("[TikTok][登录确认] URL 已确认进入 homepage，直接执行固定页面流程")

        # 1. 确认登录后直接进入广告页，不再点击“营销 -> 店铺广告”。
        LOGGER.info("[TikTok][广告] 已确认登录，直接打开广告页=%s", AD_PAGE_URL)
        ad_fields: dict[str, Any] = {}
        ad_raw_values: dict[str, str] = {}
        enabled_ad_periods = tuple(
            period for period in ("昨天", "7天", "今天") if is_period_enabled(self.config, period)
        )
        ad_page_ready = bool(enabled_ad_periods) and self._open_business_page(
            tab, AD_PAGE_URL, AD_TIME_BUTTON_XPATH, "TikTok广告页时间面板按钮"
        )
        if not ad_page_ready:
            LOGGER.error("[TikTok][广告页面失败] 直接打开广告页后未确认时间面板按钮出现，本店铺两个广告周期均按空值处理")
        else:
            LOGGER.info("[TikTok][广告页面就绪] 时间面板按钮已经出现，可以开始切换日期")

        for period in ("昨天", "7天", "今天"):
            if not is_period_enabled(self.config, period):
                LOGGER.info("[TikTok][广告] 时间范围=%s 已按配置关闭，跳过", period)
                continue
            LOGGER.info("[TikTok][广告] 开始切换并采集时间范围=%s", period)
            if not ad_page_ready:
                self._record_empty_period_metrics(period, AD_METRIC_SPECS, ad_fields, ad_raw_values, "未进入店铺广告页面")
                continue

            period_steps = AD_PERIOD_CLICK_STEPS.get(period, [])
            period_clicked = self._run_period_click_steps(tab, period_steps)
            if not period_clicked:
                self._record_empty_period_metrics(period, AD_METRIC_SPECS, ad_fields, ad_raw_values, "日期按钮点击或状态验证失败")
                continue

            self._wait_for_document_complete_after_period(tab, f"广告-{period}")
            period_specs = [spec for spec in AD_METRIC_SPECS if spec["period"] == period]
            data_loaded = self._wait_for_required_metric_count(
                tab,
                period,
                period_specs,
                f"广告-{period}",
            )
            if not data_loaded:
                self._record_empty_period_metrics(period, AD_METRIC_SPECS, ad_fields, ad_raw_values, "广告数据未确认加载完成")
                continue
            self._collect_period_metrics(tab, period, AD_METRIC_SPECS, ad_fields, ad_raw_values)

        ad_row = {
            "店铺名": store_name,
            "平台": "tiktok",
            "采集时间": collected_at,
            "指标": "TikTok店铺广告",
            "数值": "",
            "原始数据": json.dumps(ad_raw_values, ensure_ascii=False),
            "飞书字段": ad_fields,
        }

        # 2. 直接进入数据概览页，不再点击“数据分析 -> 概览”。
        LOGGER.info("[TikTok][概览] 直接打开数据概览页=%s", OVERVIEW_PAGE_URL)
        overview_fields: dict[str, Any] = {}
        overview_raw_values: dict[str, str] = {}
        enabled_overview_periods = tuple(
            period for period in ("昨天", "7天", "今天") if is_period_enabled(self.config, period)
        )
        overview_page_ready = bool(enabled_overview_periods) and self._open_business_page(
            tab, OVERVIEW_PAGE_URL, OVERVIEW_TIME_BUTTON_XPATH, "TikTok数据概览页时间面板按钮"
        )
        if not overview_page_ready:
            LOGGER.error("[TikTok][概览页面失败] 直接打开数据概览页后未确认时间面板按钮出现，本店铺两个概览周期均按空值处理")
        else:
            LOGGER.info("[TikTok][概览页面就绪] 时间面板按钮已经出现，可以开始切换日期")

        for period in ("昨天", "7天", "今天"):
            if not is_period_enabled(self.config, period):
                LOGGER.info("[TikTok][概览] 时间范围=%s 已按配置关闭，跳过", period)
                continue
            LOGGER.info("[TikTok][概览] 开始切换并采集时间范围=%s", period)
            if not overview_page_ready:
                self._record_empty_period_metrics(
                    period,
                    OVERVIEW_METRIC_SPECS,
                    overview_fields,
                    overview_raw_values,
                    "未进入数据分析概览页面",
                )
                continue

            period_steps = OVERVIEW_PERIOD_CLICK_STEPS.get(period, [])
            period_clicked = self._run_period_click_steps(tab, period_steps)
            if not period_clicked:
                self._record_empty_period_metrics(
                    period,
                    OVERVIEW_METRIC_SPECS,
                    overview_fields,
                    overview_raw_values,
                    "概览日期按钮点击或状态验证失败",
                )
                continue

            self._wait_for_document_complete_after_period(tab, f"概览-{period}")
            period_specs = [spec for spec in OVERVIEW_METRIC_SPECS if spec["period"] == period]
            data_loaded = self._wait_for_required_metric_count(
                tab,
                period,
                period_specs,
                f"概览-{period}",
            )
            if not data_loaded:
                self._record_empty_period_metrics(
                    period,
                    OVERVIEW_METRIC_SPECS,
                    overview_fields,
                    overview_raw_values,
                    "概览数据加载完成标志未出现",
                )
                continue

            self._collect_period_metrics(tab, period, OVERVIEW_METRIC_SPECS, overview_fields, overview_raw_values)
            self._calculate_gmv_ratios(period, overview_fields, overview_raw_values)

        overview_row = {
            "店铺名": store_name,
            "平台": "tiktok",
            "采集时间": collected_at,
            "指标": "TikTok数据分析概览",
            "数值": "",
            "原始数据": json.dumps(overview_raw_values, ensure_ascii=False),
            "飞书字段": overview_fields,
        }

        # 两个模块分别返回一条记录，避免同名字段（如 SKU订单数）相互覆盖。
        ad_valid_count = sum(value != "" for value in ad_fields.values())
        overview_valid_count = sum(value != "" for value in overview_fields.values())
        LOGGER.info(
            "[TikTok][完成] 店铺=%s，广告有效指标=%s/%s，概览有效指标=%s/%s",
            store_name,
            ad_valid_count,
            len(ad_fields),
            overview_valid_count,
            len(overview_fields),
        )
        return [ad_row, overview_row]

    def _wait_for_document_complete_after_period(self, tab: Any, step_name: str) -> bool:
        """日期切换后等待页面完成；超时会记录日志，数据标志仍作为最终抓取门槛。"""
        deadline = time.monotonic() + DATA_PAGE_READY_TIMEOUT_SECONDS
        check_count = 0
        LOGGER.info(
            "[TikTok][日期页面加载等待] 步骤=%s，最长 %.1f 秒等待 document.readyState=complete",
            step_name,
            DATA_PAGE_READY_TIMEOUT_SECONDS,
        )
        while time.monotonic() < deadline:
            check_count += 1
            try:
                ready_state = str(tab.run_js("return document.readyState;") or "").strip().lower()
            except Exception as exc:
                ready_state = ""
                LOGGER.warning("[TikTok][日期页面加载异常] 步骤=%s，第%s次检查异常=%s", step_name, check_count, exc)
            if ready_state == "complete":
                LOGGER.info("[TikTok][日期页面加载完成] 步骤=%s，第%s次检查成功", step_name, check_count)
                return True
            self._human_wait(tab, 0.5)
        LOGGER.warning(
            "[TikTok][日期页面加载超时] 步骤=%s，%.1f 秒内未达到 complete，继续等待数据标志",
            step_name,
            DATA_PAGE_READY_TIMEOUT_SECONDS,
        )
        return False

    def _collect_period_metrics(
        self,
        tab: Any,
        period: str,
        specs: list[dict[str, str]],
        field_values: dict[str, Any],
        raw_values: dict[str, str],
    ) -> None:
        """通过一次浏览器脚本读取某模块、某时间范围的全部指标，再逐字段转换和记录日志。"""
        period_specs = [spec for spec in specs if spec["period"] == period]
        batch_raw_values = self._read_metric_batch(tab, period, period_specs)
        for spec in period_specs:
            field_name = spec["field"]
            xpath = spec["xpath"]
            value_kind = spec["kind"]
            currency_code = spec.get("currency_code", "")
            LOGGER.info(
                "[TikTok][指标] 准备读取：时间范围=%s，字段=%s，配置类型=%s，币种=%s，xpath=%s",
                period,
                field_name,
                value_kind,
                currency_code or "无",
                xpath or "<空 XPath>",
            )
            raw_text = batch_raw_values.get(field_name, "")
            converted_value = self._format_value(raw_text, value_kind, currency_code)
            raw_values[field_name] = raw_text
            field_values[field_name] = converted_value
            LOGGER.info(
                "[TikTok][指标结果] 字段=%s，原始值=%r，原始类型=%s，转换值=%r，转换后类型=%s，配置类型=%s，币种=%s",
                field_name,
                raw_text,
                type(raw_text).__name__,
                converted_value,
                type(converted_value).__name__,
                value_kind,
                currency_code or "无",
            )
            if raw_text == "":
                LOGGER.warning("[TikTok][指标失败] 字段=%s，XPath 未抓到有效文本，最终按空值处理", field_name)
            elif converted_value == "":
                LOGGER.warning(
                    "[TikTok][转换失败] 字段=%s，已抓到原始值=%r，但无法按配置类型=%s 转换，最终按空值处理",
                    field_name,
                    raw_text,
                    value_kind,
                )

    def _read_metric_batch(
        self,
        tab: Any,
        period: str,
        specs: list[dict[str, str]],
    ) -> dict[str, str]:
        """在页面内一次执行全部 XPath；整批脚本异常时才回退为逐项读取。"""
        result = {str(spec.get("field") or ""): "" for spec in specs}
        configured_specs = [
            {"field": str(spec.get("field") or ""), "xpath": str(spec.get("xpath") or "").strip()}
            for spec in specs
            if str(spec.get("field") or "") and str(spec.get("xpath") or "").strip()
        ]
        empty_xpath_fields = [
            str(spec.get("field") or "")
            for spec in specs
            if str(spec.get("field") or "") and not str(spec.get("xpath") or "").strip()
        ]
        for field_name in empty_xpath_fields:
            LOGGER.warning("[TikTok][批量指标跳过] 时间范围=%s，字段=%s，原因=XPath 为空", period, field_name)

        if not configured_specs:
            LOGGER.warning("[TikTok][批量指标] 时间范围=%s，没有可执行的指标 XPath，全部返回空值", period)
            return result

        specs_json = json.dumps(configured_specs, ensure_ascii=False)
        script = f"""
            const metricSpecs = {specs_json};
            const batchResult = {{}};
            for (const metric of metricSpecs) {{
                try {{
                    const node = document.evaluate(
                        metric.xpath,
                        document,
                        null,
                        XPathResult.FIRST_ORDERED_NODE_TYPE,
                        null
                    ).singleNodeValue;
                    if (!node) {{
                        batchResult[metric.field] = "";
                        continue;
                    }}
                    const isInput = node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement;
                    const text = isInput ? node.value : (node.innerText ?? node.textContent ?? "");
                    batchResult[metric.field] = String(text).trim();
                }} catch (error) {{
                    batchResult[metric.field] = "";
                }}
            }}
            return JSON.stringify(batchResult);
        """
        try:
            started_at = time.monotonic()
            response = tab.run_js(script)
            parsed = json.loads(str(response or "{}"))
            if not isinstance(parsed, dict):
                raise TypeError(f"批量脚本返回类型不是字典: {type(parsed).__name__}")
            for field_name in result:
                result[field_name] = str(parsed.get(field_name) or "").strip()
            LOGGER.info(
                "[TikTok][批量指标读取完成] 时间范围=%s，一次读取=%s项，非空=%s项，耗时=%.2f秒，原始结果=%s",
                period,
                len(configured_specs),
                sum(bool(value) for value in result.values()),
                time.monotonic() - started_at,
                json.dumps(result, ensure_ascii=False),
            )
            for metric in configured_specs:
                field_name = metric["field"]
                raw_text = result.get(field_name, "")
                LOGGER.info(
                    "[TikTok][XPath原始数据] 时间范围=%s，字段=%s，XPath=%s，抓取原始数据=%r，原始类型=%s",
                    period,
                    field_name,
                    metric["xpath"],
                    raw_text,
                    type(raw_text).__name__,
                )
            return result
        except Exception as exc:
            LOGGER.exception(
                "[TikTok][批量指标异常] 时间范围=%s，一次读取失败，回退为逐项读取，异常=%s",
                period,
                exc,
            )
            for spec in specs:
                field_name = str(spec.get("field") or "")
                result[field_name] = self._read_xpath(tab, str(spec.get("xpath") or ""), field_name)
            return result

    @staticmethod
    def _record_empty_period_metrics(
        period: str,
        specs: list[dict[str, str]],
        field_values: dict[str, Any],
        raw_values: dict[str, str],
        reason: str,
    ) -> None:
        """流程未通过加载验证时写入空值，防止误用上一个日期范围的旧数据。"""
        empty_fields: list[str] = []
        for spec in specs:
            if spec["period"] != period:
                continue
            field_name = spec["field"]
            field_values[field_name] = ""
            raw_values[field_name] = ""
            empty_fields.append(field_name)
        LOGGER.error("[TikTok][周期数据置空] 时间范围=%s，原因=%s，字段=%s", period, reason, empty_fields)

    @staticmethod
    def _calculate_gmv_ratios(
        period: str,
        field_values: dict[str, Any],
        raw_values: dict[str, str],
    ) -> None:
        """用渠道 GMV 金额除以同周期总 GMV，生成直播、视频和商品卡占比。"""
        total_field = f"{period}GMV"
        total_value = field_values.get(total_field, "")
        total_is_number = isinstance(total_value, (int, float)) and not isinstance(total_value, bool)

        for ratio_spec in OVERVIEW_GMV_RATIO_SPECS.get(period, []):
            amount_field = str(ratio_spec["amount_field"])
            ratio_field = str(ratio_spec["ratio_field"])
            amount_value = field_values.get(amount_field, "")
            amount_is_number = isinstance(amount_value, (int, float)) and not isinstance(amount_value, bool)

            # 原始数据中保留明确的计算表达式，排查时能看到分子、分母分别来自哪个 XPath 结果。
            amount_raw = raw_values.get(amount_field, "")
            total_raw = raw_values.get(total_field, "")
            raw_values[ratio_field] = f"渠道GMV={amount_raw!r}; 总GMV={total_raw!r}"

            if amount_is_number and total_is_number and total_value > 0:
                ratio_number = amount_value / total_value * 100
                ratio_value = f"{ratio_number:.2f}%"
                field_values[ratio_field] = ratio_value
                LOGGER.info(
                    "[TikTok][GMV占比计算成功] 时间范围=%s，字段=%s，渠道金额=%r(%s)，总GMV=%r(%s)，计算结果=%r(%s)",
                    period,
                    ratio_field,
                    amount_value,
                    type(amount_value).__name__,
                    total_value,
                    type(total_value).__name__,
                    ratio_value,
                    type(ratio_value).__name__,
                )
            else:
                field_values[ratio_field] = ""
                LOGGER.warning(
                    "[TikTok][GMV占比计算失败] 时间范围=%s，字段=%s，渠道金额=%r，总GMV=%r；金额缺失或总GMV不大于0，返回空值",
                    period,
                    ratio_field,
                    amount_value,
                    total_value,
                )

            # 7天渠道金额保留在内部结果，供机器人消息和调试日志显示；多维表打包时仍过滤未知临时字段。

    @staticmethod
    def _current_url(tab: Any) -> str:
        """读取当前标签页 URL；优先使用 DrissionPage 属性，失败时使用页面脚本兜底。"""
        try:
            value = getattr(tab, "url", "")
            if callable(value):
                value = value()
            value = str(value or "").strip()
            if value:
                return value
        except Exception:
            pass
        try:
            return str(tab.run_js("return location.href;") or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _url_login_state(url: str) -> str | None:
        """把 URL 归类为 login、home 或 unknown；homepage 后可带任意查询参数。"""
        url = str(url or "").strip()
        if not url:
            return None
        try:
            parsed = urlsplit(url)
            host = parsed.netloc.lower()
            path = parsed.path.rstrip("/").lower()
            login_reference = urlsplit(TIKTOK_LOGIN_URL)
            home_reference = urlsplit(TIKTOK_HOME_URL_PREFIX)
            if host == login_reference.netloc.lower() and path == login_reference.path.rstrip("/").lower():
                return "login"
            if host == home_reference.netloc.lower() and path == home_reference.path.rstrip("/").lower():
                return "home"
        except Exception:
            pass
        return None

    @staticmethod
    def _url_matches_target(current_url: str, target_url: str) -> bool:
        """比较目标页面的 host 和 path；忽略 query/hash，允许页面自动附加参数。"""
        try:
            current = urlsplit(str(current_url or "").strip())
            target = urlsplit(str(target_url or "").strip())
            return (
                bool(current.netloc)
                and current.netloc.lower() == target.netloc.lower()
                and current.path.rstrip("/").lower() == target.path.rstrip("/").lower()
            )
        except Exception:
            return False

    def _wait_for_target_url(self, tab: Any, target_url: str, timeout_seconds: float, page_name: str) -> bool:
        """等待 tab.get() 真正进入目标路由，防止首页通用控件造成页面就绪误判。"""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            current_url = self._current_url(tab)
            if self._url_matches_target(current_url, target_url):
                LOGGER.info("[TikTok][目标URL确认] 页面=%s，当前url=%s", page_name, current_url)
                return True
            LOGGER.info("[TikTok][目标URL等待] 页面=%s，当前url=%s，目标=%s", page_name, current_url or "<空>", target_url)
            self._human_wait(tab, 1)
        LOGGER.error(
            "[TikTok][目标URL超时] 页面=%s，%.1f 秒内未进入目标路由，最后url=%s，目标=%s",
            page_name,
            timeout_seconds,
            self._current_url(tab) or "<空>",
            target_url,
        )
        return False

    def _check_login_url(self, tab: Any) -> str | None:
        """按用户要求检查登录页 URL；首页一旦出现就立即确认已登录。"""
        first_url = self._current_url(tab)
        first_state = self._url_login_state(first_url)
        LOGGER.info(
            "[TikTok][URL登录初检] url=%s，状态=%s",
            first_url or "<空>",
            first_state or "unknown",
        )
        if first_state == "home":
            return "home"
        if first_state != "login":
            return None

        # 初始 URL 已经是登录页，因此每一次复查前都先等待 5 秒，
        # 四次检查分别约发生在第 5、10、15、20 秒，符合“连续 4 次、总计 20 秒”的要求。
        saw_unknown_url = False
        for check_index in range(1, LOGIN_URL_CHECK_TIMES + 1):
            LOGGER.info(
                "[TikTok][URL登录复查等待] 第 %s/%s 次检查前等待 %.1f 秒",
                check_index,
                LOGIN_URL_CHECK_TIMES,
                LOGIN_URL_CHECK_INTERVAL_SECONDS,
            )
            self._human_wait(tab, LOGIN_URL_CHECK_INTERVAL_SECONDS)

            current_url = self._current_url(tab)
            current_state = self._url_login_state(current_url)
            LOGGER.info(
                "[TikTok][URL登录复查] 第 %s/%s 次，url=%s，状态=%s",
                check_index,
                LOGIN_URL_CHECK_TIMES,
                current_url or "<空>",
                current_state or "unknown",
            )
            if current_state == "home":
                LOGGER.info("[TikTok][URL登录确认] 检查过程中发现 homepage，立即确认已登录")
                return "home"
            if current_state != "login":
                saw_unknown_url = True
                LOGGER.warning("[TikTok][URL登录状态不明确] 当前 URL 不是 login 或 homepage，本次不确认已登录")

        LOGGER.warning(
            "[TikTok][URL未登录确认] %s，连续检查=%s次，累计观察约 %.1f 秒，按未登录或登录未完成处理",
            "URL 连续保持登录页" if not saw_unknown_url else "观察期间未确认 homepage",
            LOGIN_URL_CHECK_TIMES,
            LOGIN_URL_CHECK_TIMES * LOGIN_URL_CHECK_INTERVAL_SECONDS,
        )
        return "login"

    def _wait_for_homepage_url(
        self,
        tab: Any,
        timeout_seconds: float,
        handle_login_captcha: bool = False,
    ) -> bool:
        """登录提交后等待 URL 进入 homepage；可选处理登录过程中延迟出现的验证码。"""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            current_url = self._current_url(tab)
            current_state = self._url_login_state(current_url)
            LOGGER.info("[TikTok][登录成功URL等待] url=%s，状态=%s", current_url or "<空>", current_state or "unknown")
            if current_state == "home":
                return True
            if handle_login_captcha and self._element_state_matches(tab, CAPTCHA_IMAGE_XPATH, "visible"):
                LOGGER.warning("[TikTok][登录成功URL等待] 发现延迟出现的验证码，开始处理")
                self._solve_login_captcha(tab)
                continue
            self._human_wait(tab, 1)
        LOGGER.error(
            "[TikTok][登录成功URL超时] %.1f 秒内未进入 homepage，最后url=%s",
            timeout_seconds,
            self._current_url(tab) or "<空>",
        )
        return False

    def _is_login_required(self, tab: Any) -> bool:
        """只按登录页/homepage URL 判定登录状态，不再依赖任何业务元素。"""
        url_state = self._check_login_url(tab)
        if url_state in {"login", "home"}:
            LOGGER.info(
                "[TikTok][URL登录结论] 状态=%s，%s",
                url_state,
                "需要登录" if url_state == "login" else "确认已登录",
            )
            return url_state == "login"
        raise RuntimeError(
            f"TikTok 当前 URL 无法确认登录状态，只接受 {TIKTOK_LOGIN_URL} 或 {TIKTOK_HOME_URL_PREFIX}，"
            f"当前URL={self._current_url(tab) or '<空>'}"
        )

    def _perform_login(self, tab: Any) -> None:
        """执行原有登录逻辑；每一步前后发现已登录标志时立即结束登录流程。"""
        LOGGER.warning("[TikTok][登录流程] 开始处理 TikTok 登录，登录完成前暂停其他弹窗检测")
        if self._login_step_already_authenticated(tab, "登录流程开始前"):
            return

        switched_to_email = self._element_state_matches(tab, LOGIN_PHONE_FORMAT_ERROR_XPATH, "visible")
        if switched_to_email:
            LOGGER.warning("[TikTok][登录判断] 已发现“请检查输入的手机号格式”，先切换邮箱再登录")
            self._switch_to_email_login(tab)
            if self._login_step_already_authenticated(tab, "切换邮箱后"):
                return
        else:
            LOGGER.info("[TikTok][登录判断] 当前没有手机号格式错误提示，不切换邮箱，直接点击登录")

        if self._login_step_already_authenticated(tab, "点击登录按钮前"):
            return
        self._click_login_button(tab, "登录-点击登录按钮")
        if self._login_step_already_authenticated(tab, "点击登录按钮后"):
            return

        # 每次提交后同时等待 homepage URL、手机号格式错误和图形验证码。
        login_outcome = self._wait_for_login_outcome(tab, LOGIN_SUBMIT_RESULT_TIMEOUT_SECONDS)
        if login_outcome == "authenticated":
            return
        if login_outcome == "phone_error":
            if switched_to_email:
                raise RuntimeError("TikTok 已切换邮箱登录，但仍显示手机号格式错误")
            LOGGER.warning("[TikTok][登录判断] 直接登录后出现手机号格式错误，开始切换邮箱并重新登录")
            if self._login_step_already_authenticated(tab, "重新切换邮箱前"):
                return
            self._switch_to_email_login(tab)
            switched_to_email = True
            if self._login_step_already_authenticated(tab, "重新切换邮箱后"):
                return
            self._click_login_button(tab, "登录-邮箱模式重新点击登录按钮")
            if self._login_step_already_authenticated(tab, "邮箱模式重新登录后"):
                return
            login_outcome = self._wait_for_login_outcome(tab, LOGIN_SUBMIT_RESULT_TIMEOUT_SECONDS)

        if login_outcome == "captcha":
            self._solve_login_captcha(tab)
        elif login_outcome == "phone_error":
            raise RuntimeError("TikTok 邮箱模式登录后仍显示手机号格式错误")
        elif login_outcome == "authenticated":
            return

        if not self._wait_for_homepage_url(tab, LOGIN_SUCCESS_URL_TIMEOUT_SECONDS, handle_login_captcha=True):
            raise TimeoutError("TikTok 登录后 URL 未进入 homepage")
        LOGGER.info("[TikTok][登录成功] URL 已确认进入 homepage，立即进入固定页面流程")

    def _login_step_already_authenticated(self, tab: Any, step_name: str) -> bool:
        """登录过程每一步调用；URL 仍为登录页时，不能被弹窗或按钮提前放行。"""
        route_state = self._url_login_state(self._current_url(tab))
        if route_state == "home":
            LOGGER.info("[TikTok][登录步骤URL确认] 步骤=%s，当前 URL 已进入 homepage", step_name)
            return True
        if route_state == "login":
            LOGGER.info("[TikTok][登录步骤URL确认] 步骤=%s，当前仍在登录页，不判定登录成功", step_name)
            return False

        LOGGER.info("[TikTok][登录步骤URL确认] 步骤=%s，当前URL不是 homepage，不判定登录成功", step_name)
        return False

    def _switch_to_email_login(self, tab: Any) -> None:
        """点击邮箱切换按钮，并用 selected 标志确认切换完成。"""
        email_mode_ready = self._click_with_retry(
            tab,
            LOGIN_EMAIL_PANEL_BUTTON_XPATH,
            "登录-点击使用邮箱登录",
            success_xpath=LOGIN_EMAIL_SELECTED_XPATH,
            success_state="visible",
            success_name="邮箱登录入口已经切换为 selected 状态",
            stop_when_authenticated=True,
        )
        if not email_mode_ready:
            raise RuntimeError("TikTok 已出现手机号格式错误，但无法切换到邮箱登录状态")

    def _click_login_button(self, tab: Any, step_name: str) -> None:
        """点击登录按钮；失败时直接终止当前店铺，避免错误状态继续执行。"""
        if not self._click_with_retry(
            tab,
            LOGIN_BUTTON_XPATH,
            step_name,
            stop_when_authenticated=True,
        ):
            raise RuntimeError("TikTok 登录按钮点击失败")

    def _wait_for_login_outcome(self, tab: Any, timeout_seconds: float) -> str:
        """登录提交后等待 homepage URL、手机号格式错误或图形验证码。"""
        started_at = time.monotonic()
        deadline = started_at + timeout_seconds
        LOGGER.info("[TikTok][登录结果等待] 最长 %.1f 秒等待 homepage URL、手机号格式错误或图形验证码", timeout_seconds)
        while time.monotonic() < deadline:
            route_state = self._url_login_state(self._current_url(tab))
            if route_state == "home":
                LOGGER.info("[TikTok][登录结果] URL 已进入 homepage，确认登录成功")
                return "authenticated"
            if self._element_state_matches(tab, LOGIN_PHONE_FORMAT_ERROR_XPATH, "visible"):
                LOGGER.warning("[TikTok][登录结果] 已出现“请检查输入的手机号格式”")
                return "phone_error"
            if self._element_state_matches(tab, CAPTCHA_IMAGE_XPATH, "visible"):
                LOGGER.warning("[TikTok][登录结果] 已发现物体匹配验证码")
                return "captcha"
            self._human_wait(tab, 0.5)
        LOGGER.info("[TikTok][登录结果等待] %.1f 秒内没有明确结果，继续等待 homepage URL", time.monotonic() - started_at)
        return "pending"

    def _solve_login_captcha(self, tab: Any) -> None:
        """调用验证码识别接口，并在当前紫鸟标签页完成坐标点击和提交。"""
        captcha_config = self.config.get("captcha", {})
        if not isinstance(captcha_config, dict):
            raise RuntimeError("TikTok captcha 配置必须是字典")
        if not bool(captcha_config.get("enabled", True)):
            raise RuntimeError("TikTok 已出现验证码，但 platforms.tiktok.captcha.enabled 为 false")

        api_key = str(captcha_config.get("api_key") or os.getenv("TIKTOK_CAPTCHA_API_KEY", "")).strip()
        if not api_key:
            raise RuntimeError("TikTok 已出现验证码，但未配置 captcha.api_key 或 TIKTOK_CAPTCHA_API_KEY")

        endpoint = str(
            captcha_config.get("endpoint")
            or "http://220.167.181.200:9009/openapi/verify_code_identify/"
        ).strip()
        verify_idf_id = str(captcha_config.get("verify_idf_id") or "38").strip()
        words = str(captcha_config.get("words") or CAPTCHA_WORDS).strip()
        request_timeout = float(captcha_config.get("request_timeout_seconds", 60) or 60)
        max_attempts = CAPTCHA_SOLVE_RETRY_TIMES + 1
        LOGGER.warning(
            "[TikTok][验证码处理] 开始识别物体匹配验证码，识别类型=%s，最多尝试=%s次",
            verify_idf_id,
            max_attempts,
        )

        for attempt in range(max_attempts):
            if self._login_step_already_authenticated(tab, f"验证码处理第{attempt + 1}轮开始前"):
                return
            captcha_img = self._find_visible_element(tab, CAPTCHA_IMAGE_XPATH, timeout=3)
            if not captcha_img:
                LOGGER.info("[TikTok][验证码处理] 第 %s/%s 次处理前验证码已经消失", attempt + 1, max_attempts)
                return

            image_src = str(captcha_img.attr("src") or "").strip()
            try:
                image_bytes = self._read_captcha_image_bytes(captcha_img, image_src, request_timeout)
                try:
                    image_base64, image_width, image_height = self._image_bytes_to_data_uri(image_bytes, image_src)
                except RuntimeError:
                    raise
                except Exception as image_exc:
                    # CDN 地址可能返回登录页或拦截页而不是图片，此时直接截取页面中的验证码元素。
                    LOGGER.warning("[TikTok][验证码图片解析失败] 回退为元素截图，异常=%s", image_exc)
                    screenshot = captcha_img.get_screenshot(as_bytes="png", scroll_to_center=True)
                    if not screenshot:
                        raise RuntimeError("TikTok 验证码元素截图失败") from image_exc
                    image_base64, image_width, image_height = self._image_bytes_to_data_uri(bytes(screenshot), "")
                point1, point2, raw_reply = self._recognise_captcha_coordinates(
                    api_key,
                    endpoint,
                    verify_idf_id,
                    words,
                    request_timeout,
                    image_base64,
                    image_width,
                    image_height,
                )
                LOGGER.info(
                    "[TikTok][验证码识别成功] 第 %s/%s 次，原图尺寸=%sx%s，"
                    "接口原始 res_str=%r，直接使用坐标 p1=%s，p2=%s",
                    attempt + 1,
                    max_attempts,
                    image_width,
                    image_height,
                    raw_reply,
                    point1,
                    point2,
                )

                # 模型请求期间验证码可能自动刷新；刷新后旧图片坐标不得继续使用。
                current_img = self._find_visible_element(tab, CAPTCHA_IMAGE_XPATH, timeout=2)
                if not current_img:
                    LOGGER.info("[TikTok][验证码处理] 模型返回前验证码已经消失，无需继续点击")
                    return
                current_src = str(current_img.attr("src") or "").strip()
                if image_src and current_src and current_src != image_src:
                    LOGGER.warning("[TikTok][验证码图片已更新] 本次识别结果作废，重新识别新图片")
                    continue

                click_points = self._convert_captcha_points(
                    current_img,
                    point1,
                    point2,
                    image_width,
                    image_height,
                )
                self._click_captcha_points(tab, click_points)
                self._click_captcha_confirm(tab)
                if self._wait_for_captcha_submission(tab, image_src, CAPTCHA_SUBMIT_RESULT_TIMEOUT_SECONDS):
                    LOGGER.info("[TikTok][验证码处理成功] 验证码已通过或已关闭")
                    return
                LOGGER.warning("[TikTok][验证码处理重试] 提交后验证码仍存在或已刷新，准备重新识别")
            except Exception as exc:
                LOGGER.exception(
                    "[TikTok][验证码处理失败] 第 %s/%s 次异常=%s",
                    attempt + 1,
                    max_attempts,
                    exc,
                )

            if attempt < CAPTCHA_SOLVE_RETRY_TIMES:
                self._human_wait(tab, CLICK_RETRY_INTERVAL_SECONDS)

        raise RuntimeError(f"TikTok 物体匹配验证码连续 {max_attempts} 次处理失败")

    @classmethod
    def _recognise_captcha_coordinates(
        cls,
        api_key: str,
        endpoint: str,
        verify_idf_id: str,
        words: str,
        timeout_seconds: float,
        image_base64: str,
        image_width: int,
        image_height: int,
    ) -> tuple[tuple[float, float], tuple[float, float], str]:
        """请求一次并直接返回接口给出的两个坐标，不做重复采样、平均或四舍五入。"""
        point1, point2, raw_reply = cls._call_captcha_identify_api(
            api_key,
            endpoint,
            verify_idf_id,
            words,
            timeout_seconds,
            image_base64,
            image_width,
            image_height,
        )
        LOGGER.info(
            "[TikTok][验证码坐标读取] res_str=%r，坐标1=%s，坐标2=%s，坐标未做平均或四舍五入",
            raw_reply,
            point1,
            point2,
        )
        return point1, point2, raw_reply

    @staticmethod
    def _read_captcha_image_bytes(captcha_img: Any, image_src: str, timeout_seconds: float) -> bytes:
        """把验证码图片读入内存；远程地址失败时回退为元素截图，不向磁盘写文件。"""
        try:
            if image_src.startswith("data:") and "," in image_src:
                encoded = image_src.split(",", 1)[1]
                image_bytes = base64.b64decode(encoded)
                if image_bytes:
                    return image_bytes
            if image_src.startswith("//"):
                image_src = f"https:{image_src}"
            if image_src.startswith(("http://", "https://")):
                response = requests.get(image_src, timeout=timeout_seconds)
                response.raise_for_status()
                if response.content:
                    return response.content
        except Exception as exc:
            LOGGER.warning("[TikTok][验证码图片下载失败] 将回退为元素截图，异常=%s", exc)

        screenshot = captcha_img.get_screenshot(as_bytes="png", scroll_to_center=True)
        if not screenshot:
            raise RuntimeError("无法下载或截取 TikTok 验证码图片")
        return bytes(screenshot)

    @staticmethod
    def _image_bytes_to_data_uri(image_bytes: bytes, image_src: str = "") -> tuple[str, int, int]:
        """保留原始图片格式生成 Data URI，并返回接口坐标对应的原图尺寸。"""
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("缺少 Pillow，请先执行 pip install -r requirements.txt") from exc

        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
            width, height = image.size
            image_format = str(image.format or "PNG").lower()

        # 优先使用图片实际格式；如果 Pillow 没有识别，再尝试从 src 后缀读取。
        format_to_mime = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
            "gif": "image/gif",
            "bmp": "image/bmp",
        }
        mime_type = format_to_mime.get(image_format)
        if not mime_type and image_src:
            mime_type = mimetypes.guess_type(urlsplit(image_src).path)[0]
        if not mime_type or not mime_type.startswith("image/"):
            mime_type = "image/png"

        encoded = base64.b64encode(image_bytes).decode("ascii")
        return f"data:{mime_type};base64,{encoded}", int(width), int(height)

    @staticmethod
    def _call_captcha_identify_api(
        api_key: str,
        endpoint: str,
        verify_idf_id: str,
        words: str,
        timeout_seconds: float,
        image_base64: str,
        image_width: int,
        image_height: int,
    ) -> tuple[tuple[float, float], tuple[float, float], str]:
        """调用验证码识别接口，并从 res_str 严格解析、校验两个图片像素坐标。"""
        headers = {"Content-Type": "application/json"}
        payload = {
            "key": api_key,
            "verify_idf_id": verify_idf_id,
            "img_base64": image_base64,
            "words": words,
        }
        LOGGER.info(
            "[TikTok][验证码接口请求] endpoint=%s，verify_idf_id=%s，words=%r，图片格式=%s，图片尺寸=%sx%s",
            endpoint,
            verify_idf_id,
            words,
            image_base64.split(";", 1)[0].replace("data:", "") if image_base64.startswith("data:") else "unknown",
            image_width,
            image_height,
        )
        response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout_seconds)
        response.raise_for_status()
        try:
            response_json = response.json()
        except ValueError as exc:
            raise ValueError(f"验证码接口返回的不是 JSON: {response.text!r}") from exc
        if not isinstance(response_json, dict):
            raise ValueError(f"验证码接口响应结构异常: {response_json!r}")
        if response_json.get("code") != 200:
            raise ValueError(
                f"验证码接口识别失败 code={response_json.get('code')!r} "
                f"msg={response_json.get('msg')!r}"
            )

        response_data = response_json.get("data")
        if not isinstance(response_data, dict):
            raise ValueError(f"验证码接口响应缺少 data 字典: {response_json!r}")
        raw_reply = str(response_data.get("res_str") or "").strip()
        if not raw_reply:
            raise ValueError(f"验证码接口响应缺少 data.res_str: {response_json!r}")
        try:
            coordinate_data = ast.literal_eval(raw_reply)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"验证码接口 res_str 坐标格式错误: {raw_reply!r}") from exc
        if not isinstance(coordinate_data, (list, tuple)) or len(coordinate_data) != 2:
            raise ValueError(f"验证码接口必须返回两个坐标: {raw_reply!r}")

        points: list[tuple[float, float]] = []
        for point_index, point in enumerate(coordinate_data, start=1):
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError(f"验证码第 {point_index} 个坐标格式错误: {point!r}")
            try:
                x, y = float(point[0]), float(point[1])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"验证码第 {point_index} 个坐标不是数字: {point!r}") from exc
            if not (0 <= x <= image_width and 0 <= y <= image_height):
                raise ValueError(
                    f"验证码第 {point_index} 个坐标=({x}, {y}) 超出图片范围 {image_width}x{image_height}"
                )
            points.append((x, y))
        return points[0], points[1], str(raw_reply)

    @staticmethod
    def _convert_captcha_points(
        captcha_img: Any,
        point1: tuple[float, float],
        point2: tuple[float, float],
        image_width: int,
        image_height: int,
    ) -> list[tuple[float, float]]:
        """把模型给出的原图像素坐标换算为当前浏览器视口中的 CSS 像素坐标。"""
        try:
            captcha_img.run_js("this.scrollIntoView({behavior:'smooth', block:'center', inline:'center'});")
            time.sleep(0.5)
        except Exception:
            pass
        left, top = captcha_img.rect.viewport_location
        rendered_width, rendered_height = captcha_img.rect.size
        if image_width <= 0 or image_height <= 0 or rendered_width <= 0 or rendered_height <= 0:
            raise ValueError("验证码原图或页面渲染尺寸无效")

        converted: list[tuple[float, float]] = []
        for x, y in (point1, point2):
            converted.append(
                (
                    float(left) + x * float(rendered_width) / image_width,
                    float(top) + y * float(rendered_height) / image_height,
                )
            )
        LOGGER.info(
            "[TikTok][验证码坐标换算] 页面位置=(%.1f, %.1f)，渲染尺寸=%.1fx%.1f，点击坐标=%s",
            left,
            top,
            rendered_width,
            rendered_height,
            converted,
        )
        return converted

    def _click_captcha_points(self, tab: Any, points: list[tuple[float, float]]) -> None:
        """通过公共真人交互层在当前标签页依次点击验证码坐标。"""
        for index, (target_x, target_y) in enumerate(points, start=1):
            self._human_interaction.click_point(tab, (target_x, target_y))
            LOGGER.info("[TikTok][验证码坐标点击] 已点击第%s个物体，坐标=(%.1f, %.1f)", index, target_x, target_y)
            time.sleep(random.uniform(0.35, 0.65) if self._human_interaction.enabled else 0.01)

    @staticmethod
    def _bezier_mouse_path(
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        steps: int = 25,
    ) -> list[tuple[float, float]]:
        """生成三阶贝塞尔鼠标轨迹，减少两次坐标点击之间的机械直跳。"""
        control1_x = start_x + (end_x - start_x) * random.uniform(0.2, 0.6)
        control1_y = start_y + (end_y - start_y) * random.uniform(0.1, 0.7)
        control2_x = start_x + (end_x - start_x) * random.uniform(0.4, 0.9)
        control2_y = start_y + (end_y - start_y) * random.uniform(0.3, 0.8)
        path: list[tuple[float, float]] = []
        for index in range(steps + 1):
            progress = index / steps
            remaining = 1 - progress
            x = (
                remaining**3 * start_x
                + 3 * remaining**2 * progress * control1_x
                + 3 * remaining * progress**2 * control2_x
                + progress**3 * end_x
            )
            y = (
                remaining**3 * start_y
                + 3 * remaining**2 * progress * control1_y
                + 3 * remaining * progress**2 * control2_y
                + progress**3 * end_y
            )
            path.append((x, y))
        return path

    def _click_captcha_confirm(self, tab: Any) -> None:
        """点击验证码确认按钮；确认按钮不存在时立即报错，避免错误坐标被当作已提交。"""
        confirm_button = self._find_visible_element(tab, CAPTCHA_CONFIRM_BUTTON_XPATH, timeout=TK_STEP_WAIT_SECONDS)
        if not confirm_button:
            raise RuntimeError("TikTok 验证码确认按钮不可见")
        self._prepare_human_click(tab, confirm_button, "验证码-点击确认按钮")
        try:
            self._human_interaction.click_element(tab, confirm_button)
        except Exception:
            confirm_button.click()
        self._record_button_click()
        LOGGER.info("[TikTok][验证码提交] 已点击确认按钮，开始等待验证结果")

    def _wait_for_captcha_submission(self, tab: Any, previous_src: str, timeout_seconds: float) -> bool:
        """验证码提交后等待 homepage；图片更新表示识别失败，需要处理新验证码。"""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            route_state = self._url_login_state(self._current_url(tab))
            if route_state == "home":
                LOGGER.info("[TikTok][验证码结果] URL 已进入 homepage，验证码流程通过")
                return True
            captcha_img = self._find_visible_element(tab, CAPTCHA_IMAGE_XPATH, timeout=0.5)
            if captcha_img:
                current_src = str(captcha_img.attr("src") or "").strip()
                if previous_src and current_src and current_src != previous_src:
                    LOGGER.warning("[TikTok][验证码结果] 验证码图片已经更新，需要重新识别")
                    return False
            self._human_wait(tab, 0.5)

        captcha_still_visible = self._element_state_matches(tab, CAPTCHA_IMAGE_XPATH, "visible")
        if not captcha_still_visible:
            LOGGER.info("[TikTok][验证码结果] 验证码图片已经消失，继续等待 homepage URL")
            return True
        LOGGER.warning("[TikTok][验证码结果] 等待 %.1f 秒后原验证码仍可见", timeout_seconds)
        return False

    def _open_business_page(self, tab: Any, url: str, ready_xpath: str, page_name: str) -> bool:
        """在紫鸟当前标签页打开固定业务 URL，并等待该页面的时间控件出现。"""
        if not url:
            LOGGER.error("[TikTok][页面跳转失败] 页面=%s，URL 为空", page_name)
            return False
        try:
            LOGGER.info("[TikTok][页面跳转] 页面=%s，开始打开 URL=%s", page_name, url)
            tab.get(url)
            LOGGER.info(
                "[TikTok][页面跳转完成] 页面=%s，已调用 tab.get，先等待目标 URL，再等待时间面板按钮，xpath=%s",
                page_name,
                ready_xpath,
            )
        except Exception as exc:
            LOGGER.exception("[TikTok][页面跳转失败] 页面=%s，URL=%s，异常=%s", page_name, url, exc)
            return False

        if not self._wait_for_target_url(tab, url, BUSINESS_PAGE_URL_TIMEOUT_SECONDS, page_name):
            LOGGER.error("[TikTok][页面跳转失败] 页面=%s，当前页面没有进入目标 URL，不读取首页控件", page_name)
            return False

        ready = self._wait_for_xpath(
            tab,
            ready_xpath,
            BUSINESS_PAGE_ELEMENT_TIMEOUT_SECONDS,
            f"{page_name}时间面板按钮",
        )
        if ready:
            LOGGER.info("[TikTok][页面就绪] 页面=%s，时间面板按钮已出现，xpath=%s", page_name, ready_xpath)
        else:
            LOGGER.error(
                "[TikTok][页面就绪失败] 页面=%s，等待 %.1f 秒仍未看到时间面板按钮，xpath=%s",
                page_name,
                BUSINESS_PAGE_ELEMENT_TIMEOUT_SECONDS,
                ready_xpath,
            )
        return ready

    def _prepare_human_click(self, tab: Any, element: Any, step_name: str) -> None:
        """在真实点击前补足随机间隔，并执行居中滚动、鼠标移动和短暂停留。"""
        if not self._human_interaction.enabled:
            return
        target_interval = random.uniform(HUMAN_CLICK_INTERVAL_MIN_SECONDS, HUMAN_CLICK_INTERVAL_MAX_SECONDS)
        if self._last_button_click_at is None:
            # 第一个按钮前也稍作停留，避免页面刚加载完就立即发生机械点击。
            first_pause = random.uniform(HUMAN_PRE_CLICK_PAUSE_MIN_SECONDS, HUMAN_PRE_CLICK_PAUSE_MAX_SECONDS)
            LOGGER.info(
                "[TikTok][仿人点击准备] 步骤=%s，当前是本店铺首次点击，先观察页面 %.2f 秒",
                step_name,
                first_pause,
            )
            self._human_wait(tab, first_pause)
        else:
            elapsed = time.monotonic() - self._last_button_click_at
            remaining = max(0.0, target_interval - elapsed)
            if remaining > 0:
                LOGGER.info(
                    "[TikTok][仿人点击间隔] 步骤=%s，距上次点击 %.2f 秒，随机目标 %.2f 秒，继续等待 %.2f 秒",
                    step_name,
                    elapsed,
                    target_interval,
                    remaining,
                )
                self._human_wait(tab, remaining)
            else:
                LOGGER.info(
                    "[TikTok][仿人点击间隔] 步骤=%s，距上次点击 %.2f 秒，已达到随机目标 %.2f 秒",
                    step_name,
                    elapsed,
                    target_interval,
                )

        scrolled = False
        try:
            scroll = getattr(element, "scroll", None)
            to_see = getattr(scroll, "to_see", None) if scroll is not None else None
            if callable(to_see):
                to_see(center=True)
                scrolled = True
        except Exception as exc:
            LOGGER.debug("[TikTok][元素定位] 步骤=%s，DrissionPage 居中定位失败=%s", step_name, exc)
        if not scrolled:
            try:
                element.run_js("this.scrollIntoView({behavior:'smooth', block:'center', inline:'center'});")
                scrolled = True
            except Exception as exc:
                LOGGER.debug("[TikTok][元素定位] 步骤=%s，JavaScript 居中定位失败=%s", step_name, exc)
        LOGGER.info("[TikTok][元素定位] 步骤=%s，目标进入视口结果=%s", step_name, "成功" if scrolled else "跳过")

        scroll_pause = random.uniform(HUMAN_PRE_CLICK_PAUSE_MIN_SECONDS, HUMAN_PRE_CLICK_PAUSE_MAX_SECONDS)
        self._human_wait(tab, scroll_pause)
        moved_to_target = self._human_mouse_move_to_element(tab, element)
        hover_pause = random.uniform(0.3, 0.8)
        LOGGER.info(
            "[TikTok][仿人鼠标] 步骤=%s，移动到目标结果=%s，点击前停留 %.2f 秒",
            step_name,
            "成功" if moved_to_target else "使用随机移动代替",
            hover_pause,
        )
        # 此处直接停留，不再调用会随机移动鼠标的 _human_wait，确保光标停在目标附近再点击。
        time.sleep(hover_pause)

    def _human_mouse_move_to_element(self, tab: Any, element: Any) -> bool:
        """把鼠标平滑移动到目标元素；失败时保留随机坐标作为后备。"""
        try:
            return self._human_interaction.move_to_element(tab, element)
        except Exception:
            self._human_interaction.move_to_fallback(tab)
            return False

    def _record_button_click(self) -> None:
        """记录真实点击完成时间，供下一个按钮计算最小随机间隔。"""
        self._last_button_click_at = time.monotonic()

    def _run_period_click_steps(self, tab: Any, steps: list[dict[str, Any]]) -> bool:
        """执行“打开日期面板 -> 选择日期”；日期选项消失时自动重新打开面板。"""
        if len(steps) < 2:
            LOGGER.error("[TikTok][日期流程配置错误] 至少需要时间面板和日期选项两个步骤，当前步骤数=%s", len(steps))
            return False

        open_step = steps[0]
        option_step = steps[1]
        open_name = str(open_step.get("name") or "打开日期面板")
        open_xpath = str(open_step.get("xpath") or "").strip()
        option_name = str(option_step.get("name") or "选择日期")
        option_xpath = str(option_step.get("xpath") or "").strip()
        if not open_xpath or not option_xpath:
            LOGGER.error(
                "[TikTok][日期流程配置错误] 步骤=%s/%s，时间按钮或日期选项 XPath 为空",
                open_name,
                option_name,
            )
            return False

        LOGGER.info("[TikTok][日期流程] 先执行上一步=%s，确认日期选项出现后再执行=%s", open_name, option_name)
        panel_opened = self._click_with_retry(
            tab,
            open_xpath,
            open_name,
            success_xpath=option_xpath,
            success_state="visible",
            success_name=str(open_step.get("success_name") or "日期选项已经出现"),
            success_timeout_seconds=TK_STEP_WAIT_SECONDS,
        )
        if not panel_opened:
            # 不在这里终止：下一步会再次检查日期选项；不可见时由 recovery_xpath 重做本步骤。
            LOGGER.warning("[TikTok][日期流程恢复] 上一步=%s 未确认成功，下一步将按页面实际可见性决定是否重开面板", open_name)

        option_clicked = self._click_with_retry(
            tab,
            option_xpath,
            option_name,
            success_xpath=str(option_step.get("success_xpath") or option_xpath).strip(),
            success_state=str(option_step.get("success_state") or "hidden").strip().lower(),
            success_name=str(option_step.get("success_name") or "日期选项已经消失"),
            recovery_xpath=open_xpath,
            recovery_step_name=f"{open_name}（日期选项不可见，重新执行上一步）",
            recovery_success_name=str(open_step.get("success_name") or "日期选项已经重新出现"),
            success_timeout_seconds=TK_STEP_WAIT_SECONDS,
        )
        if not option_clicked:
            LOGGER.error("[TikTok][日期流程失败] 日期选项=%s 点击及恢复均失败", option_name)
            return False

        LOGGER.info(
            "[TikTok][日期流程成功] 日期选项=%s 点击后已在 %.1f 秒内消失，确认日期切换成功",
            option_name,
            TK_STEP_WAIT_SECONDS,
        )
        return True

    def _click_with_retry(
        self,
        tab: Any,
        xpath: str,
        step_name: str = "未命名按钮",
        success_xpath: str = "",
        success_state: str = "",
        success_name: str = "点击后的页面状态",
        recovery_xpath: str = "",
        recovery_step_name: str = "重新执行上一步",
        recovery_success_name: str = "目标按钮已经重新出现",
        success_timeout_seconds: float = TK_STEP_WAIT_SECONDS,
        stop_when_authenticated: bool = False,
    ) -> bool:
        """按钮最多点击4次，并在统一超时内验证状态；登录步骤可被已登录标志提前终止。"""
        max_attempts = CLICK_RETRY_TIMES + 1
        click_dispatched = False
        for attempt in range(max_attempts):
            if stop_when_authenticated and self._login_step_already_authenticated(tab, f"{step_name} 第{attempt + 1}次点击前"):
                return True
            # 对“展开菜单、打开面板”类步骤，目标元素已经可见就说明当前状态正确，不应再次点击把它关闭。
            if success_xpath and success_state == "visible" and self._element_state_matches(tab, success_xpath, "visible"):
                LOGGER.info("[TikTok][按钮验证成功] 步骤=%s，%s，无需再次点击", step_name, success_name)
                return True
            # 只有确实发出过点击后，才允许用“选项已经隐藏”确认延迟生效，防止首次检查时误判成功。
            if click_dispatched and success_xpath and success_state == "hidden" and self._element_state_matches(tab, success_xpath, "hidden"):
                LOGGER.info("[TikTok][按钮验证成功] 步骤=%s，%s，上一次点击已经延迟生效", step_name, success_name)
                return True

            if attempt > 0:
                # 在固定 2 秒基础上增加少量随机等待，避免点击节奏完全一致。
                interval = CLICK_RETRY_INTERVAL_SECONDS + random.uniform(0, 1)
                LOGGER.warning(
                    "[TikTok][按钮重试] 步骤=%s，第 %s/%s 次尝试前等待 %.2f 秒，xpath=%s",
                    step_name,
                    attempt + 1,
                    max_attempts,
                    interval,
                    xpath,
                )
                self._human_wait(tab, interval)
            try:
                LOGGER.info(
                    "[TikTok][按钮查找] 步骤=%s，第 %s/%s 次尝试，xpath=%s",
                    step_name,
                    attempt + 1,
                    max_attempts,
                    xpath,
                )
                element = self._find_visible_element(tab, xpath, timeout=TK_STEP_WAIT_SECONDS)
                if not element and recovery_xpath:
                    LOGGER.warning(
                        "[TikTok][按钮恢复上一步] 步骤=%s，第 %s/%s 次尝试发现目标不可见；不直接重试目标，先执行=%s，xpath=%s",
                        step_name,
                        attempt + 1,
                        max_attempts,
                        recovery_step_name,
                        recovery_xpath,
                    )
                    recovered = self._click_with_retry(
                        tab,
                        recovery_xpath,
                        recovery_step_name,
                        success_xpath=xpath,
                        success_state="visible",
                        success_name=recovery_success_name,
                        success_timeout_seconds=success_timeout_seconds,
                    )
                    if recovered:
                        element = self._find_visible_element(tab, xpath, timeout=TK_STEP_WAIT_SECONDS)
                        LOGGER.info(
                            "[TikTok][按钮恢复结果] 步骤=%s，重新执行上一步后目标可见=%s",
                            step_name,
                            "是" if element else "否",
                        )
                if not element:
                    LOGGER.warning(
                        "[TikTok][按钮未找到] 步骤=%s，第 %s/%s 次尝试未找到可见元素",
                        step_name,
                        attempt + 1,
                        max_attempts,
                    )
                    continue
                self._prepare_human_click(tab, element, step_name)
                try:
                    self._human_interaction.click_element(tab, element)
                except Exception:
                    element.click()
                click_dispatched = True
                self._record_button_click()
                if stop_when_authenticated and self._login_step_already_authenticated(tab, f"{step_name} 第{attempt + 1}次点击后"):
                    return True
                if success_xpath and success_state:
                    LOGGER.info("[TikTok][按钮已点击] 步骤=%s，开始验证=%s", step_name, success_name)
                    verified = self._wait_for_element_state(
                        tab,
                        success_xpath,
                        success_state,
                        success_timeout_seconds,
                        success_name,
                    )
                    if not verified:
                        LOGGER.warning(
                            "[TikTok][按钮验证失败] 步骤=%s，第 %s/%s 次点击后未满足=%s，准备重试",
                            step_name,
                            attempt + 1,
                            max_attempts,
                            success_name,
                        )
                        continue
                LOGGER.info(
                    "[TikTok][按钮成功] 步骤=%s，第 %s/%s 次点击并验证成功，xpath=%s",
                    step_name,
                    attempt + 1,
                    max_attempts,
                    xpath,
                )
                return True
            except Exception as exc:
                LOGGER.warning(
                    "[TikTok][按钮异常] 步骤=%s，第 %s/%s 次尝试失败，异常=%s，xpath=%s",
                    step_name,
                    attempt + 1,
                    max_attempts,
                    exc,
                    xpath,
                )
                continue
        LOGGER.error("[TikTok][按钮终止] 步骤=%s，全部 %s 次点击均失败，xpath=%s", step_name, max_attempts, xpath)
        return False

    def _wait_for_element_state(
        self,
        tab: Any,
        xpath: str,
        expected_state: str,
        timeout_seconds: float,
        target_name: str,
    ) -> bool:
        """等待按钮或面板出现/消失；消失连续确认两次，避免瞬时查询失败造成误判。"""
        if expected_state not in {"visible", "hidden"}:
            LOGGER.error("[TikTok][状态配置错误] 目标=%s，不支持 expected_state=%s", target_name, expected_state)
            return False

        started_at = time.monotonic()
        deadline = started_at + timeout_seconds
        hidden_checks = 0
        LOGGER.info(
            "[TikTok][状态等待] 目标=%s，期望=%s，最长 %.1f 秒，xpath=%s",
            target_name,
            expected_state,
            timeout_seconds,
            xpath,
        )
        while time.monotonic() < deadline:
            visible = bool(self._find_visible_element(tab, xpath, timeout=1))
            if expected_state == "visible" and visible:
                LOGGER.info("[TikTok][状态满足] 目标=%s 已出现，耗时 %.2f 秒", target_name, time.monotonic() - started_at)
                return True
            if expected_state == "hidden":
                if visible:
                    hidden_checks = 0
                else:
                    hidden_checks += 1
                    if hidden_checks >= 2:
                        LOGGER.info("[TikTok][状态满足] 目标=%s 连续两次不可见，确认已经消失", target_name)
                        return True
            self._human_wait(tab, 0.5)

        LOGGER.error(
            "[TikTok][状态等待超时] 目标=%s，等待 %.1f 秒仍未达到=%s，xpath=%s",
            target_name,
            timeout_seconds,
            expected_state,
            xpath,
        )
        return False

    def _element_state_matches(self, tab: Any, xpath: str, expected_state: str) -> bool:
        """立即检查元素当前是否符合 visible 或 hidden 状态。"""
        visible = bool(self._find_visible_element(tab, xpath, timeout=0.5))
        if expected_state == "visible":
            return visible
        if expected_state == "hidden":
            return not visible
        return False

    @staticmethod
    def _find_visible_element(tab: Any, xpath: str, timeout: float = 1) -> Any:
        """查找真正可见的元素，并兼容 DrissionPage 不同版本的可见性属性。"""
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
                legacy_displayed = legacy_displayed()
            if legacy_displayed is not None:
                return element if bool(legacy_displayed) else None
            return element
        except Exception:
            return None

    def _wait_for_xpath(self, tab: Any, xpath: str, timeout_seconds: float, target_name: str = "下一元素") -> bool:
        """成功点击后轮询下一元素，最长等待 30 秒。"""
        if not xpath:
            LOGGER.warning("[TikTok][元素等待跳过] 目标=%s，原因=XPath 为空", target_name)
            return True
        started_at = time.monotonic()
        deadline = time.monotonic() + timeout_seconds
        check_count = 0
        LOGGER.info("[TikTok][元素等待] 目标=%s，最长等待 %.1f 秒，xpath=%s", target_name, timeout_seconds, xpath)
        while time.monotonic() < deadline:
            check_count += 1
            try:
                if self._find_visible_element(tab, xpath, timeout=1):
                    LOGGER.info(
                        "[TikTok][元素出现] 目标=%s，第 %s 次检查找到元素，耗时 %.2f 秒，xpath=%s",
                        target_name,
                        check_count,
                        time.monotonic() - started_at,
                        xpath,
                    )
                    return True
            except Exception as exc:
                LOGGER.warning("[TikTok][元素检查异常] 目标=%s，第 %s 次检查异常=%s", target_name, check_count, exc)
            self._human_wait(tab, 1)
        LOGGER.error(
            "[TikTok][元素等待超时] 目标=%s，等待 %.1f 秒仍未出现，xpath=%s",
            target_name,
            timeout_seconds,
            xpath,
        )
        return False

    def _wait_for_data_ready_marker(self, tab: Any, xpath: str, marker_name: str) -> bool:
        """检查数据加载完成标志；首次未找到后每 2 秒重查，最多重查 5 次。"""
        xpath = str(xpath or "").strip()
        if not xpath:
            LOGGER.warning("[TikTok][数据标志跳过] 目标=%s，XPath 尚未填写，暂不阻止后续批量读取", marker_name)
            return True

        total_checks = DATA_READY_RETRY_TIMES + 1
        started_at = time.monotonic()
        LOGGER.info(
            "[TikTok][数据标志等待] 目标=%s，首次检查失败后每 %.1f 秒重查，最多重查 %s 次，xpath=%s",
            marker_name,
            DATA_READY_RETRY_INTERVAL_SECONDS,
            DATA_READY_RETRY_TIMES,
            xpath,
        )
        for check_index in range(total_checks):
            try:
                marker = self._find_visible_element(tab, xpath, timeout=0.2)
            except Exception as exc:
                marker = None
                LOGGER.warning(
                    "[TikTok][数据标志异常] 目标=%s，第 %s/%s 次检查异常=%s",
                    marker_name,
                    check_index + 1,
                    total_checks,
                    exc,
                )

            if marker:
                LOGGER.info(
                    "[TikTok][数据标志出现] 目标=%s，第 %s/%s 次检查找到标志，耗时=%.2f秒",
                    marker_name,
                    check_index + 1,
                    total_checks,
                    time.monotonic() - started_at,
                )
                return True

            if check_index < DATA_READY_RETRY_TIMES:
                LOGGER.warning(
                    "[TikTok][数据标志未出现] 目标=%s，第 %s/%s 次检查未找到，等待 %.1f 秒后重查",
                    marker_name,
                    check_index + 1,
                    total_checks,
                    DATA_READY_RETRY_INTERVAL_SECONDS,
                )
                self._human_wait(tab, DATA_READY_RETRY_INTERVAL_SECONDS)

        LOGGER.error(
            "[TikTok][数据标志超时] 目标=%s，首次检查及 %s 次重查均未找到，累计等待约 %.1f 秒，xpath=%s",
            marker_name,
            DATA_READY_RETRY_TIMES,
            DATA_READY_RETRY_TIMES * DATA_READY_RETRY_INTERVAL_SECONDS,
            xpath,
        )
        return False

    def _wait_for_required_metric_count(
        self,
        tab: Any,
        period: str,
        specs: list[dict[str, str]],
        page_name: str,
    ) -> bool:
        """等待当前周期所有已配置指标出现非空文本，再额外缓冲三秒。"""
        configured_specs = [
            spec for spec in specs if str(spec.get("xpath") or "").strip()
        ]
        # 同一个 XPath 只代表一个页面节点，按唯一 XPath 计数避免重复配置造成误判。
        unique_xpaths = list(dict.fromkeys(str(spec["xpath"]) for spec in configured_specs))
        configured_specs = [{"xpath": xpath} for xpath in unique_xpaths]
        required_count = len(configured_specs)
        if required_count == 0:
            LOGGER.warning("[TikTok][指标数量等待跳过] 页面=%s，时间范围=%s，没有有效 XPath", page_name, period)
            return True

        LOGGER.info(
            "[TikTok][指标数量等待] 页面=%s，时间范围=%s，要求=%s个非空指标，间隔=%.1f秒，最多检查=%s次",
            page_name,
            period,
            required_count,
            METRIC_COUNT_READY_INTERVAL_SECONDS,
            METRIC_COUNT_READY_CHECK_TIMES,
        )
        started_at = time.monotonic()
        for check_index in range(1, METRIC_COUNT_READY_CHECK_TIMES + 1):
            loaded_count = self._count_non_empty_metrics(tab, configured_specs)
            LOGGER.info(
                "[TikTok][指标数量检查] 页面=%s，时间范围=%s，第%s/%s次，已加载=%s/%s",
                page_name,
                period,
                check_index,
                METRIC_COUNT_READY_CHECK_TIMES,
                loaded_count,
                required_count,
            )
            if loaded_count >= required_count:
                LOGGER.info(
                    "[TikTok][指标数量满足] 页面=%s，时间范围=%s，已加载=%s/%s，额外等待%.1f秒",
                    page_name,
                    period,
                    loaded_count,
                    required_count,
                    METRIC_COUNT_READY_EXTRA_WAIT_SECONDS,
                )
                time.sleep(METRIC_COUNT_READY_EXTRA_WAIT_SECONDS)
                return True
            if check_index < METRIC_COUNT_READY_CHECK_TIMES:
                time.sleep(METRIC_COUNT_READY_INTERVAL_SECONDS)

        LOGGER.error(
            "[TikTok][指标数量等待超时] 页面=%s，时间范围=%s，60次检查后仍只有%s/%s，耗时%.1f秒",
            page_name,
            period,
            self._count_non_empty_metrics(tab, configured_specs),
            required_count,
            time.monotonic() - started_at,
        )
        return False

    @staticmethod
    def _count_non_empty_metrics(tab: Any, specs: list[dict[str, str]]) -> int:
        """在页面内按每个配置 XPath 统计非空指标数量，不输出具体数据。"""
        specs_json = json.dumps(
            [{"xpath": str(spec.get("xpath") or "")} for spec in specs],
            ensure_ascii=False,
        )
        script = f"""
            const metricSpecs = {specs_json};
            let loadedCount = 0;
            for (const metric of metricSpecs) {{
                try {{
                    const node = document.evaluate(
                        metric.xpath,
                        document,
                        null,
                        XPathResult.FIRST_ORDERED_NODE_TYPE,
                        null
                    ).singleNodeValue;
                    if (!node) continue;
                    const isInput = node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement;
                    const text = String(isInput ? node.value : (node.innerText ?? node.textContent ?? '')).trim();
                    if (text) loadedCount += 1;
                }} catch (error) {{}}
            }}
            return loadedCount;
        """
        try:
            result = tab.run_js(script)
            return max(0, int(result or 0))
        except (TypeError, ValueError, OverflowError):
            return 0
        except Exception:
            return 0

    def _human_wait(self, tab: Any, seconds: float) -> None:
        """分段等待并穿插鼠标移动，避免长时间完全静止。"""
        if not self._human_interaction.enabled:
            time.sleep(max(0.0, seconds))
            return
        deadline = time.monotonic() + max(0, seconds)
        while time.monotonic() < deadline:
            self._human_mouse_move(tab)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, random.uniform(0.8, 1.5)))

    def _human_mouse_move(self, tab: Any) -> None:
        """在总开关开启时执行随机坐标的平滑移动。"""
        if self._human_interaction.enabled:
            self._human_interaction.move_to_fallback(tab)

    def _read_xpath(self, tab: Any, xpath: str, field_name: str = "未命名指标") -> str:
        """读取一个 XPath 文本；XPath 为空、元素不存在或异常时返回空字符串。"""
        if not xpath:
            LOGGER.warning("[TikTok][指标跳过] 字段=%s，原因=XPath 为空", field_name)
            return ""
        # 首次读取失败后等待 2 秒再重读一次。
        for attempt in range(2):
            if attempt > 0:
                LOGGER.warning(
                    "[TikTok][指标重试] 字段=%s，第 2/2 次读取前等待 %s 秒，xpath=%s",
                    field_name,
                    CLICK_RETRY_INTERVAL_SECONDS,
                    xpath,
                )
                self._human_wait(tab, CLICK_RETRY_INTERVAL_SECONDS)
            try:
                LOGGER.info("[TikTok][指标查找] 字段=%s，第 %s/2 次读取，xpath=%s", field_name, attempt + 1, xpath)
                element = tab.ele(f"xpath:{xpath}", timeout=3)
                if element:
                    raw_text = str(element.text or "").strip()
                    if raw_text:
                        LOGGER.info(
                            "[TikTok][指标抓取成功] 字段=%s，XPath=%s，原始文本=%r，原始类型=%s",
                            field_name,
                            xpath,
                            raw_text,
                            type(raw_text).__name__,
                        )
                        return raw_text
                    LOGGER.warning("[TikTok][指标文本为空] 字段=%s，已找到元素但 text 为空", field_name)
                else:
                    LOGGER.warning("[TikTok][指标未找到] 字段=%s，第 %s/2 次未找到元素", field_name, attempt + 1)
            except Exception as exc:
                LOGGER.warning(
                    "[TikTok][指标异常] 字段=%s，第 %s/2 次读取失败，异常=%s，xpath=%s",
                    field_name,
                    attempt + 1,
                    exc,
                    xpath,
                )
                continue
        LOGGER.error("[TikTok][指标最终失败] 字段=%s，两次读取均未得到有效文本，返回空字符串", field_name)
        return ""

    @staticmethod
    def _format_value(raw_text: str, kind: str, currency_code: str = "") -> Any:
        """按类型格式化：货币/ROI 两位小数、数量为整数、比率为无小数百分数。"""
        if not raw_text:
            return ""
        if kind == "integer":
            integer_text = re.sub(r"[^0-9-]", "", str(raw_text))
            if not integer_text or integer_text == "-":
                return ""
            try:
                return int(integer_text)
            except ValueError:
                return ""

        number = TiktokAuto._parse_number(raw_text, currency_code)
        if number is None:
            return ""
        if kind in {"currency", "decimal"}:
            return round(number, 2)
        if kind == "percent":
            return f"{round(number):.0f}%"
        return raw_text

    @staticmethod
    def _parse_number(raw_text: str, currency_code: str = "") -> float | None:
        """解析第一个数值，避免把同一元素后面的趋势百分比拼接到金额中。"""
        text = str(raw_text).strip()
        # 页面可能把整数、小数点和小数部分拆成多行，先清理分隔符两侧的换行和空格。
        text = re.sub(r"\s*([.,])\s*", r"\1", text)

        if currency_code == "USD":
            # 美元示例：$1,234.56。只提取第一个金额，忽略其后的环比/同比百分数。
            match = re.search(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", text)
            if not match:
                return None
            text = match.group(0).replace(",", "")
        elif currency_code == "BRL":
            # 巴西币示例：R$ 1.234,56。只提取第一个金额，忽略其后的趋势信息。
            match = re.search(r"-?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?", text)
            if not match:
                return None
            text = match.group(0).replace(".", "").replace(",", ".")
        else:
            text = text.replace("%", "")
            text = re.sub(r"[^0-9,.-]", "", text)

        if not text or text in {"-", ".", ","}:
            return None
        try:
            if not currency_code and "." in text and "," in text:
                # 无币种指标按最后出现的符号判断小数点。
                if text.rfind(",") > text.rfind("."):
                    text = text.replace(".", "").replace(",", ".")
                else:
                    text = text.replace(",", "")
            elif not currency_code and "," in text:
                text = text.replace(",", ".")
            return float(text)
        except (TypeError, ValueError):
            return None


def collect_tiktok_ad(store_name: str, download_path: str = "", debugging_port: int | str | None = None) -> list[dict[str, Any]]:
    """提供一个可直接调用的 TikTok 函数入口。"""
    return TiktokAuto().collect(store_name, download_path, debugging_port)
