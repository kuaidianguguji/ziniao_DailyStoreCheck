"""紫鸟 WebDriver 客户端与单店铺上下文。

官方示例支持通过本地 HTTP IPC 控制紫鸟。本文件把全局变量改成实例属性，
并提供 context manager，保证每个店铺处理结束后一定关闭 driver 和店铺。
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

import requests
from selenium import webdriver
from selenium.common import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

LOGGER = logging.getLogger(__name__)


# 紫鸟启动后的 IP 检测页、launcherPage 使用单独的页面导航超时，避免 Selenium 默认的
# 120 秒读取超时直接终止店铺。TikTok/Shopee/Mercado 后续页面由各自 DrissionPage 等待。
ZINIAO_NAVIGATION_TIMEOUT_SECONDS = 60

# launcherPage 导航超时后的有限重试次数。首次打开失败后最多再打开两次，避免无限卡住本轮任务。
LAUNCHER_PAGE_RETRY_TIMES = 2

# launcherPage 导航返回后，等待当前文档至少进入 interactive/complete 的最长时间。
LAUNCHER_PAGE_READY_TIMEOUT_SECONDS = 30

# launcherPage 成功后给紫鸟页面和插件留出的稳定时间。
LAUNCHER_PAGE_SETTLE_SECONDS = 6


class ZiniaoStoreCloseError(RuntimeError):
    """紫鸟店铺在重试和 IPC 恢复后仍无法关闭。"""


class ZiniaoClient:
    """紫鸟本地 IPC 接口封装。"""

    def __init__(self, config: dict[str, Any]):
        """读取紫鸟客户端、驱动目录、IPC 端口和企业登录信息。"""
        ziniao = config.get("ziniao", {})
        browser = ziniao.get("browser", {})
        user = ziniao.get("user_info", {})
        self.client_path = str(browser.get("client_path", ""))
        self.driver_folder = Path(browser.get("webdriver_path", "")) if browser.get("webdriver_path") else None
        self.socket_port = int(browser.get("socket_port", 16851))
        self.version = str(browser.get("version", "v6"))
        # 店铺执行期间定期发送合法的只读请求，防止部分 V6 客户端的 WebDriver IPC 长时间空闲后退出。
        self.ipc_keepalive_seconds = max(15, int(browser.get("ipc_keepalive_seconds", 60)))
        self.user_info = {"company": user.get("company", ""), "username": user.get("username", ""), "password": user.get("password", "")}
        self.system = platform.system()
        self._client_process: subprocess.Popen[Any] | None = None
        # 紫鸟本地 IPC 的单次控制请求串行发送；浏览器打开后的页面操作仍完全并行。
        self._ipc_request_lock = threading.RLock()

    @property
    def ipc_url(self) -> str:
        """返回紫鸟本地 HTTP IPC 地址。"""
        return f"http://127.0.0.1:{self.socket_port}"

    def send_http(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按官方示例方式序列化请求体并发送紫鸟 IPC 请求。"""
        with self._ipc_request_lock:
            response = requests.post(self.ipc_url, json.dumps(payload).encode("utf-8"), timeout=120)
            response.raise_for_status()
            return json.loads(response.text)

    def _probe_ipc(self, timeout_seconds: float = 15) -> bool:
        """用合法 HTTP 请求检查紫鸟 WebDriver IPC 是否已经可用。

        紫鸟没有提供独立的健康检查接口，因此沿用官方的
        ``getBrowserList``。这个接口首次执行时会完成登录、读取店铺列表和
        工作台握手，实际耗时可能超过 2 秒，所以探活必须给予足够时间。
        不能只建立 TCP 后立即断开：部分紫鸟 V6 版本会把这种空连接视为
        异常请求，影响本地 HTTP 服务稳定性。
        """
        with self._ipc_request_lock:
            try:
                payload = {"action": "getBrowserList", "requestId": str(uuid.uuid4()), **self.user_info}
                response = requests.post(
                    self.ipc_url,
                    json.dumps(payload).encode("utf-8"),
                    timeout=timeout_seconds,
                )
                result = json.loads(response.text)
                # 即使 statusCode 是登录错误，能够收到紫鸟标准 JSON 也说明 IPC
                # 已经启动；具体业务错误交给 update_core/list_browsers 清晰报告。
                return isinstance(result, dict) and result.get("statusCode") is not None
            except (requests.RequestException, json.JSONDecodeError, TypeError, ValueError):
                return False

    def start_client(self) -> None:
        """按官方演示启动紫鸟客户端；已启动时不重复启动。

        官方示例启动后固定等待 5 秒，再由 ``update_core`` 循环请求确认
        IPC 和内核状态。启动阶段不再额外调用 ``getBrowserList``，避免在
        紫鸟工作台握手尚未完成时重复触发列表读取。
        """
        if self._probe_ipc():
            LOGGER.info("紫鸟客户端已经在端口 %s 运行，直接复用", self.socket_port)
            return
        if not self.client_path:
            raise RuntimeError("未配置 ziniao.browser.client_path")
        if self.system == "Windows":
            command = [self.client_path, "--run_type=web_driver", "--ipc_type=http", f"--port={self.socket_port}"]
        elif self.system == "Darwin":
            command = ["open", "-a", self.client_path, "--args", "--run_type=web_driver", "--ipc_type=http", f"--port={self.socket_port}"]
        elif self.system == "Linux":
            command = [self.client_path, "--no-sandbox", "--run_type=web_driver", "--ipc_type=http", f"--port={self.socket_port}"]
        else:
            raise RuntimeError(f"不支持的操作系统: {self.system}")
        try:
            self._client_process = subprocess.Popen(command)
        except OSError as exc:
            raise RuntimeError(f"启动紫鸟客户端失败: {exc}") from exc

        # 与官方 ziniao_webdriver_demo.py 保持一致：启动客户端后等待 5 秒。
        # 后续 update_core() 会在 IPC 暂时不可用时按官方方式循环重试，
        # 因此这里不主动发送任何业务请求，也不产生第二个客户端进程。
        time.sleep(5)
        LOGGER.info(
            "紫鸟 WebDriver 客户端启动命令已执行，已按官方流程等待 5 秒，端口=%s，启动进程PID=%s",
            self.socket_port,
            self._client_process.pid if self._client_process else "未知",
        )

    def recover_ipc(self, timeout_seconds: float = 30) -> bool:
        """互斥执行 IPC 恢复，避免一个店铺恢复客户端时其他线程同时发送控制命令。"""
        with self._ipc_request_lock:
            return self._recover_ipc_locked(timeout_seconds)

    def _recover_ipc_locked(self, timeout_seconds: float = 30) -> bool:
        """IPC 失联后重新启动客户端，并确认官方 updateCore 已经可以正常执行。"""
        if self._probe_ipc(timeout_seconds=3):
            LOGGER.info("[紫鸟][IPC恢复] 端口=%s 已经可用，继续验证 updateCore", self.socket_port)
            try:
                self.update_core()
                return True
            except Exception:
                LOGGER.error("[紫鸟][IPC恢复] 端口可连接但 updateCore 失败: %s", traceback.format_exc())
                return False

        try:
            self.start_client()
        except Exception:
            LOGGER.error("[紫鸟][IPC恢复] 启动客户端失败: %s", traceback.format_exc())
            return False

        started_at = time.monotonic()
        deadline = started_at + timeout_seconds
        check_count = 0
        while time.monotonic() < deadline:
            check_count += 1
            if self._probe_ipc(timeout_seconds=3):
                try:
                    self.update_core()
                    LOGGER.info(
                        "[紫鸟][IPC恢复成功] 端口=%s，第 %s 次检查及 updateCore 均成功，耗时=%.2f秒",
                        self.socket_port,
                        check_count,
                        time.monotonic() - started_at,
                    )
                    return True
                except Exception:
                    LOGGER.error("[紫鸟][IPC恢复] IPC 已出现但 updateCore 失败: %s", traceback.format_exc())
                    return False
            LOGGER.warning("[紫鸟][IPC恢复等待] 端口=%s，第 %s 次检查仍不可用", self.socket_port, check_count)
            time.sleep(2)

        LOGGER.error("[紫鸟][IPC恢复超时] 等待 %.1f 秒后端口 %s 仍不可用", timeout_seconds, self.socket_port)
        return False

    def ipc_keepalive(self) -> bool:
        """发送只读 getBrowserList 请求，为长时间爬虫任务维持并检查 IPC。"""
        return self._probe_ipc(timeout_seconds=10)

    @staticmethod
    def _browser_debug_endpoint_alive(debugging_port: int | str | None, timeout_seconds: float = 2) -> bool:
        """通过 Chromium 调试接口判断对应店铺浏览器进程是否仍然存活。"""
        if not debugging_port:
            return False
        try:
            response = requests.get(f"http://127.0.0.1:{int(debugging_port)}/json/version", timeout=timeout_seconds)
            return response.ok and bool(response.text.strip())
        except (requests.RequestException, TypeError, ValueError):
            return False

    def confirm_browser_closed(
        self,
        debugging_port: int | str | None,
        checks: int = 3,
        interval_seconds: float = 1,
    ) -> bool:
        """连续检查调试端口；只有三次均不可用，才确认店铺浏览器已经关闭。"""
        if not debugging_port:
            LOGGER.warning("[紫鸟][浏览器关闭确认跳过] startBrowser 未返回 debuggingPort，不能通过端口确认")
            return False

        for check_index in range(1, checks + 1):
            if self._browser_debug_endpoint_alive(debugging_port):
                LOGGER.warning(
                    "[紫鸟][浏览器仍存活] debuggingPort=%s，第 %s/%s 次检查仍可访问",
                    debugging_port,
                    check_index,
                    checks,
                )
                return False
            LOGGER.info(
                "[紫鸟][浏览器关闭检查] debuggingPort=%s，第 %s/%s 次检查不可用",
                debugging_port,
                check_index,
                checks,
            )
            if check_index < checks:
                time.sleep(interval_seconds)
        LOGGER.info("[紫鸟][浏览器关闭确认成功] debuggingPort=%s 连续 %s 次不可用", debugging_port, checks)
        return True

    def update_core(self) -> None:
        """等待紫鸟内核更新完成。"""
        payload = {"action": "updateCore", "requestId": str(uuid.uuid4()), **self.user_info}
        for attempt in range(1, 61):
            try:
                result = self.send_http(payload)
            except requests.RequestException as exc:
                LOGGER.warning("[紫鸟][updateCore] IPC 暂不可用，第 %s/60 次，异常=%s", attempt, exc)
                time.sleep(2)
                continue
            code = result.get("statusCode")
            if code == 0:
                LOGGER.info("[紫鸟][updateCore] 内核更新完成，第 %s 次请求成功", attempt)
                return
            if code == -10003:
                raise RuntimeError(f"紫鸟登录或版本不支持: {result}")
            LOGGER.info("[紫鸟][updateCore] 尚未完成，第 %s/60 次，statusCode=%s", attempt, code)
            time.sleep(2)
        raise TimeoutError("等待紫鸟内核更新超时")

    def list_browsers(self) -> list[dict[str, Any]]:
        """读取紫鸟店铺列表。"""
        result = self.send_http({"action": "getBrowserList", "requestId": str(uuid.uuid4()), **self.user_info})
        if str(result.get("statusCode")) != "0":
            raise RuntimeError(f"读取紫鸟店铺失败: {result}")
        browser_list = result.get("browserList", [])
        LOGGER.info("[紫鸟][getBrowserList] 店铺列表读取成功，店铺数=%s", len(browser_list))
        return browser_list

    def open_store(
        self,
        store_info: str,
        isWebDriverReadOnlyMode: int = 0,
        isprivacy: int = 0,
        isHeadless: int = 0,
        cookieTypeSave: int = 0,
        jsInfo: Any = "",
    ) -> dict[str, Any]:
        """严格按官方 ``open_store`` 参数调用 ``startBrowser`` 打开店铺。"""
        store_info = str(store_info)
        payload: dict[str, Any] = {
            "action": "startBrowser",
            "isWaitPluginUpdate": 0,
            "isHeadless": isHeadless,
            "requestId": str(uuid.uuid4()),
            "isWebDriverReadOnlyMode": isWebDriverReadOnlyMode,
            "cookieTypeLoad": 0,
            "cookieTypeSave": cookieTypeSave,
            "runMode": "1",
            "isLoadUserPlugin": False,
            "pluginIdType": 1,
            "privacyMode": isprivacy,
            "notPromptForDownload": 1,
            **self.user_info,
        }
        if store_info.isdigit():
            payload["browserId"] = store_info
        else:
            payload["browserOauth"] = store_info
        if len(str(jsInfo)) > 2:
            payload["injectJsInfo"] = json.dumps(jsInfo)
        result = self.send_http(payload)
        status_code = str(result.get("statusCode"))
        LOGGER.info("[紫鸟][startBrowser] 店铺标识=%s，返回 statusCode=%s", store_info, status_code)
        if status_code == "0":
            return result
        if status_code == "-10003":
            raise RuntimeError(f"紫鸟登录失败或当前版本不支持 startBrowser: {json.dumps(result, ensure_ascii=False)}")
        raise RuntimeError(f"紫鸟 startBrowser 打开店铺失败: {json.dumps(result, ensure_ascii=False)}")

    def close_store(self, browser_oauth: str, debugging_port: int | str | None = None) -> None:
        """优先用官方 stopBrowser 关闭；IPC 失联时结合调试端口确认并恢复。"""
        last_error: Exception | None = None
        for attempt in range(3):
            if attempt > 0:
                wait_seconds = 2 * attempt
                LOGGER.warning("紫鸟关闭店铺重试，第 %s/3 次尝试前等待 %s 秒", attempt + 1, wait_seconds)
                time.sleep(wait_seconds)
            try:
                result = self.send_http(
                    {
                        "action": "stopBrowser",
                        "requestId": str(uuid.uuid4()),
                        "duplicate": 0,
                        "browserOauth": browser_oauth,
                        **self.user_info,
                    }
                )
                if str(result.get("statusCode")) == "0":
                    LOGGER.info("紫鸟 stopBrowser 成功: browserOauth=%s", browser_oauth)
                    return
                last_error = RuntimeError(f"关闭紫鸟店铺失败: {result}")
                LOGGER.error("紫鸟 stopBrowser 返回失败，第 %s/3 次，response=%s", attempt + 1, result)
            except requests.RequestException as exc:
                last_error = exc
                LOGGER.warning("紫鸟 IPC 连接失败，第 %s/3 次，url=%s，异常=%s", attempt + 1, self.ipc_url, exc)

                # IPC 可能随店铺浏览器一起退出。只有调试端口连续三次不可用，才把它视为“浏览器已经关闭”。
                if self.confirm_browser_closed(debugging_port):
                    if self.recover_ipc():
                        LOGGER.warning(
                            "[紫鸟][关闭兜底成功] stopBrowser 请求前 IPC 已失联，但 debuggingPort=%s 已确认关闭且 IPC 已恢复",
                            debugging_port,
                        )
                        return
                    last_error = RuntimeError("店铺浏览器已关闭，但紫鸟 IPC 恢复失败，不能继续打开下一店铺")
                    break

                if attempt == 0:
                    LOGGER.warning("紫鸟 IPC 已失联，店铺浏览器仍存活；尝试恢复 IPC 后继续发送 stopBrowser")
                    if not self.recover_ipc():
                        LOGGER.error("重新启动紫鸟 WebDriver 客户端失败，将继续后续关闭重试")

        raise ZiniaoStoreCloseError(f"紫鸟店铺连续 3 次关闭失败，browserOauth={browser_oauth}: {last_error}") from last_error

    def get_driver(self, opened: dict[str, Any]) -> webdriver.Chrome:
        """根据紫鸟返回的内核信息找到 webdriver 并连接调试端口。"""
        browser_path = str(opened.get("browserPath") or "")
        if browser_path.lower().endswith(("superbrowser.exe", "superbrowser")):
            browser_path = os.path.dirname(browser_path)
        candidate = Path(browser_path) / ("webdriver.exe" if self.system == "Windows" else "webdriver") if browser_path else None
        if not candidate or not candidate.exists():
            core_type = opened.get("core_type")
            core_version = str(opened.get("core_version", ""))
            if self.driver_folder and core_version and (core_type in (None, 0, "Chromium", "")):
                suffix = ".exe" if self.system == "Windows" else ""
                candidate = self.driver_folder / f"chromedriver{core_version.split('.')[0]}{suffix}"
        if not candidate or not candidate.exists():
            raise FileNotFoundError(f"找不到紫鸟 webdriver: {candidate}")
        options = webdriver.ChromeOptions()
        options.add_argument("--log-level=3")
        # 紫鸟启动页常包含长期运行的第三方脚本。使用 eager 让 driver.get() 在 DOM
        # 基本可用时返回，避免等待所有广告/统计资源导致整个导航卡满 120 秒。
        options.page_load_strategy = "eager"
        options.add_experimental_option("debuggerAddress", f"127.0.0.1:{opened.get('debuggingPort')}")
        driver = webdriver.Chrome(service=Service(str(candidate)), options=options)
        driver.set_page_load_timeout(ZINIAO_NAVIGATION_TIMEOUT_SECONDS)
        LOGGER.info(
            "[紫鸟][WebDriver连接] debuggingPort=%s，webdriver=%s，page_load_strategy=eager，"
            "page_load_timeout=%s秒",
            opened.get("debuggingPort"),
            candidate,
            ZINIAO_NAVIGATION_TIMEOUT_SECONDS,
        )
        return driver

    @staticmethod
    def open_ip_check(driver: webdriver.Chrome, ip_check_url: str) -> bool:
        """按官方示例打开紫鸟 IP 检测页并检查成功按钮。"""
        try:
            LOGGER.info("[紫鸟][IP检测页] 开始打开 url=%s", ip_check_url)
            driver.get(ip_check_url)
            driver.find_element(By.XPATH, '//button[contains(@class, "styles_btn--success")]')
            LOGGER.info("[紫鸟][IP检测页] 成功找到检测通过按钮")
            return True
        except NoSuchElementException:
            LOGGER.error("紫鸟 IP 检测页未找到成功元素")
            return False
        except Exception:
            LOGGER.error("紫鸟 IP 检测异常: %s", traceback.format_exc())
            return False

    @staticmethod
    def open_launcher_page(driver: webdriver.Chrome, launcher_page: str) -> None:
        """打开紫鸟返回的平台主页；导航超时后检查状态并进行有限重试。"""
        if not launcher_page:
            raise RuntimeError("紫鸟 startBrowser 没有返回 launcherPage")

        last_error: Exception | None = None
        max_attempts = LAUNCHER_PAGE_RETRY_TIMES + 1
        for attempt in range(max_attempts):
            attempt_number = attempt + 1
            try:
                LOGGER.info(
                    "[紫鸟][launcherPage] 开始导航，第 %s/%s 次，url=%s，超时=%s秒",
                    attempt_number,
                    max_attempts,
                    launcher_page,
                    ZINIAO_NAVIGATION_TIMEOUT_SECONDS,
                )
                driver.set_page_load_timeout(ZINIAO_NAVIGATION_TIMEOUT_SECONDS)
                driver.get(launcher_page)
                if ZiniaoClient._wait_driver_document_ready(driver, LAUNCHER_PAGE_READY_TIMEOUT_SECONDS):
                    LOGGER.info(
                        "[紫鸟][launcherPage] 导航成功，第 %s/%s 次，current_url=%s，readyState 已就绪",
                        attempt_number,
                        max_attempts,
                        ZiniaoClient._driver_current_url(driver),
                    )
                    time.sleep(LAUNCHER_PAGE_SETTLE_SECONDS)
                    return
                raise TimeoutException(
                    f"document.readyState 在 {LAUNCHER_PAGE_READY_TIMEOUT_SECONDS} 秒内未进入 interactive/complete"
                )
            except Exception as exc:
                # Selenium 的导航超时有时会直接透出 urllib3.exceptions.ReadTimeoutError，
                # 不一定继承 selenium.common.TimeoutException；这里统一按导航异常处理，
                # 先检查页面状态，再进入有限重试。
                last_error = exc
                current_url = ZiniaoClient._driver_current_url(driver)
                ready_state = ZiniaoClient._driver_ready_state(driver)
                LOGGER.warning(
                    "[紫鸟][launcherPage] 导航异常，第 %s/%s 次，异常=%s，current_url=%s，readyState=%s",
                    attempt_number,
                    max_attempts,
                    exc,
                    current_url or "<空>",
                    ready_state or "<未知>",
                )

                # driver.get() 超时不一定代表页面没有打开；若当前 URL 已经离开空白页，且
                # document 已进入 interactive/complete，则页面可交给后续 DrissionPage 继续处理。
                if current_url and not current_url.startswith(("about:blank", "chrome://newtab")):
                    if ready_state in {"interactive", "complete"}:
                        LOGGER.warning(
                            "[紫鸟][launcherPage] 虽然导航抛出超时，但页面已可用，继续执行，current_url=%s",
                            current_url,
                        )
                        time.sleep(LAUNCHER_PAGE_SETTLE_SECONDS)
                        return

                if attempt < max_attempts - 1:
                    wait_seconds = 5 * (attempt + 1)
                    LOGGER.warning(
                        "[紫鸟][launcherPage] 第 %s 次失败后等待 %s 秒再重试",
                        attempt_number,
                        wait_seconds,
                    )
                    time.sleep(wait_seconds)

        raise RuntimeError(
            f"紫鸟 launcherPage 连续 {max_attempts} 次导航失败，最后错误={last_error}，"
            f"current_url={ZiniaoClient._driver_current_url(driver) or '<空>'}"
        ) from last_error

    @staticmethod
    def _driver_current_url(driver: webdriver.Chrome) -> str:
        """安全读取 WebDriver 当前 URL，供导航超时后的诊断和恢复判断。"""
        try:
            return str(driver.current_url or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _driver_ready_state(driver: webdriver.Chrome) -> str:
        """安全读取页面 readyState；读取失败返回空字符串。"""
        try:
            return str(driver.execute_script("return document.readyState;") or "").strip().lower()
        except Exception:
            return ""

    @staticmethod
    def _wait_driver_document_ready(driver: webdriver.Chrome, timeout_seconds: float) -> bool:
        """等待 WebDriver 页面进入 interactive 或 complete。"""
        deadline = time.monotonic() + timeout_seconds
        last_state = ""
        while time.monotonic() < deadline:
            state = ZiniaoClient._driver_ready_state(driver)
            if state != last_state:
                LOGGER.info("[紫鸟][页面状态] launcherPage document.readyState=%s", state or "<空>")
                last_state = state
            if state in {"interactive", "complete"}:
                return True
            time.sleep(1)
        return False

    def exit_client(self) -> None:
        """任务全部结束后通知紫鸟客户端退出。"""
        try:
            # 官方示例在任务结束后直接发送 exit。这里不再先调用
            # getBrowserList 探活，避免退出阶段额外触发登录或列表读取。
            self.send_http({"action": "exit", "requestId": str(uuid.uuid4()), **self.user_info})
            LOGGER.info("紫鸟客户端 exit 请求发送成功")
        except requests.RequestException as exc:
            # IPC 已经随店铺浏览器退出时，连接被拒绝等同于客户端已关闭，
            # 无需输出整段 urllib3 堆栈干扰排错。
            LOGGER.info("紫鸟 IPC 已不可用，客户端无需重复退出: %s", exc)
        except Exception:
            LOGGER.warning("退出紫鸟客户端时发生异常: %s", traceback.format_exc())


class ZiniaoStoreSession:
    """单店铺会话；退出时先由紫鸟 stopBrowser 关闭店铺，再清理 driver。"""

    def __init__(self, client: ZiniaoClient, identifier: str, store_name: str):
        """记录客户端、店铺标识和用于日志展示的店铺名。"""
        self.client = client
        self.identifier = identifier
        self.store_name = store_name
        self.opened: dict[str, Any] = {}
        self.driver: webdriver.Chrome | None = None
        self.browser_oauth = identifier
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    def __enter__(self) -> "ZiniaoStoreSession":
        LOGGER.info("打开店铺: %s", self.store_name)
        self.opened = self.client.open_store(self.identifier)
        self.browser_oauth = str(self.opened.get("browserOauth") or self.opened.get("browserId") or self.identifier)
        try:
            self.driver = self.client.get_driver(self.opened)
            self.driver.implicitly_wait(60)
            ip_check_url = str(self.opened.get("ipDetectionPage") or "")
            if not ip_check_url:
                raise RuntimeError("紫鸟 startBrowser 没有返回 ipDetectionPage，请升级紫鸟客户端")
            if not self.client.open_ip_check(self.driver, ip_check_url):
                raise RuntimeError("紫鸟 IP 检测未通过")
            self.client.open_launcher_page(self.driver, str(self.opened.get("launcherPage") or ""))
            self._start_ipc_heartbeat()
            return self
        except Exception:
            # __enter__ 内抛错时 Python 不会调用 __exit__，这里必须主动关闭店铺。
            try:
                self.client.close_store(self.browser_oauth, self.opened.get("debuggingPort"))
            except Exception:
                LOGGER.error("初始化 driver 失败后关闭店铺 %s 也失败: %s", self.store_name, traceback.format_exc())
            raise

    @property
    def download_path(self) -> str:
        """返回紫鸟为当前店铺分配的下载目录。"""
        return str(self.opened.get("downloadPath") or "")

    def _start_ipc_heartbeat(self) -> None:
        """启动单店铺 IPC 心跳，爬虫长时间运行时每隔固定时间检查一次。"""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_stop.clear()

        def heartbeat_loop() -> None:
            heartbeat_index = 0
            while not self._heartbeat_stop.wait(self.client.ipc_keepalive_seconds):
                heartbeat_index += 1
                if self.client.ipc_keepalive():
                    LOGGER.info(
                        "[紫鸟][IPC心跳成功] 店铺=%s，第 %s 次，间隔=%s秒",
                        self.store_name,
                        heartbeat_index,
                        self.client.ipc_keepalive_seconds,
                    )
                else:
                    # 爬虫过程中不强行重启客户端，避免影响仍在运行的店铺浏览器；退出会话时再执行关闭兜底。
                    LOGGER.error(
                        "[紫鸟][IPC心跳失败] 店铺=%s，第 %s 次；浏览器继续采集，结束时将执行端口确认和 IPC 恢复",
                        self.store_name,
                        heartbeat_index,
                    )

        self._heartbeat_thread = threading.Thread(
            target=heartbeat_loop,
            name=f"ziniao-ipc-heartbeat-{self.store_name}",
            daemon=True,
        )
        self._heartbeat_thread.start()
        LOGGER.info(
            "[紫鸟][IPC心跳启动] 店铺=%s，间隔=%s秒",
            self.store_name,
            self.client.ipc_keepalive_seconds,
        )

    def _stop_ipc_heartbeat(self) -> None:
        """关闭前停止心跳，避免心跳请求与 stopBrowser 同时访问 IPC。"""
        self._heartbeat_stop.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=12)
        if self._heartbeat_thread:
            LOGGER.info(
                "[紫鸟][IPC心跳停止] 店铺=%s，线程是否仍存活=%s",
                self.store_name,
                self._heartbeat_thread.is_alive(),
            )

    def __exit__(self, exc_type, exc_value, traceback_value) -> bool:
        """无论爬虫成功或失败，均关闭浏览器和紫鸟店铺。"""
        close_error: Exception | None = None
        driver_error: Exception | None = None
        debugging_port = self.opened.get("debuggingPort")

        self._stop_ipc_heartbeat()

        # 当前紫鸟 V6 环境中，先 driver.quit() 可能导致 16851 IPC 一并失联。
        # 因此先调用官方 stopBrowser，确认店铺关闭后再释放本地 Selenium 会话。
        try:
            self.client.close_store(self.browser_oauth, debugging_port)
        except Exception as exc:
            close_error = exc
        finally:
            try:
                if self.driver:
                    self.driver.quit()
            except Exception as exc:
                driver_error = exc
                if close_error:
                    LOGGER.warning("stopBrowser 和 WebDriver quit 均失败，店铺=%s，driver异常=%s", self.store_name, traceback.format_exc())
                else:
                    # stopBrowser 已关闭浏览器后，driver.quit() 可能因连接已断开而失败，可安全忽略。
                    LOGGER.info("店铺 %s 已由 stopBrowser 关闭，WebDriver 会话已随浏览器断开: %s", self.store_name, exc)

        if close_error and self.client.confirm_browser_closed(debugging_port):
            # 本次日志中的实际路径：stopBrowser 因 IPC 失联失败，但随后 driver.quit() 已关闭浏览器。
            # 此时重新拉起 IPC；浏览器关闭和 IPC 恢复都确认成功后，才允许继续下一店铺。
            LOGGER.warning(
                "[紫鸟][关闭二次确认] 店铺=%s，stopBrowser 未成功返回，但 driver.quit() 后浏览器已确认关闭，开始恢复 IPC",
                self.store_name,
            )
            if self.client.recover_ipc():
                LOGGER.warning(
                    "[紫鸟][关闭二次确认成功] 店铺=%s，浏览器已关闭且 IPC 已恢复，允许继续下一店铺",
                    self.store_name,
                )
                close_error = None
            else:
                close_error = ZiniaoStoreCloseError(
                    f"店铺 {self.store_name} 浏览器已经关闭，但紫鸟 IPC 恢复失败，不能继续下一店铺"
                )

        if close_error:
            LOGGER.critical("关闭店铺 %s 失败，禁止继续打开下一店铺: %s", self.store_name, close_error)
            if isinstance(close_error, ZiniaoStoreCloseError):
                raise close_error
            raise ZiniaoStoreCloseError(f"关闭店铺 {self.store_name} 失败: {close_error}") from close_error

        LOGGER.info("关闭店铺成功: %s", self.store_name)
        if driver_error:
            LOGGER.debug("店铺 %s 的 WebDriver 清理异常已忽略: %s", self.store_name, driver_error)
        return False
