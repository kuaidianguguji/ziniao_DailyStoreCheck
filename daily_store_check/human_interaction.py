"""跨平台的真人鼠标交互辅助。"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HumanInteractionSettings:
    """真人鼠标参数；所有数值都可以通过 config.yaml 覆盖。"""

    enabled: bool = True
    move_min_seconds: float = 0.3
    move_max_seconds: float = 1.0
    jitter_pixels: float = 1.0
    click_pause_min_seconds: float = 0.2
    click_pause_max_seconds: float = 0.6
    safe_inset_ratio: float = 0.18
    fallback_x_min: int = 80
    fallback_x_max: int = 700
    fallback_y_min: int = 80
    fallback_y_max: int = 500

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "HumanInteractionSettings":
        """从配置读取参数，并限制到合理范围。"""
        values = config if isinstance(config, dict) else {}

        def number(name: str, default: float, minimum: float = 0.0) -> float:
            try:
                return max(minimum, float(values.get(name, default)))
            except (TypeError, ValueError):
                return default

        enabled = values.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in {"false", "0", "off", "no", "关闭", "停用"}
        return cls(
            enabled=bool(enabled),
            move_min_seconds=number("move_min_seconds", 0.3),
            move_max_seconds=max(number("move_max_seconds", 1.0), number("move_min_seconds", 0.3)),
            jitter_pixels=number("jitter_pixels", 1.0),
            click_pause_min_seconds=number("click_pause_min_seconds", 0.2),
            click_pause_max_seconds=max(
                number("click_pause_max_seconds", 0.6),
                number("click_pause_min_seconds", 0.2),
            ),
            safe_inset_ratio=min(0.45, number("safe_inset_ratio", 0.18)),
            fallback_x_min=int(number("fallback_x_min", 80)),
            fallback_x_max=int(number("fallback_x_max", 700)),
            fallback_y_min=int(number("fallback_y_min", 80)),
            fallback_y_max=int(number("fallback_y_max", 500)),
        )


class HumanInteraction:
    """使用 CDP 发送平滑鼠标轨迹，并在失败时保留安全回退。"""

    def __init__(self, config: dict[str, Any] | None = None):
        self.settings = HumanInteractionSettings.from_config(config)
        self._last_position: tuple[float, float] | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def move_to_element(self, tab: Any, element: Any) -> bool:
        """移动到元素内部的安全随机点；无法读取坐标时使用 DrissionPage 回退。"""
        point = self._safe_click_point(element)
        if point is None:
            try:
                from DrissionPage import Actions

                Actions(tab).move_to(element, duration=random.uniform(0.3, 0.8))
                return False
            except Exception:
                return self.move_to_fallback(tab)
        return self.move_to_point(tab, *point)

    def move_to_fallback(self, tab: Any) -> bool:
        """鼠标移动失败时按原有范围移动到随机坐标。"""
        x = random.randint(self.settings.fallback_x_min, self.settings.fallback_x_max)
        y = random.randint(self.settings.fallback_y_min, self.settings.fallback_y_max)
        return self.move_to_point(tab, x, y)

    def move_to_point(self, tab: Any, target_x: float, target_y: float) -> bool:
        """沿三次贝塞尔曲线移动，并用正弦函数控制加速和减速。"""
        if not self.enabled:
            self._last_position = (float(target_x), float(target_y))
            return True
        start = self._last_position or self._viewport_start(tab)
        end = (float(target_x), float(target_y))
        try:
            path = self._bezier_path(start, end, tab)
            duration = random.uniform(
                self.settings.move_min_seconds,
                self.settings.move_max_seconds,
            )
            interval = duration / max(1, len(path) - 1)
            for index, (x, y) in enumerate(path):
                tab.run_cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
                if index < len(path) - 1:
                    time.sleep(interval)
            self._last_position = end
            return True
        except Exception:
            try:
                from DrissionPage import Actions

                Actions(tab).move_to((end[0], end[1]), duration=random.uniform(0.2, 0.6))
                self._last_position = end
                return False
            except Exception:
                return False

    def click_element(self, tab: Any, element: Any) -> bool:
        """在元素安全区域内执行真实鼠标点击；关闭总开关时使用元素点击。"""
        if not self.enabled:
            element.click()
            return True
        point = self._safe_click_point(element)
        if point is None:
            element.click()
            return False
        self.move_to_point(tab, *point)
        time.sleep(
            random.uniform(
                self.settings.click_pause_min_seconds,
                self.settings.click_pause_max_seconds,
            )
        )
        tab.run_cdp(
            "Input.dispatchMouseEvent",
            type="mousePressed",
            x=point[0],
            y=point[1],
            button="left",
            buttons=1,
            clickCount=1,
        )
        time.sleep(random.uniform(0.04, 0.12))
        tab.run_cdp(
            "Input.dispatchMouseEvent",
            type="mouseReleased",
            x=point[0],
            y=point[1],
            button="left",
            buttons=0,
            clickCount=1,
        )
        return True

    def click_point(self, tab: Any, point: tuple[float, float]) -> bool:
        """在已经确认的坐标点击，不对坐标做偏移。"""
        x, y = float(point[0]), float(point[1])
        if self.enabled:
            self.move_to_point(tab, x, y)
            time.sleep(
                random.uniform(
                    self.settings.click_pause_min_seconds,
                    self.settings.click_pause_max_seconds,
                )
            )
        tab.run_cdp(
            "Input.dispatchMouseEvent",
            type="mousePressed",
            x=x,
            y=y,
            button="left",
            buttons=1,
            clickCount=1,
        )
        time.sleep(random.uniform(0.04, 0.12) if self.enabled else 0.01)
        tab.run_cdp(
            "Input.dispatchMouseEvent",
            type="mouseReleased",
            x=x,
            y=y,
            button="left",
            buttons=0,
            clickCount=1,
        )
        return True

    def _safe_click_point(self, element: Any) -> tuple[float, float] | None:
        try:
            rect = getattr(element, "rect", None)
            location = getattr(rect, "viewport_location", None)
            size = getattr(rect, "size", None)
            if location is None or size is None:
                return None
            left, top = float(location[0]), float(location[1])
            width, height = float(size[0]), float(size[1])
            if width <= 0 or height <= 0:
                return None
            inset_x = min(width / 2, max(1.0, width * self.settings.safe_inset_ratio))
            inset_y = min(height / 2, max(1.0, height * self.settings.safe_inset_ratio))
            x = random.uniform(left + inset_x, left + width - inset_x)
            y = random.uniform(top + inset_y, top + height - inset_y)
            return x, y
        except (TypeError, ValueError, IndexError, AttributeError):
            return None

    def _viewport_start(self, tab: Any) -> tuple[float, float]:
        try:
            size = tab.run_js("return [window.innerWidth, window.innerHeight];")
            width, height = float(size[0]), float(size[1])
            return random.uniform(0, max(1.0, width)), random.uniform(0, max(1.0, height))
        except Exception:
            return float(self.settings.fallback_x_min), float(self.settings.fallback_y_min)

    def _bezier_path(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        tab: Any,
    ) -> list[tuple[float, float]]:
        distance = math.hypot(end[0] - start[0], end[1] - start[1])
        steps = max(12, min(60, int(distance / 12) + 12))
        dx, dy = end[0] - start[0], end[1] - start[1]
        normal = (-dy, dx)
        normal_length = math.hypot(*normal) or 1.0
        bend = random.uniform(-0.18, 0.18) * max(distance, 20.0)
        bend_x, bend_y = normal[0] / normal_length * bend, normal[1] / normal_length * bend
        c1 = (start[0] + dx * random.uniform(0.25, 0.4) + bend_x, start[1] + dy * random.uniform(0.25, 0.4) + bend_y)
        c2 = (start[0] + dx * random.uniform(0.65, 0.85) + bend_x, start[1] + dy * random.uniform(0.65, 0.85) + bend_y)
        path: list[tuple[float, float]] = []
        for index in range(steps):
            t = index / (steps - 1)
            eased = (1.0 - math.cos(math.pi * t)) / 2.0
            x = self._cubic(start[0], c1[0], c2[0], end[0], eased)
            y = self._cubic(start[1], c1[1], c2[1], end[1], eased)
            jitter = self.settings.jitter_pixels * math.sin(math.pi * t)
            x += random.uniform(-jitter, jitter) if jitter else 0.0
            y += random.uniform(-jitter, jitter) if jitter else 0.0
            path.append((x, y))
        path[0] = start
        path[-1] = end
        return path

    @staticmethod
    def _cubic(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
        return ((1 - t) ** 3 * p0) + (3 * (1 - t) ** 2 * t * p1) + (3 * (1 - t) * t**2 * p2) + (t**3 * p3)
