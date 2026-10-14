"""三个平台今天数据的字段标准化、币种元数据和展示规则。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

BRAZIL_TIMEZONE = timezone(timedelta(hours=-3), "BRT")
SHOPEE_AD_LABELS = (
    "展示次数", "点击数", "点击率", "订单量", "商品已出售", "销售额", "花费",
    "广告支出回报率", "优惠价金额", "优惠劵带来销售额", "加购次数", "加购率",
)
SHOPEE_BUSINESS_LABELS = (
    "销售额", "销售额（扣除Shopee补贴）", "订单数", "订单转化率", "访客数", "商品点击量", "每个订单的销售额",
)


def is_today_field(name: str) -> bool:
    """仅接收明确标为今天的字段，不把其他日期转换成今天。"""
    return name.startswith(("今天", "_今天")) or name.endswith("_今天")


def number_value(raw: Any) -> int | float | None:
    """保留零，解析巴西及翻译后的分隔符；占位符不伪装成零。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return raw if math.isfinite(raw) else None
    text = str(raw or "").strip()
    if not re.search(r"\d", text):
        return None
    percent = "%" in text
    numeric = re.sub(r"[^\d,.-]", "", text)
    if "," in numeric and "." in numeric:
        decimal = "," if numeric.rfind(",") > numeric.rfind(".") else "."
        numeric = numeric.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif "," in numeric or "." in numeric:
        separator = "," if "," in numeric else "."
        parts = numeric.split(separator)
        grouped = not percent and len(parts) > 1 and all(len(part) == 3 for part in parts[1:])
        numeric = "".join(parts) if grouped else numeric.replace(separator, ".")
    try:
        value = float(numeric)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return value / 100 if percent else value


def _canonical_name(name: str, platform: str, module: str) -> str:
    if platform == "tiktok":
        if name == "今天SKU订单数":
            return "今天概览SKU订单数" if module == "概览" else "今天广告SKU订单数"
        return name.removeprefix("_")
    if platform == "shopee":
        suffix = name.removeprefix("今天").removeprefix("ALL")
        suffix = suffix.replace("优惠券", "优惠劵")
        return "今天广告支出回报率" if suffix == "广告支出回报率" else f"今天广告{suffix}"
    return name


def _metadata(field: str, value: Any, platform: str, module: str, supplied: dict[str, Any] | None = None) -> dict[str, Any]:
    details = dict(supplied or {})
    money = any(word in field for word in ("GMV", "销售额", "销售价值", "退货价值", "平均单价", "成本", "花费", "均单价", "总收入", "优惠价金额", "每个订单的销售额"))
    ratio = any(word in field for word in ("转换率", "转化率", "点击率", "加购率", "GMV直播比", "GMV视频比", "GMV商品卡比"))
    multiple = field.endswith("ROI") or "支出回报率" in field
    unit = "ratio" if ratio else "multiple" if multiple else "currency" if money else "count"
    details.setdefault("unit", unit)
    details.setdefault("module", module)
    details["period"] = "今天"
    if money and not ratio:
        raw = str(value)
        currency = "USD" if "USD" in raw.upper() else "BRL" if "BRL" in raw.upper() or "R$" in raw else None
        details["currency"] = currency or details.get("currency") or ("USD" if platform == "tiktok" and module == "广告" else "BRL")
    return details


def extract_today(rows: list[dict[str, Any]], platform: str = "") -> tuple[str, dict[str, Any], dict[str, Any]]:
    """按来源提取今天指标，避免广告订单与经营订单覆盖，并保留每项金额币种。"""
    collected_at = ""
    values: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    for row in rows:
        platform_key = platform or str(row.get("平台") or "")
        metric = str(row.get("指标") or "")
        if not platform_key:
            platform_key = "tiktok" if "TikTok" in metric else "shopee" if "Shopee" in metric or "ALL" in metric else "mercado"
        collected_at = collected_at or str(row.get("采集时间") or "")
        module = "概览" if "概览" in metric else "经营" if platform_key == "mercado" else "广告"
        supplied = row.get("指标元数据", {})
        supplied = supplied if isinstance(supplied, dict) else {}
        fields = row.get("飞书字段", {})
        fields = fields if isinstance(fields, dict) else {}
        if fields:
            for name, value in fields.items():
                if not is_today_field(name):
                    continue
                canonical = _canonical_name(name, platform_key, module)
                actual_module = "广告" if "广告" in canonical else module
                number = number_value(value)
                if number is not None:
                    values[canonical] = number
                metadata[canonical] = _metadata(canonical, value, platform_key, actual_module, supplied.get(name))
            continue
        if not is_today_field(metric):
            continue
        if metric.startswith("Shopee商业分析概述_"):
            try:
                payload = json.loads(row.get("数值") or row.get("显示值") or "{}")
            except (TypeError, ValueError):
                continue
            for item in payload.get("指标", []) if isinstance(payload, dict) else []:
                name = str(item.get("名称", ""))
                if name not in SHOPEE_BUSINESS_LABELS:
                    continue
                canonical = f"今天经营{name}"
                value = item.get("值")
                parsed = number_value(value)
                if parsed is not None:
                    values[canonical] = parsed
                    metadata[canonical] = _metadata(canonical, value, "shopee", "经营")
            continue
        canonical = _canonical_name(metric, platform_key, module)
        value = row.get("数值")
        parsed = number_value(value)
        if parsed is not None:
            values[canonical] = parsed
            metadata[canonical] = _metadata(canonical, row.get("显示值", value), platform_key, module, supplied.get(metric))
    return collected_at, values, metadata


def business_date(collected_at: str) -> str:
    """业务日期使用巴西 UTC-3，不能使用北京日期替代。"""
    try:
        moment = datetime.fromisoformat(str(collected_at).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        moment = datetime.now(timezone.utc)
    return moment.astimezone(BRAZIL_TIMEZONE).date().isoformat()


def capture_batch(platform: str, store_name: str, collected_at: str) -> str:
    """同一采集快照无论补写多少次，使用同一个批次标识。"""
    return hashlib.sha256(f"{platform}\n{store_name}\n{collected_at}".encode()).hexdigest()[:24]


def today_record(platform: str, store_name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    collected_at, values, metadata = extract_today(rows, platform)
    collected_at = collected_at or datetime.now(timezone.utc).isoformat()
    return {
        "店铺名": store_name, "平台": platform, "业务日期": business_date(collected_at),
        "采集时间": collected_at, "采集批次": capture_batch(platform, store_name, collected_at),
        "指标币种": json.dumps(metadata, ensure_ascii=False, sort_keys=True), **values,
    }


def display_metric(field: str, value: Any, metadata: dict[str, Any]) -> str:
    if value in ("", None):
        return "-"
    unit = metadata.get("unit", "count")
    if metadata.get("currency") and unit == "currency":
        return f"{metadata['currency']} {float(value):.2f}"
    if unit in {"ratio", "percent"}:
        return f"{float(value) * 100:.2f}%"
    if unit in {"multiple", "decimal"} or field.endswith("ROI"):
        return f"{float(value):.2f}"
    return str(int(value)) if isinstance(value, (int, float)) and float(value).is_integer() else str(value)
