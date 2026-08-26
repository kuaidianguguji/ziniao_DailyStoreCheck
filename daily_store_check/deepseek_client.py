"""DeepSeek 单店分析与全店铺汇总客户端。

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


SHOPEE_SYSTEM_PROMPT = r"""
# 角色

你是一名资深的巴西电商运营经理、Shopee Brazil 店铺运营专家和经营数据分析师。

你会收到单个 Shopee Brazil 店铺的完整原始经营数据。你的任务不是复述数据，而是完成：

数据清洗 -> 指标计算 -> 经营诊断 -> 异常识别 -> 原因拆解 -> 运营优先级 -> 可执行建议。

# 分析原则

1. 所有结论必须来自输入数据，禁止编造行业均值、平台标准或不存在的数据。
2. 缺少历史基准时，明确说明“当前缺少历史基准，暂不判断绝对好坏”。
3. `-`、空字符串、null、“数据尚未准备好”、“抓取失败”和“未找到”均为数据缺失，不得按 0 计算。
4. 字段值可能是 JSON 字符串，必须先解析；字段名前后空格、中文空格和换行应先标准化。
5. 巴西金额和数字使用点作为千位分隔符、逗号作为小数分隔符；程序清洗后的 `R$8044.00` 等值则按现有小数点解释。
6. 不同模块可能存在统计口径、归因和更新时间差异，不要强行让数字一致。
7. Shopee Brazil 使用 America/Sao_Paulo 时间；今天的数据属于实时数据，样本小时只能作为观察项。
8. 不要完整抄写输入数据，只突出 3 至 5 个最值得运营关注的问题。

# 时间比较

分析优先级为：昨天完整数据、近7天基准、今天实时数据。

累计指标比较昨天和近7天时，先计算“近7天日均 = 近7天累计 / 7”，再计算昨日相对日均变化。比率指标可直接比较昨天与近7天。

# 经营分析

1. 销售结果：使用“销售额约等于订单数乘客单价”和“订单数约等于商品点击量乘订单转化率”，判断问题主要来自流量、转化还是客单价。
2. 商品漏斗：商品曝光 -> 商品点击 -> 商品访客 -> 加购 -> 下单 -> 付款。数据允许时计算下单到付款率。
3. 广告诊断：至少分析展示、点击、CTR、花费、广告订单、广告销售额、ROAS、加购次数和加购率。
4. 数据允许时计算：CPC、广告CVR、CPA、广告客单价、ACOS、TACOS、广告销售贡献率和广告订单贡献率。
5. ROAS 变化必须结合 CTR、CPC、广告CVR和广告客单价拆解，不能仅因单日下降就建议停广告或大幅降预算。
6. 流量来源重点识别高曝光低CTR、高点击低转化、高转化低流量、高流量高转化。
7. “商品卡”可能是整体汇总，搜索、推荐、购物车等可能是其子来源，不得重复相加。
8. “其他”来源归因不明确时只指出需要核对口径，不得自行定义其组成。

# 异常与建议

异常等级使用：正常、关注、异常、暂不判断、数据缺失。优先根据近7天、同店趋势和指标逻辑判断，不得凭空设置固定行业阈值。小样本波动不得定性为严重异常。

建议必须采用“现象 -> 原因 -> 动作”的逻辑，具体到当天可以执行。每天只给 3 至 5 条建议，按 P1、P2、P3 等优先级排列。

# 输出格式

只输出可直接发送给运营人员的简体中文 Markdown，不展示冗长推理过程。必须使用以下结构：

# {店铺名} Shopee 每日经营分析

## 一、经营结论

用 3 至 6 句话概括昨天表现、主要问题、最大机会和今天最优先动作。

## 二、核心经营指标

| 指标 | 昨天 | 近7天/日均 | 判断 |
|---|---:|---:|---|

至少包含销售额、订单数、客单价、访客数、商品点击量和订单转化率。缺失时写“数据缺失”。

## 三、广告诊断

| 指标 | 昨天 | 近7天 | 判断 |
|---|---:|---:|---|

至少包含 CTR、CPC、广告CVR、CPA、广告客单价、ROAS、ACOS，并用一段话说明 ROAS 变化主要来自点击成本、转化率还是客单价。

## 四、商品转化漏斗

| 环节 | 昨天数据 | 判断 |
|---|---:|---|

至少观察商品访客、跳出率、加购率、下单转化率、付款转化率和下单到付款率。

## 五、流量来源诊断

| 来源 | 点击 | CTR | 订单 | CVR | 销售额 | 判断 |
|---|---:|---:|---:|---:|---:|---|

只列有数据的主要来源，不要对极小样本过度分析。

## 六、今日实时观察

说明采集时间，只列真正值得观察的 2 至 4 项，并标记小样本。

## 七、异常与机会

最多 5 项，按照影响大小排序。

## 八、今日行动建议

只给 3 至 5 条按优先级排列、可以直接执行的建议。

## 九、数据质量

仅说明缺失或冲突的核心模块及其是否影响判断；没有明显问题时可以省略。
""".strip()


class DeepSeekClient:
    """复用同一客户端执行三平台单店分析和全店铺汇总。"""

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
        shopee_system_prompt = str(
            deepseek_config.get("shopee_system_prompt") or SHOPEE_SYSTEM_PROMPT
        ).strip()
        # 三个平台共用客户端、密钥和模型，仅系统提示词彼此独立。
        # TikTok 和美客多尚未提供专用提示词时，临时回退到虾皮提示词。
        self.platform_system_prompts = {
            "tiktok": str(deepseek_config.get("tiktok_system_prompt") or shopee_system_prompt).strip(),
            "shopee": shopee_system_prompt,
            "mercado": str(deepseek_config.get("mercado_system_prompt") or shopee_system_prompt).strip(),
        }

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
    2. 自动识别 Shopee（虾皮）、TikTok Shop、Mercado Livre（美客多）店铺。
    3. 不同平台必须使用各自的数据分析逻辑。
    4. 不得将不同店铺的数据混合。
    5. 不得将广告订单与自然订单混淆。
    6. 所有计算必须基于原始数据。
    7. 数据有矛盾时必须明确指出。
    8. 样本量过小时必须说明。
    9. 最终重点告诉我：
    
       - 今天最需要处理哪些店铺
       - 为什么
    
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

    @staticmethod
    def build_store_user_prompt(shop_data: str, platform_name: str) -> str:
        """构造单个平台店铺的分析请求，保留完整原始数据。"""
        return f"""
以下是今天需要分析的单个巴西 {platform_name} 店铺原始数据。

请严格按照系统提示词中的规则完成分析。

重要要求：

1. 必须分析全部有效数据，不能遗漏核心模块。
2. 不得将广告订单与自然订单混淆。
3. 所有计算必须基于原始数据。
4. 数据有矛盾时必须明确指出。
5. 样本量过小时必须说明。
6. 最终重点说明发生了什么、为什么、是否严重，以及今天应该执行什么。

================ 原始店铺数据 ================

{shop_data}

================ 数据结束 ================
"""

    def analyze_store(self, store_info: dict[str, Any]) -> str:
        """使用对应平台提示词同步分析单个店铺，并返回 Markdown。"""
        platform = str(store_info.get("平台") or "").strip().lower()
        platform_names = {
            "tiktok": "TikTok Shop",
            "shopee": "Shopee",
            "mercado": "Mercado Livre",
        }
        platform_name = platform_names.get(platform, platform or "未知平台")
        if not store_info:
            LOGGER.info("[DeepSeek][单店跳过] 店铺数据为空")
            return ""
        if platform not in self.platform_system_prompts:
            LOGGER.warning("[DeepSeek][单店跳过] 不支持的平台=%s", platform_name)
            return ""
        if not self.enabled:
            LOGGER.info("[DeepSeek][单店跳过] 平台=%s，deepseek.enabled=false", platform_name)
            return ""
        if not self.configured:
            LOGGER.warning(
                "[DeepSeek][单店跳过] 平台=%s，配置不完整，缺少=%s",
                platform_name,
                self._missing_config_fields(),
            )
            return ""

        shop_data = json.dumps([store_info], ensure_ascii=False, default=str)
        user_prompt = self.build_store_user_prompt(shop_data, platform_name)
        system_prompt = self.platform_system_prompts[platform]
        store_name = str(store_info.get("店铺名") or "未知店铺")
        LOGGER.info(
            "[DeepSeek][单店请求准备] 平台=%s，店铺=%s，url=%s，model=%s，原始数据字符数=%s，"
            "timeout=%s，thinking=enabled，reasoning_effort=high，重试次数=%s",
            platform_name,
            store_name,
            f"{self.base_url}/chat/completions",
            self.model_name,
            len(shop_data),
            self.timeout_seconds,
            self.retry_times,
        )

        total_attempts = self.retry_times + 1
        for attempt in range(1, total_attempts + 1):
            LOGGER.info(
                "[DeepSeek][单店分析请求] 平台=%s，店铺=%s，第 %s/%s 次尝试",
                platform_name,
                store_name,
                attempt,
                total_attempts,
            )
            try:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": system_prompt},
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
                    "[DeepSeek][单店分析成功] 平台=%s，店铺=%s，第 %s/%s 次请求成功，返回字符数=%s",
                    platform_name,
                    store_name,
                    attempt,
                    total_attempts,
                    len(answer),
                )
                return answer
            except Exception as exc:
                if attempt >= total_attempts:
                    LOGGER.exception(
                        "[DeepSeek][单店分析最终失败] 平台=%s，店铺=%s，已完成 %s 次请求",
                        platform_name,
                        store_name,
                        total_attempts,
                    )
                    raise RuntimeError(
                        f"DeepSeek 对店铺 {store_name} 的分析连续 {total_attempts} 次失败: {exc}"
                    ) from exc
                LOGGER.warning(
                    "[DeepSeek][单店分析失败准备重试] 平台=%s，店铺=%s，第 %s/%s 次失败；%.1f 秒后重试",
                    platform_name,
                    store_name,
                    attempt,
                    total_attempts,
                    self.retry_interval_seconds,
                )
                if self.retry_interval_seconds > 0:
                    time.sleep(self.retry_interval_seconds)

        raise RuntimeError("DeepSeek 单店分析未返回结果")

    def analyze_shopee_store(self, store_info: dict[str, Any]) -> str:
        """兼容原有调用名称，实际进入三平台共用的单店分析流程。"""
        return self.analyze_store(store_info)

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
