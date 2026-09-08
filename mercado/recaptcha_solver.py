"""美客多登录页的 reCAPTCHA v2 网格验证码处理。"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from io import BytesIO
from typing import Any

import requests
from openai import OpenAI
from PIL import Image

LOGGER = logging.getLogger(__name__)

# 以下定位符按参考脚本及页面实际结构保留。
CSS_ENTRY_IFRAME = 'iframe[title="reCAPTCHA"]'
XPATH_CONTENT_IFRAME_PRIMARY = (
    "//iframe[contains(@src, '/recaptcha/enterprise/bframe')]"
)
XPATH_CONTENT_IFRAME_FALLBACK = (
    "//iframe[contains(@src, '/recaptcha/enterprise/bframe') and contains(@title, 'reCAPTCHA')]"
)
XPATH_CHECKBOX_BTN = '//div[@class="recaptcha-checkbox-border"]'
XPATH_RC_DIALOG = '//div[@id="rc-imageselect"]'
XPATH_QUESTION_TEXT = (
    '//div[@id="rc-imageselect"]'
    '//div[contains(@class, "rc-imageselect-desc")]//strong'
)
CSS_TILE_TD = '#rc-imageselect-target table td'
CSS_TILE_IMG = 'img'
XPATH_VERIFY_BTN = '//button[@id="recaptcha-verify-button"]'

# YesCaptcha ReCaptchaV2Classification 接口支持的题型白名单。
CAPTCHA_TARGET_NAME_QUESTION_ID_MAPPING: dict[str, str] = {
    "taxis": "/m/0pg52",
    "bus": "/m/01bjv",
    "school bus": "/m/02yvhj",
    "motorcycles": "/m/04_sv",
    "tractors": "/m/013xlm",
    "chimneys": "/m/01jk_4",
    "crosswalks": "/m/014xcs",
    "traffic lights": "/m/015qff",
    "bicycles": "/m/0199g",
    "parking meters": "/m/015qbp",
    "cars": "/m/0k4j",
    "vehicles": "/m/0k4j",
    "bridges": "/m/015kr",
    "boats": "/m/019jd",
    "palm trees": "/m/0cdl1",
    "mountains or hills": "/m/09d_r",
    "fire hydrant": "/m/01pns0",
    "fire hydrants": "/m/01pns0",
    "a fire hydrant": "/m/01pns0",
    "stairs": "/m/01lynh",
}

QUESTION_ID_PATTERN = re.compile(r"^/m/[A-Za-z0-9_]+$")


class MercadoRecaptchaSolver:
    """在紫鸟已接管的浏览器标签页中完成验证码识别。"""

    def __init__(self, page: Any, config: dict[str, Any]):
        self.page = page
        self.config = config
        self.wait_timeout = max(1.0, float(config.get("wait_timeout_seconds", 25) or 25))
        self.appearance_timeout = max(1.0, float(config.get("appearance_timeout_seconds", 25) or 25))
        self.max_retry = max(1, int(config.get("max_retry", 3) or 3))
        self.retry_interval = max(0.0, float(config.get("retry_interval_seconds", 5) or 5))
        self.api_key = str(config.get("api_key") or "").strip()
        self.create_task_url = str(config.get("create_task_url") or "https://api.yescaptcha.com/createTask").strip()
        self.result_url = str(config.get("result_url") or "https://api.yescaptcha.com/getTaskResult").strip()
        self.trigger_xpath = str(config.get("captcha_trigger_button_xpath") or XPATH_CHECKBOX_BTN).strip()
        self.question_id = ""

        deepseek_config = config.get("deepseek", {})
        if not isinstance(deepseek_config, dict):
            deepseek_config = {}
        deepseek_enabled = str(deepseek_config.get("enabled", True)).strip().lower()
        self.deepseek_enabled = deepseek_enabled not in {"false", "0", "no", "off", "关闭"}
        self.deepseek_api_key = str(deepseek_config.get("api_key") or "").strip()
        self.deepseek_model = str(deepseek_config.get("model_name") or "").strip()
        self.deepseek_base_url = str(
            deepseek_config.get("base_url") or "https://api.deepseek.com"
        ).rstrip("/")
        self.deepseek_timeout = max(
            1.0, float(deepseek_config.get("timeout_seconds", 300) or 300)
        )
        self.deepseek_retry_times = max(0, int(deepseek_config.get("retry_times", 5) or 0))
        self.deepseek_retry_interval = max(
            0.0, float(deepseek_config.get("retry_interval_seconds", 3) or 0)
        )
        self.deepseek_client = (
            OpenAI(
                api_key=self.deepseek_api_key,
                base_url=self.deepseek_base_url,
                timeout=self.deepseek_timeout,
            )
            if self.deepseek_api_key
            else None
        )

    def _get_frame(self, selector: str, timeout: float | None = None) -> Any:
        """获取 iframe 对象；DrissionPage 4.x 使用 frame 对象而非 Selenium switch_to。"""
        wait = self.wait_timeout if timeout is None else max(0.1, timeout)
        prefix = "xpath:" if selector.startswith("/") else "css:"
        frame = self.page.get_frame(f"{prefix}{selector}", timeout=wait)
        if not frame:
            raise RuntimeError(f"未找到 iframe：{selector}")
        return frame

    def _get_content_frame(self, timeout: float | None = None) -> Any:
        """按宽松、严格两个 XPath 顺序获取验证码挑战 iframe。"""
        wait = self.appearance_timeout if timeout is None else max(0.1, timeout)
        deadline = time.monotonic() + wait
        attempt = 0
        primary_count = 0
        fallback_count = 0
        last_error: Exception | None = None

        while time.monotonic() < deadline:
            attempt += 1
            remaining = max(0.1, deadline - time.monotonic())
            query_timeout = min(1.0, remaining)
            try:
                primary_frames = list(
                    self.page.eles(
                        f"xpath:{XPATH_CONTENT_IFRAME_PRIMARY}",
                        timeout=query_timeout,
                    )
                    or []
                )
                primary_count = len(primary_frames)
                if primary_count == 1:
                    LOGGER.info(
                        "[美客多][验证码 iframe] 第 %s 次查找成功，使用第一 XPath，匹配数=1",
                        attempt,
                    )
                    return primary_frames[0]

                fallback_frames = list(
                    self.page.eles(
                        f"xpath:{XPATH_CONTENT_IFRAME_FALLBACK}",
                        timeout=query_timeout,
                    )
                    or []
                )
                fallback_count = len(fallback_frames)
                if fallback_count == 1:
                    LOGGER.info(
                        "[美客多][验证码 iframe] 第一 XPath 匹配数=%s，使用第二 XPath成功，匹配数=1",
                        primary_count,
                    )
                    return fallback_frames[0]

                if attempt == 1 or attempt % 5 == 0:
                    LOGGER.info(
                        "[美客多][验证码 iframe] 第 %s 次等待，第一 XPath 匹配数=%s，第二 XPath 匹配数=%s",
                        attempt,
                        primary_count,
                        fallback_count,
                    )
            except Exception as exc:
                last_error = exc
                if attempt == 1 or attempt % 5 == 0:
                    LOGGER.warning(
                        "[美客多][验证码 iframe] 第 %s 次查找遇到页面刷新或元素变化：%s",
                        attempt,
                        exc,
                    )

            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.5, remaining))

        detail = (
            f"，最后异常={last_error}" if last_error is not None else ""
        )
        raise RuntimeError(
            f"验证码挑战 iframe 在 {wait:.1f} 秒内未唯一匹配："
            f"第一 XPath 匹配数={primary_count}，第二 XPath 匹配数={fallback_count}{detail}"
        )

    @staticmethod
    def _image_base64(image_url: str, target_size: int) -> str:
        """下载并缩放图片，返回纯 Base64 字符串。"""
        response = requests.get(image_url, timeout=10)
        response.raise_for_status()
        image = Image.open(BytesIO(response.content)).convert("RGB")
        image = image.resize((target_size, target_size), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    @staticmethod
    def _read_question_text(content_frame: Any) -> str:
        """在挑战 iframe 中读取当前图片题目。"""
        question_element = content_frame.ele(
            f"xpath:{XPATH_QUESTION_TEXT}",
            timeout=10,
        )
        if not question_element:
            raise RuntimeError("验证码挑战框中未找到题目文本")
        question_text = str(getattr(question_element, "text", "") or "").strip()
        if not question_text:
            raise RuntimeError("验证码挑战框中的题目文本为空")
        return question_text

    @staticmethod
    def _build_question_messages(question_text: str) -> list[dict[str, str]]:
        """构造只允许返回题型 ID 的 DeepSeek 请求。"""
        mapping_text = json.dumps(
            CAPTCHA_TARGET_NAME_QUESTION_ID_MAPPING,
            ensure_ascii=False,
            indent=2,
        )
        return [
            {
                "role": "system",
                "content": (
                    "你是 reCAPTCHA 图片题型匹配器。根据题目的真实语义，从给定映射表中选择最相关的一项。"
                    "题目可能是英语、巴西葡萄牙语或其他语言，必须按语义跨语言匹配。"
                    "最终只能返回映射表中的一个问题 ID，格式必须类似 /m/01bjv。"
                    "禁止返回名称、解释、标点、引号、Markdown 或任何其他文字。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"捕获到的验证码题目：{question_text}\n\n"
                    f"问题名称与问题 ID 映射表：\n{mapping_text}\n\n"
                    "请选择语义最接近的一项，只返回对应的问题 ID。"
                ),
            },
        ]

    def _match_question_id(self, question_text: str) -> str:
        """调用 config.yaml 中配置的 DeepSeek 模型匹配题型 ID。"""
        if not self.deepseek_enabled:
            raise RuntimeError("验证码题型匹配需要 DeepSeek，但 deepseek.enabled 为 false")
        if not self.deepseek_client or not self.deepseek_api_key:
            raise RuntimeError("验证码题型匹配需要 DeepSeek，但未配置 deepseek.api_key")
        if not self.deepseek_model:
            raise RuntimeError("验证码题型匹配需要 DeepSeek，但未配置 deepseek.model_name")

        messages = self._build_question_messages(question_text)
        allowed_ids = set(CAPTCHA_TARGET_NAME_QUESTION_ID_MAPPING.values())
        total_attempts = self.deepseek_retry_times + 1
        last_error: Exception | None = None

        for attempt in range(1, total_attempts + 1):
            try:
                LOGGER.info(
                    "[美客多][验证码题型] 捕获题目=%r，DeepSeek 匹配第 %s/%s 次",
                    question_text,
                    attempt,
                    total_attempts,
                )
                response = self.deepseek_client.chat.completions.create(
                    model=self.deepseek_model,
                    messages=messages,
                )
                question_id = str(response.choices[0].message.content or "").strip()
                if not QUESTION_ID_PATTERN.fullmatch(question_id):
                    raise RuntimeError(f"DeepSeek 未严格返回问题 ID 格式：{question_id!r}")
                if question_id not in allowed_ids:
                    raise RuntimeError(f"DeepSeek 返回的问题 ID 不在白名单中：{question_id}")
                LOGGER.info(
                    "[美客多][验证码题型] 匹配成功，题目=%r，问题ID=%s",
                    question_text,
                    question_id,
                )
                return question_id
            except Exception as exc:
                last_error = exc
                if attempt >= total_attempts:
                    break
                LOGGER.warning(
                    "[美客多][验证码题型] 第 %s/%s 次匹配失败：%s；%.1f 秒后重试",
                    attempt,
                    total_attempts,
                    exc,
                    self.deepseek_retry_interval,
                )
                if self.deepseek_retry_interval > 0:
                    time.sleep(self.deepseek_retry_interval)

        raise RuntimeError(
            f"DeepSeek 连续 {total_attempts} 次未返回有效验证码问题 ID：{last_error}"
        ) from last_error

    def _classify(self, image: str) -> dict[str, Any]:
        """把图片和已确认的问题 ID 发送给 YesCaptcha。"""
        if not self.api_key:
            raise RuntimeError("美客多验证码已出现，但未配置 MERCADO_YESCAPTCHA_API_KEY")
        if not self.question_id:
            raise RuntimeError("验证码图片识别前尚未获得有效问题 ID")
        response = requests.post(
            self.create_task_url,
            json={
                "clientKey": self.api_key,
                "task": {
                    "type": "ReCaptchaV2Classification",
                    "image": image,
                    "question": self.question_id,
                },
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("errorId") != 0:
            raise RuntimeError(f"YesCaptcha 创建任务失败：errorId={data.get('errorId')}")
        task_id = data.get("taskId")
        if not task_id:
            raise RuntimeError("YesCaptcha 未返回 taskId")
        for _ in range(20):
            time.sleep(2)
            result = requests.post(
                self.result_url,
                json={"clientKey": self.api_key, "taskId": task_id},
                timeout=60,
            )
            result.raise_for_status()
            result_data = result.json()
            if result_data.get("status") == "ready":
                return result_data.get("solution") or {}
            if result_data.get("errorId") != 0:
                raise RuntimeError(f"YesCaptcha 获取结果失败：errorId={result_data.get('errorId')}")
        raise TimeoutError("YesCaptcha 获取结果超时")

    def _check_success(self) -> bool:
        """确认入口 iframe 中的复选框已经通过。"""
        try:
            entry_frame = self._get_frame(CSS_ENTRY_IFRAME, timeout=2)
            anchor = entry_frame.ele("css:#recaptcha-anchor", timeout=2)
            return str(anchor.attr("aria-checked") or "").lower() == "true"
        except Exception:
            return False

    def solve(self) -> bool:
        """按参考脚本重试验证码，成功返回 True。"""
        last_error: Exception | None = None
        for attempt in range(self.max_retry):
            try:
                LOGGER.info("[美客多][验证码] 开始第 %s/%s 次识别", attempt + 1, self.max_retry)
                entry_frame = self._get_frame(CSS_ENTRY_IFRAME, timeout=self.appearance_timeout)
                trigger = entry_frame.ele(f"xpath:{self.trigger_xpath}", timeout=self.wait_timeout)
                if not trigger:
                    raise RuntimeError(f"未找到 reCAPTCHA 触发按钮（iframe 内）：{self.trigger_xpath}")
                trigger.click()
                LOGGER.info("[美客多][验证码] 复选框已点击，等待挑战 iframe 出现")

                content_frame = self._get_content_frame(timeout=self.appearance_timeout)
                dialog = content_frame.ele(f"xpath:{XPATH_RC_DIALOG}", timeout=self.wait_timeout)
                if not dialog:
                    raise RuntimeError("未找到 reCAPTCHA 挑战对话框")

                question_text = self._read_question_text(content_frame)
                self.question_id = self._match_question_id(question_text)

                tiles = list(content_frame.eles(f"css:{CSS_TILE_TD}", timeout=self.wait_timeout) or [])
                if len(tiles) not in {9, 16}:
                    raise RuntimeError(f"不支持的验证码网格数量：{len(tiles)}")
                image = tiles[0].ele(f"css:{CSS_TILE_IMG}", timeout=self.wait_timeout)
                image_url = str(image.attr("src") or "")
                solution = self._classify(
                    self._image_base64(image_url, 300 if len(tiles) == 9 else 450)
                )
                for index in solution.get("objects", []) or []:
                    if isinstance(index, int) and 0 <= index < len(tiles):
                        tiles[index].click()
                verify_button = content_frame.ele(
                    f"xpath:{XPATH_VERIFY_BTN}",
                    timeout=self.wait_timeout,
                )
                if not verify_button:
                    raise RuntimeError("未找到 reCAPTCHA 验证按钮")
                verify_button.click()
                time.sleep(2)
                if self._check_success():
                    LOGGER.info("[美客多][验证码] 第 %s 次识别成功", attempt + 1)
                    return True
                raise RuntimeError("验证码提交后未确认通过")
            except Exception as exc:
                last_error = exc
                LOGGER.warning("[美客多][验证码] 第 %s 次识别失败：%s", attempt + 1, exc)
                LOGGER.debug("[美客多][验证码] 第 %s 次识别失败：%s", attempt + 1, exc)
                if attempt < self.max_retry - 1:
                    time.sleep(self.retry_interval)

        raise RuntimeError(f"美客多验证码连续 {self.max_retry} 次失败：{last_error}")
