"""三个平台的今日飞书字段定义，旧周期字段只保留历史记录。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StorageField:
    """飞书字段类型：文本为 1，数值为 2，日期为 5。"""

    name: str
    field_type: int = 2


METADATA_FIELD_ORDER: tuple[str, ...] = (
    "店铺名", "平台", "业务日期", "采集时间", "采集批次", "指标币种",
)

TODAY_FIELD_ORDER_BY_PLATFORM: dict[str, tuple[str, ...]] = {
    "tiktok": (
        "今天成本", "今天广告SKU订单数", "今天均单价", "今天总收入", "今天ROI",
        "今天GMV", "今天成交件数", "今天概览SKU订单数", "今天订单数", "今天客户数",
        "今天商品访客数", "今天曝光数", "今天去重曝光数", "今天直播GMV", "今天短视频GMV",
        "今天商品卡GMV", "今天GMV直播比", "今天GMV视频比", "今天GMV商品卡比",
    ),
    "shopee": (
        "今天广告展示次数", "今天广告点击数", "今天广告点击率", "今天广告订单量",
        "今天广告商品已出售", "今天广告销售额", "今天广告花费", "今天广告支出回报率",
        "今天广告优惠价金额", "今天广告优惠劵带来销售额", "今天广告加购次数", "今天广告加购率",
        "今天经营销售额", "今天经营销售额（扣除Shopee补贴）", "今天经营订单数",
        "今天经营订单转化率", "今天经营访客数", "今天经营商品点击量", "今天经营每个订单的销售额",
    ),
    "mercado": (
        "今天总销售额", "今天已售件数", "今天平均单价", "今天访问", "今天销售量",
        "今天转换率", "今天取消的销售数量", "今天取消的销售价值", "今天退货数量",
        "今天退货价值", "今天独特的参观", "今天购买意向", "今天独立意向转换率",
        "今天意向购买转换率", "今天总转换率", "今天广告销售额", "今天广告曝光数",
        "今天广告访客数", "今天广告新粉丝数", "今天广告成本", "今天广告点击量", "今天广告销售量",
    ),
}


def today_field_names(platform: str) -> tuple[str, ...]:
    """返回平台今日宽行顺序，金额币种和业务日期单独保存。"""
    return METADATA_FIELD_ORDER + TODAY_FIELD_ORDER_BY_PLATFORM[platform]


def today_field_specs(platform: str) -> tuple[StorageField, ...]:
    """生成今日可写字段；比例保持 0 到 1 的数值，不创建只读公式。"""
    return tuple(
        StorageField(name, 5 if name == "采集时间" else 1)
        for name in METADATA_FIELD_ORDER
    ) + tuple(StorageField(name) for name in TODAY_FIELD_ORDER_BY_PLATFORM[platform])
