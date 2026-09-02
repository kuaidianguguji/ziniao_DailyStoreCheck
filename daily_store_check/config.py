"""配置加载与校验。

真实凭据只允许通过 config/config.yaml 或环境变量注入；代码中只保留字段占位。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """读取 YAML 配置，并用环境变量覆盖常见的敏感字段。"""
    path = Path(config_path or os.getenv("DAILY_STORE_CONFIG", DEFAULT_CONFIG_PATH))
    with path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}

    # 环境变量用于部署机密，不强制要求用户现在提供。
    feishu = config.setdefault("feishu", {})
    feishu["app_id"] = os.getenv("FEISHU_APP_ID", feishu.get("app_id", ""))
    feishu["app_secret"] = os.getenv("FEISHU_APP_SECRET", feishu.get("app_secret", ""))
    deepseek = config.setdefault("deepseek", {})
    # API Key 既可以直接填写在 config.yaml，也可以在部署环境中用 DEEPSEEK_API_KEY 覆盖。
    deepseek["api_key"] = os.getenv("DEEPSEEK_API_KEY", deepseek.get("api_key", ""))
    platforms = config.setdefault("platforms", {})
    mercado = platforms.setdefault("mercado", {})
    mercado_login = mercado.setdefault("login", {})
    mercado_captcha = mercado_login.setdefault("captcha", {})
    mercado_captcha["api_key"] = os.getenv(
        "MERCADO_YESCAPTCHA_API_KEY", mercado_captcha.get("api_key", "")
    )
    ziniao = config.setdefault("ziniao", {})
    user_info = ziniao.setdefault("user_info", {})
    for key, env_name in (("company", "ZINIAO_COMPANY"), ("username", "ZINIAO_USERNAME"), ("password", "ZINIAO_PASSWORD")):
        user_info[key] = os.getenv(env_name, user_info.get(key, ""))
    return config


def normalise_platform(platform: Any) -> str:
    """把控制表中的平台名称统一成内部 key。"""
    value = str(platform or "").strip().lower()
    aliases = {
        "tk": "tiktok",
        "tiktok": "tiktok",
        "sp": "shopee",
        "shopee": "shopee",
        "mkd": "mercado",
        "meicado": "mercado",
        "mercado": "mercado",
        "mercadolibre": "mercado",
    }
    return aliases.get(value, value)


def is_enabled_switch(value: Any) -> bool:
    """判断控制表“开关”是否允许执行，空值和暂停均视为关闭。"""
    if value is None or str(value).strip() == "":
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"开启", "开", "启用", "运行", "执行", "on", "true", "1", "是"}


def is_period_enabled(platform_config: dict[str, Any] | None, period: str) -> bool:
    """读取平台时间范围开关；未配置时默认开启，兼容中文和英文配置键。"""
    config = platform_config if isinstance(platform_config, dict) else {}
    periods = config.get("periods", {})
    if not isinstance(periods, dict):
        return True

    aliases: dict[str, tuple[str, ...]] = {
        "今天": ("今天", "today"),
        "昨天": ("昨天", "yesterday"),
        "7天": ("7天", "7d", "last_7_days", "last7days"),
        "30天": ("30天", "30d", "last_30_days", "last30days"),
    }
    candidate_keys = aliases.get(period, (period,))
    selected_key = next((key for key in candidate_keys if key in periods), None)
    if selected_key is None:
        return True

    value = periods[selected_key]
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"false", "0", "no", "off", "关闭", "停用"}


@dataclass(frozen=True)
class StoreTask:
    """控制表中一条可执行店铺任务的标准结构。"""

    store_name: str
    recipient: str
    platform: str
    browser_oauth: str = ""
    browser_id: str = ""
    source_record_id: str = ""
