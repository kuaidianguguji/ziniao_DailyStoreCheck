"""DeepSeek 全店铺分析客户端。

本文件按 test/DS_an2.py 的调用流程实现：使用 OpenAI SDK、参考脚本的提示词、
原始店铺数据用户消息，以及 thinking=enabled / reasoning_effort=high 请求参数。
飞书消息发送仍由 orchestrator.py 负责。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from openai import OpenAI


LOGGER = logging.getLogger(__name__)


# 必须与 test/DS_an2.py 中 SYSTEM_PROMPT 的内容保持一致。
REFERENCE_SYSTEM_PROMPT = r"""
在这里粘贴你的完整提示词。

例如：

你是一名资深巴西电商运营经理和数据分析师，
长期负责：

Shopee Brazil
TikTok Shop Brazil
Mercado Livre Brazil

我每天会提供多个平台、多个店铺的经营数据。

你的任务是：

数据清洗
→ 指标计算
→ 单店诊断
→ 同平台横向比较
→ 异常识别
→ 机会识别
→ 给出运营优先级

所有结论必须严格基于我提供的数据。

禁止虚构数据。
""".strip()


class DeepSeekClient:
    """按照 DS_an2.py 调用 DeepSeek v4 pro 的全店铺分析客户端。"""

    def __init__(self, config: dict[str, Any]):
        """读取配置并创建与参考脚本相同的 OpenAI 客户端。"""
        deepseek_config = config.get("deepseek", {})
        if not isinstance(deepseek_config, dict):
            deepseek_config = {}

        enabled_value = deepseek_config.get("enabled", True)
        self.enabled = str(enabled_value).strip().lower() not in {"false", "0", "no", "off", "关闭"}
        self.api_key = str(deepseek_config.get("api_key") or "").strip()
        self.model_name = str(deepseek_config.get("model_name") or "deepseek-v4-pro").strip()
        self.base_url = str(deepseek_config.get("base_url") or "https://api.deepseek.com").rstrip("/")
        self.timeout_seconds = float(deepseek_config.get("timeout_seconds", 300) or 300)

        # 参考脚本中的系统提示词是固定内容，当前请求直接使用该原文。
        self.system_prompt = REFERENCE_SYSTEM_PROMPT

        # 保留项目原有失败重试；每次重试仍使用完全相同的 DS_an2 请求体。
        self.retry_times = max(0, int(deepseek_config.get("retry_times", 5)))
        self.retry_interval_seconds = max(0.0, float(deepseek_config.get("retry_interval_seconds", 3)))

        self.client = (
            OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout_seconds,
            )
            if self.api_key
            else None
        )

    @property
    def configured(self) -> bool:
        """判断是否具备发送参考脚本请求的必要配置。"""
        return bool(self.enabled and self.api_key and self.model_name and self.base_url and self.client)

    @staticmethod
    def build_user_prompt(shop_data: str) -> str:
        """完全复制 DS_an2.py 的 build_user_prompt 文本结构。"""
        return f"""
以下是今天需要分析的巴西电商店铺原始数据。

请严格按照系统提示词中的规则完成分析。

重要要求：

1. 必须分析全部店铺，不能遗漏。
2. 自动识别 Shopee、TikTok Shop、Mercado Livre。
3. 不同平台必须使用各自的数据分析逻辑。
4. 不得将不同店铺的数据混合。
5. 不得将广告订单与自然订单混淆。
6. 所有计算必须基于原始数据。
7. 数据有矛盾时必须明确指出。
8. 样本量过小时必须说明。
9. 最终重点告诉我：

   - 今天最需要处理哪些店铺
   - 为什么
   - 哪些店铺值得放量
   - 今天具体应该做什么

================ 原始店铺数据 ================

{shop_data}

================ 数据结束 ================
"""

    def analyze_all_info(self, all_info: list[dict[str, Any]]) -> str:
        """将 ALL_info 按 DS_an2.py 流程发送并返回分析文本。"""
        if not all_info:
            LOGGER.info("[DeepSeek][跳过] ALL_info 为空，不发送分析请求")
            return ""
        if not self.enabled:
            LOGGER.info("[DeepSeek][跳过] deepseek.enabled=false")
            return ""
        if not self.configured:
            LOGGER.warning("[DeepSeek][跳过] 配置不完整，缺少=%s", self._missing_config_fields())
            return ""

        # DS_an2.py 的 SHOP_DATA 是原始文本；这里仅序列化 ALL_info，不额外总结或改写。
        shop_data = json.dumps(all_info, ensure_ascii=False, default=str)
        user_prompt = self.build_user_prompt(shop_data)
        request_url = f"{self.base_url}/chat/completions"
        LOGGER.info(
            "[DeepSeek][DS_an2请求准备] url=%s，model=%s，店铺数=%s，原始数据字符数=%s，"
            "timeout=%s，thinking=enabled，reasoning_effort=high，重试次数=%s",
            request_url,
            self.model_name,
            len(all_info),
            len(shop_data),
            self.timeout_seconds,
            self.retry_times,
        )
        LOGGER.info("[DeepSeek][DS_an2系统提示词] %s", self.system_prompt)
        LOGGER.info("[DeepSeek][DS_an2用户提示词] %s", user_prompt)

        total_attempts = self.retry_times + 1
        for attempt in range(1, total_attempts + 1):
            LOGGER.info("[DeepSeek][分析请求] 第 %s/%s 次尝试", attempt, total_attempts)
            try:
                # 这一段与 DS_an2.py 的 client.chat.completions.create 完全一致。
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    extra_body={
                        "thinking": {"type": "enabled"},
                        "reasoning_effort": "high",
                    },
                )
                answer = str(response.choices[0].message.content or "").strip()
                if not answer:
                    raise RuntimeError("DeepSeek 返回内容为空")
                LOGGER.info(
                    "[DeepSeek][分析成功] 第 %s/%s 次请求成功，返回字符数=%s，分析结果=%r",
                    attempt,
                    total_attempts,
                    len(answer),
                    answer,
                )
                return answer
            except Exception as exc:
                if attempt >= total_attempts:
                    LOGGER.exception("[DeepSeek][分析最终失败] 已完成 %s 次请求，最后异常=%s", total_attempts, exc)
                    raise RuntimeError(f"DeepSeek 分析连续 {total_attempts} 次失败: {exc}") from exc
                LOGGER.warning(
                    "[DeepSeek][分析失败准备重试] 第 %s/%s 次失败，异常=%s；%.1f 秒后进行第 %s 次尝试",
                    attempt,
                    total_attempts,
                    exc,
                    self.retry_interval_seconds,
                    attempt + 1,
                )
                if self.retry_interval_seconds > 0:
                    time.sleep(self.retry_interval_seconds)

        raise RuntimeError("DeepSeek 分析未返回结果")

    def _missing_config_fields(self) -> list[str]:
        """返回日志用的缺失配置名。"""
        missing: list[str] = []
        if not self.api_key:
            missing.append("api_key")
        if not self.model_name:
            missing.append("model_name")
        if not self.base_url:
            missing.append("base_url")
        return missing
