"""飞书开放平台客户端。

这里集中管理租户 token、控制台多维表、三张数据多维表、三张历史电子表和机器人推送。
没有填写凭据时客户端进入 dry-run，便于先开发和测试紫鸟/爬虫，不会发出网络请求。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import requests

from .config import StoreTask, is_enabled_switch, normalise_platform

LOGGER = logging.getLogger(__name__)

# 单张飞书 Markdown 卡片使用保守的字符上限。DeepSeek 的全店铺分析可能超过一万字，
# 拆分后按顺序发送，避免整个 interactive 请求因内容过长返回 HTTP 400。
MARKDOWN_CARD_MAX_CHARS = 4000

# 连续发送多张分片卡片时保留短暂间隔，降低触发飞书消息频率限制的概率。
MARKDOWN_CARD_SEND_INTERVAL_SECONDS = 0.5


class FeishuClient:
    """飞书接口的薄封装，业务层不直接拼接 URL 或处理 token。"""

    def __init__(self, config: dict[str, Any]):
        """读取飞书节点并创建可复用的 HTTP Session。"""
        self.config = config.get("feishu", {})
        self.base_url = str(self.config.get("base_url", "https://open.feishu.cn")).rstrip("/")
        self.timeout = int(config.get("data", {}).get("request_timeout_seconds", 30))
        self._tenant_token = ""
        # 同一轮任务中每张表的字段结构只读取一次，避免每个店铺重复请求字段接口。
        self._table_field_names_cache: dict[tuple[str, str], set[str]] = {}
        self.session = requests.Session()
        # requests.Session、tenant token 和字段缓存由所有店铺线程共享；
        # 仅串行化短暂的飞书 HTTP 请求，不影响多个浏览器并行采集。
        self._request_lock = threading.RLock()

    @property
    def configured(self) -> bool:
        """判断应用凭据是否齐全；具体表的 token/id 在各接口中单独校验。"""
        return bool(self.config.get("app_id") and self.config.get("app_secret"))

    def get_bitable_ref(self, kind: str, platform: str = "") -> tuple[str, str]:
        """返回指定多维表的 ``(app_token, table_id)``，并兼容旧版配置。"""
        bitable = self.config.get("bitable", {})
        default_token = str(bitable.get("default_app_token") or bitable.get("app_token") or "")
        if kind == "control":
            value = bitable.get("control", {})
            if isinstance(value, dict):
                return str(value.get("app_token") or default_token), str(value.get("table_id") or bitable.get("control_table_id") or "")
            return default_token, str(value or bitable.get("control_table_id") or "")
        if kind == "data" and platform:
            value = bitable.get("data_tables", {}).get(platform, {})
            if isinstance(value, dict):
                return str(value.get("app_token") or default_token), str(value.get("table_id") or "")
            return default_token, str(value or "")
        raise ValueError(f"未知多维表类型: {kind}")

    def get_summary_recipients(self) -> dict[str, str]:
        """读取最终汇总接收人字典，返回“姓名 -> 接收 ID”的有效配置。"""
        robot = self.config.get("robot", {})
        raw_recipients = robot.get("summary_recipients", {})
        if raw_recipients in (None, ""):
            return {}
        if not isinstance(raw_recipients, dict):
            LOGGER.error("[飞书][汇总接收人配置错误] summary_recipients 必须是字典，当前类型=%s", type(raw_recipients).__name__)
            return {}

        receive_id_type = str(robot.get("receive_id_type") or "open_id").strip()
        recipients: dict[str, str] = {}
        for raw_name, raw_receive_id in raw_recipients.items():
            name = str(raw_name or "").strip()
            receive_id = str(raw_receive_id or "").strip()
            if not name or not receive_id:
                LOGGER.warning("[飞书][汇总接收人跳过] 姓名或 %s 为空：name=%r", receive_id_type, name)
                continue
            if receive_id_type == "open_id" and not receive_id.startswith("ou_"):
                LOGGER.warning("[飞书][汇总接收人跳过] 姓名=%s 的值不是 open_id（应以 ou_ 开头）", name)
                continue
            recipients[name] = receive_id
        return recipients

    def list_table_field_names(self, table_id: str, app_token: str = "") -> set[str]:
        """读取多维表的真实字段名称并缓存，用于写入前校验 FieldNameNotFound。"""
        if not self.configured or not app_token or not table_id:
            LOGGER.warning("[飞书][字段列表] app_token/table_id 或应用凭据不完整，无法读取真实字段")
            return set()

        cache_key = (app_token, table_id)
        if cache_key in self._table_field_names_cache:
            return set(self._table_field_names_cache[cache_key])

        field_names: set[str] = set()
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            payload = self._request(
                "GET",
                f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                headers=self._headers(),
                params=params,
            )
            data = payload.get("data", {})
            for item in data.get("items", []):
                field_name = str(item.get("field_name") or "")
                if field_name:
                    field_names.add(field_name)
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
            if not page_token:
                break

        self._table_field_names_cache[cache_key] = set(field_names)
        LOGGER.info("[飞书][字段列表] table_id=%s，读取真实字段数=%s，字段=%s", table_id, len(field_names), sorted(field_names))
        return field_names

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """通过共享可重入锁发送飞书请求，保证并发店铺使用同一 Session 时安全。"""
        with self._request_lock:
            return self._request_locked(method, path, **kwargs)

    def _request_locked(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """统一发送请求并检查飞书 code，避免每个接口重复写错误处理。"""
        safe_path = self._safe_path_for_log(path)
        request_log = {
            "params": kwargs.get("params", {}),
            "json": kwargs.get("json", {}),
        }
        LOGGER.info(
            "[飞书][HTTP请求] method=%s，path=%s，request=%s",
            method.upper(),
            safe_path,
            self._json_for_log(request_log),
        )
        try:
            response = self.session.request(method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs)
        except requests.RequestException:
            LOGGER.exception("[飞书][HTTP异常] method=%s，path=%s", method.upper(), safe_path)
            raise
        if not response.ok:
            # 飞书在 HTTP 400 响应中仍会返回可定位问题的 code/msg。旧逻辑先调用
            # raise_for_status()，导致这些信息没有写入日志，只能看到笼统的 Bad Request。
            response_text = response.text[:4000]
            response_code: Any = ""
            response_message = ""
            try:
                error_payload = response.json()
                if isinstance(error_payload, dict):
                    response_code = error_payload.get("code", "")
                    response_message = str(error_payload.get("msg") or "")
                    response_text = self._json_for_log(error_payload)
            except ValueError:
                pass
            LOGGER.error(
                "[飞书][HTTP失败] method=%s，path=%s，status=%s，code=%s，msg=%s，response=%s，请求JSON=%s",
                method.upper(),
                safe_path,
                response.status_code,
                response_code or "<无>",
                response_message or "<无>",
                response_text,
                self._json_for_log(kwargs.get("json", {})),
            )
            raise RuntimeError(
                f"飞书HTTP失败 status={response.status_code} "
                f"code={response_code or '<无>'} msg={response_message or response_text[:500]}"
            )
        try:
            payload = response.json()
        except ValueError:
            LOGGER.error(
                "[飞书][响应解析失败] method=%s，path=%s，status=%s，response_text=%r",
                method.upper(),
                safe_path,
                response.status_code,
                response.text[:2000],
            )
            raise
        if payload.get("code", 0) != 0:
            LOGGER.error(
                "[飞书][业务失败] method=%s，path=%s，code=%s，msg=%s，完整响应=%s，请求JSON=%s",
                method.upper(),
                safe_path,
                payload.get("code"),
                payload.get("msg"),
                self._json_for_log(payload),
                self._json_for_log(kwargs.get("json", {})),
            )
            raise RuntimeError(f"飞书接口失败 code={payload.get('code')} msg={payload.get('msg')}")
        response_summary: dict[str, Any] = {"code": payload.get("code", 0), "msg": payload.get("msg", "")}
        data = payload.get("data")
        if isinstance(data, dict):
            if isinstance(data.get("items"), list):
                response_summary["items_count"] = len(data["items"])
            if isinstance(data.get("records"), list):
                response_summary["records_count"] = len(data["records"])
            if "has_more" in data:
                response_summary["has_more"] = data.get("has_more")
        LOGGER.info("[飞书][HTTP成功] method=%s，path=%s，response=%s", method.upper(), safe_path, self._json_for_log(response_summary))
        return payload

    @classmethod
    def _safe_log_value(cls, value: Any, parent_key: str = "") -> Any:
        """递归脱敏日志中的密钥、访问令牌和签名，同时保留业务字段内容。"""
        sensitive_keys = {
            "app_secret",
            "tenant_access_token",
            "authorization",
            "sign",
            "sign_secret",
            "webhook_url",
        }
        if parent_key.lower() in sensitive_keys:
            return "***"
        if isinstance(value, dict):
            return {key: cls._safe_log_value(item, str(key)) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._safe_log_value(item, parent_key) for item in value]
        return value

    @classmethod
    def _json_for_log(cls, value: Any) -> str:
        """把请求或响应转换成单行中文 JSON，遇到非标准对象时使用字符串表示。"""
        return json.dumps(cls._safe_log_value(value), ensure_ascii=False, default=str)

    @staticmethod
    def _safe_path_for_log(path: str) -> str:
        """隐藏 URL 路径中的多维表 app_token 和电子表 spreadsheet_token。"""
        safe_path = re.sub(r"(/bitable/v1/apps/)[^/]+", r"\1***", path)
        return re.sub(r"(/spreadsheets/)[^/]+", r"\1***", safe_path)

    def _get_tenant_token(self) -> str:
        """按需获取 tenant_access_token，并在进程内复用。"""
        with self._request_lock:
            if self._tenant_token:
                return self._tenant_token
            payload = self._request(
                "POST",
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.config.get("app_id"), "app_secret": self.config.get("app_secret")},
            )
            self._tenant_token = payload.get("tenant_access_token", "")
            if not self._tenant_token:
                raise RuntimeError("飞书没有返回 tenant_access_token")
            return self._tenant_token

    def _headers(self) -> dict[str, str]:
        """生成带租户 token 的飞书标准请求头。"""
        return {"Authorization": f"Bearer {self._get_tenant_token()}", "Content-Type": "application/json; charset=utf-8"}

    @staticmethod
    def _field_value(value: Any) -> Any:
        """把飞书字段可能返回的数组、人员对象、文本对象转成普通值。"""
        if isinstance(value, list):
            return ", ".join(str(FeishuClient._field_value(item)) for item in value)
        if isinstance(value, dict):
            return value.get("name") or value.get("text") or value.get("open_id") or value.get("id") or json.dumps(value, ensure_ascii=False)
        return value

    def _recipient_value(self, value: Any) -> str:
        """从人员字段提取接收 ID；仅有显示姓名时不冒充 open_id。"""
        if isinstance(value, list):
            values = [self._recipient_value(item) for item in value]
            return ",".join(item for item in values if item)
        if isinstance(value, dict):
            receive_id_type = self.config.get("robot", {}).get("receive_id_type", "open_id")
            keys_by_type = {
                "open_id": ("open_id", "id"),
                "user_id": ("user_id", "id"),
                "union_id": ("union_id", "id"),
                "email": ("email",),
            }
            for key in keys_by_type.get(receive_id_type, (receive_id_type, "id")):
                if value.get(key):
                    return str(value[key])
            LOGGER.warning("推送人员字段只有姓名、没有可用用户 ID: %s", value.get("name", ""))
            return ""
        text = str(value or "").strip()
        receive_id_type = self.config.get("robot", {}).get("receive_id_type", "open_id")
        if receive_id_type == "open_id" and text and not text.startswith("ou_"):
            LOGGER.warning("推送人员字段不是人员对象或 open_id，已忽略文本值: %s", text)
            return ""
        return text

    def list_records(self, table_id: str, page_size: int = 500, app_token: str = "") -> list[dict[str, Any]]:
        """分页读取指定多维表的全部记录。"""
        if not self.configured or not app_token or not table_id:
            LOGGER.warning("飞书多维表配置不完整，跳过读取 app_token=%s table=%s", bool(app_token), table_id)
            return []
        records: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            payload = self._request("GET", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records", headers=self._headers(), params=params)
            data = payload.get("data", {})
            records.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            page_token = data.get("page_token", "")
            if not page_token:
                break
        return records

    def list_control_tasks(self) -> list[StoreTask]:
        """读取控制台并过滤暂停、空开关和缺失必要字段的记录。"""
        bitable = self.config.get("bitable", {})
        fields = bitable.get("control_fields", {})
        app_token, table_id = self.get_bitable_ref("control")
        records = self.list_records(table_id, app_token=app_token)
        tasks: list[StoreTask] = []
        LOGGER.info("[飞书][控制表] 读取记录数=%s", len(records))
        for record in records:
            raw = record.get("fields", {})
            store_name = str(self._field_value(raw.get(fields.get("store_name", "店铺名称"), "")) or "").strip()
            recipient = self._recipient_value(raw.get(fields.get("recipient", "数据推送人"), "")).strip()
            switch = self._field_value(raw.get(fields.get("switch", "推送开关"), ""))
            platform = normalise_platform(self._field_value(raw.get(fields.get("platform", "平台"), "")))
            browser_oauth = str(raw.get("browserOauth", raw.get("browser_oauth", "")) or "").strip()
            browser_id = str(raw.get("browserId", raw.get("browser_id", "")) or "").strip()
            if not store_name or not is_enabled_switch(switch) or platform not in {"tiktok", "shopee", "mercado"}:
                continue
            tasks.append(StoreTask(store_name, recipient, platform, browser_oauth, browser_id, record.get("record_id", "")))
        return tasks

    def batch_create_records(self, table_id: str, rows: Iterable[dict[str, Any]], app_token: str = "") -> None:
        """批量写入数据多维表；空 rows 不发送请求。"""
        row_list = list(rows)
        if not row_list:
            return
        if not table_id or not app_token:
            LOGGER.warning("数据多维表 app_token/table_id 未配置，跳过写入 rows=%s", len(row_list))
            return
        if not self.configured:
            LOGGER.warning("飞书凭据未配置，跳过写入多维表 table=%s rows=%s", table_id, len(row_list))
            return
        for start in range(0, len(row_list), 500):
            chunk = row_list[start:start + 500]
            request_body = {"records": [{"fields": row} for row in chunk]}
            LOGGER.info(
                "[飞书][多维表JSON] table_id=%s，批次=%s-%s，总记录数=%s，body=%s",
                table_id,
                start + 1,
                start + len(chunk),
                len(row_list),
                self._json_for_log(request_body),
            )
            self._request(
                "POST",
                f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
                headers=self._headers(),
                json=request_body,
            )

    def remove_old_records(self, table_id: str, collected_field: str, retention_days: int, app_token: str = "") -> int:
        """删除数据多维表中超过保留期的记录，返回删除条数。"""
        if not self.configured or not app_token or not table_id:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        old_ids: list[str] = []
        for record in self.list_records(table_id, app_token=app_token):
            value = record.get("fields", {}).get(collected_field)
            try:
                if isinstance(value, (int, float)) or str(value).isdigit():
                    timestamp = datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
                else:
                    timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError, OSError):
                continue
            if timestamp < cutoff and record.get("record_id"):
                old_ids.append(record["record_id"])
        for start in range(0, len(old_ids), 500):
            self._request("POST", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete", headers=self._headers(), json={"records": old_ids[start:start + 500]})
        return len(old_ids)

    def append_spreadsheet_rows(self, spreadsheet_token: str, rows: list[list[Any]], range_name: str = "Sheet1!A:Z") -> None:
        """追加历史电子表数据；range_name 通常写成 ``sheet_id!A:Z``。"""
        if not rows:
            return
        if not self.configured or not spreadsheet_token:
            LOGGER.warning("飞书电子表未配置，跳过追加 rows=%s", len(rows))
            return
        request_body = {"valueRange": {"range": range_name, "values": rows}}
        LOGGER.info(
            "[飞书][电子表JSON] range=%s，行数=%s，列数=%s，body=%s",
            range_name,
            len(rows),
            max((len(row) for row in rows), default=0),
            self._json_for_log(request_body),
        )
        self._request(
            "POST",
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values_append",
            headers=self._headers(),
            json=request_body,
        )

    def send_robot_message(self, recipient: str, title: str, content: str) -> None:
        """优先按 robot.receive_id_type 使用应用机器人定向推送，否则使用 webhook。"""
        recipients = [item.strip() for item in recipient.split(",") if item.strip()]
        if recipients and self.config.get("app_id") and self.config.get("app_secret"):
            receive_id_type = self.config.get("robot", {}).get("receive_id_type", "open_id")
            for receive_id in recipients:
                request_body = {
                    "receive_id": receive_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": f"{title}\n{content}"}, ensure_ascii=False),
                }
                LOGGER.info(
                    "[飞书][应用机器人JSON] receive_id_type=%s，body=%s",
                    receive_id_type,
                    self._json_for_log(request_body),
                )
                self._request(
                    "POST",
                    "/open-apis/im/v1/messages",
                    headers=self._headers(),
                    params={"receive_id_type": receive_id_type},
                    json=request_body,
                )
            return
        webhook = self.config.get("robot", {}).get("webhook_url", "")
        if not webhook:
            LOGGER.warning("飞书机器人 webhook 未配置，跳过推送 recipient=%s", recipient)
            return
        body: dict[str, Any] = {"msg_type": "text", "content": {"text": f"{title}\n{content}"}}
        secret = self.config.get("robot", {}).get("sign_secret", "")
        if secret:
            timestamp = str(int(time.time()))
            body["timestamp"] = timestamp
            body["sign"] = self._sign(timestamp, secret)
        LOGGER.info("[飞书][Webhook机器人JSON] body=%s", self._json_for_log(body))
        response = requests.post(webhook, json=body, timeout=self.timeout)
        response.raise_for_status()
        result = response.json()
        if result.get("code", 0) != 0:
            LOGGER.error("[飞书][Webhook机器人失败] response=%s", self._json_for_log(result))
            raise RuntimeError(f"飞书机器人推送失败: {result}")
        LOGGER.info("[飞书][Webhook机器人成功] response=%s", self._json_for_log(result))

    def send_robot_markdown_message(self, recipient: str, title: str, markdown_content: str) -> None:
        """使用飞书 interactive 卡片把 Markdown 内容推送给指定接收人。

        应用机器人接口要求把 Card 2.0 JSON 序列化到 ``content`` 字符串中；
        webhook 接口则要求把同一张卡片直接放到顶层 ``card`` 字段中。
        这样 DeepSeek 返回的标题、列表、加粗和换行会由飞书按 Markdown 渲染，
        而不是作为普通纯文本显示。
        """
        recipients = [item.strip() for item in recipient.split(",") if item.strip()]
        markdown_text = str(markdown_content or "").strip()
        if not markdown_text:
            LOGGER.warning("[飞书][Markdown机器人跳过] Markdown 内容为空，recipient=%s", recipient)
            return

        title_text = str(title or "消息").strip()
        markdown_parts = self._split_markdown_text(markdown_text, MARKDOWN_CARD_MAX_CHARS)
        total_parts = len(markdown_parts)
        LOGGER.info(
            "[飞书][Markdown分片] recipient=%s，原文字符数=%s，单片上限=%s，分片数=%s",
            recipient or "<空接收人>",
            len(markdown_text),
            MARKDOWN_CARD_MAX_CHARS,
            total_parts,
        )

        if recipients and self.config.get("app_id") and self.config.get("app_secret"):
            receive_id_type = self.config.get("robot", {}).get("receive_id_type", "open_id")
            for receive_id in recipients:
                for part_index, markdown_part in enumerate(markdown_parts, start=1):
                    part_title = title_text if total_parts == 1 else f"{title_text}（{part_index}/{total_parts}）"
                    card = self._build_markdown_card(part_title, markdown_part)
                    request_body = {
                        "receive_id": receive_id,
                        "msg_type": "interactive",
                        # /im/v1/messages 的 interactive content 必须是 JSON 字符串。
                        "content": json.dumps(card, ensure_ascii=False),
                    }
                    LOGGER.info(
                        "[飞书][应用机器人Markdown JSON] receive_id_type=%s，接收人=%s，分片=%s/%s，"
                        "分片字符数=%s，body=%s",
                        receive_id_type,
                        receive_id,
                        part_index,
                        total_parts,
                        len(markdown_part),
                        self._json_for_log(request_body),
                    )
                    self._request(
                        "POST",
                        "/open-apis/im/v1/messages",
                        headers=self._headers(),
                        params={"receive_id_type": receive_id_type},
                        json=request_body,
                    )
                    if part_index < total_parts:
                        time.sleep(MARKDOWN_CARD_SEND_INTERVAL_SECONDS)
            return

        webhook = self.config.get("robot", {}).get("webhook_url", "")
        if not webhook:
            LOGGER.warning("飞书机器人 webhook 未配置，跳过 Markdown 推送 recipient=%s", recipient)
            return

        secret = self.config.get("robot", {}).get("sign_secret", "")
        for part_index, markdown_part in enumerate(markdown_parts, start=1):
            part_title = title_text if total_parts == 1 else f"{title_text}（{part_index}/{total_parts}）"
            body: dict[str, Any] = {
                "msg_type": "interactive",
                "card": self._build_markdown_card(part_title, markdown_part),
            }
            if secret:
                timestamp = str(int(time.time()))
                body["timestamp"] = timestamp
                body["sign"] = self._sign(timestamp, secret)
            LOGGER.info(
                "[飞书][Webhook机器人Markdown JSON] 分片=%s/%s，分片字符数=%s，body=%s",
                part_index,
                total_parts,
                len(markdown_part),
                self._json_for_log(body),
            )
            response = requests.post(webhook, json=body, timeout=self.timeout)
            if not response.ok:
                LOGGER.error(
                    "[飞书][Webhook机器人Markdown HTTP失败] status=%s，response=%r",
                    response.status_code,
                    response.text[:4000],
                )
                response.raise_for_status()
            result = response.json()
            if result.get("code", 0) != 0:
                LOGGER.error("[飞书][Webhook机器人Markdown失败] response=%s", self._json_for_log(result))
                raise RuntimeError(f"飞书 Markdown 机器人推送失败: {result}")
            LOGGER.info(
                "[飞书][Webhook机器人Markdown成功] 分片=%s/%s，response=%s",
                part_index,
                total_parts,
                self._json_for_log(result),
            )
            if part_index < total_parts:
                time.sleep(MARKDOWN_CARD_SEND_INTERVAL_SECONDS)

    @staticmethod
    def _build_markdown_card(title: str, markdown_text: str) -> dict[str, Any]:
        """构造一张飞书 Card 2.0 Markdown 卡片。"""
        return {
            "schema": "2.0",
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": f"## {title}\n\n{markdown_text}",
                    }
                ]
            },
        }

    @staticmethod
    def _split_markdown_text(markdown_text: str, max_chars: int) -> list[str]:
        """按 Markdown 结构拆分长文本，避免截断列表，并保证每张卡片最多一张表格。"""
        text = str(markdown_text or "").strip()
        if not text:
            return []
        table_count = FeishuClient._count_markdown_tables(text)
        if max_chars <= 0 or (len(text) <= max_chars and table_count <= 1):
            return [text]

        lines = text.splitlines()
        blocks: list[tuple[str, list[str]]] = []
        index = 0
        while index < len(lines):
            # 飞书 Card 2.0 对单张卡片中的 table 数量有限制。识别标准 Markdown
            # 表格（表头 + 分隔线），把整张表作为独立块，后续不会和其他表格合并。
            if (
                index + 1 < len(lines)
                and lines[index].lstrip().startswith("|")
                and FeishuClient._is_markdown_table_separator(lines[index + 1])
            ):
                table_lines = [lines[index], lines[index + 1]]
                index += 2
                while index < len(lines) and lines[index].lstrip().startswith("|"):
                    table_lines.append(lines[index])
                    index += 1
                blocks.append(("table", table_lines))
                continue

            text_lines = [lines[index]]
            index += 1
            while index < len(lines):
                if (
                    index + 1 < len(lines)
                    and lines[index].lstrip().startswith("|")
                    and FeishuClient._is_markdown_table_separator(lines[index + 1])
                ):
                    break
                text_lines.append(lines[index])
                index += 1
            blocks.append(("text", text_lines))

        parts: list[str] = []
        current_text_lines: list[str] = []
        current_length = 0

        def flush_text() -> None:
            nonlocal current_text_lines, current_length
            if current_text_lines:
                parts.append("\n".join(current_text_lines).strip())
                current_text_lines = []
                current_length = 0

        for block_kind, block_lines in blocks:
            block_text = "\n".join(block_lines).strip()
            if not block_text:
                continue
            if block_kind == "table":
                flush_text()
                parts.extend(FeishuClient._split_markdown_table(block_lines, max_chars))
                continue

            # 普通文本块按行聚合；单行极端超长时再按字符切开。
            for source_line in block_lines:
                line_chunks = [
                    source_line[index:index + max_chars]
                    for index in range(0, len(source_line), max_chars)
                ] or [""]
                for line_chunk in line_chunks:
                    added_length = len(line_chunk) + (1 if current_text_lines else 0)
                    if current_text_lines and current_length + added_length > max_chars:
                        flush_text()
                    current_text_lines.append(line_chunk)
                    current_length += len(line_chunk) + (1 if len(current_text_lines) > 1 else 0)
        flush_text()
        return [part for part in parts if part]

    @staticmethod
    def _is_markdown_table_separator(line: str) -> bool:
        """判断 Markdown 表格分隔线，例如 ``|---|---:|``。"""
        cells = [cell.strip() for cell in str(line or "").strip().strip("|").split("|")]
        return bool(cells) and all(bool(re.fullmatch(r":?-{3,}:?", cell)) for cell in cells)

    @staticmethod
    def _count_markdown_tables(markdown_text: str) -> int:
        """统计普通 Markdown 表格数量，供卡片分片前判断是否触发飞书表格上限。"""
        lines = str(markdown_text or "").splitlines()
        return sum(
            1
            for index in range(len(lines) - 1)
            if lines[index].lstrip().startswith("|")
            and FeishuClient._is_markdown_table_separator(lines[index + 1])
        )

    @staticmethod
    def _split_markdown_table(table_lines: list[str], max_chars: int) -> list[str]:
        """保证每个表格分片保留表头和分隔线，并限制单片字符数。"""
        table_text = "\n".join(table_lines).strip()
        if len(table_text) <= max_chars:
            return [table_text]

        header = table_lines[:2]
        header_length = len("\n".join(header))
        chunks: list[str] = []
        current = list(header)
        current_length = header_length
        for row in table_lines[2:]:
            added_length = len(row) + 1
            if len(current) > 2 and current_length + added_length > max_chars:
                chunks.append("\n".join(current).strip())
                current = list(header)
                current_length = header_length
            current.append(row)
            current_length += added_length
        if len(current) > 2:
            chunks.append("\n".join(current).strip())
        return chunks or [table_text[:max_chars]]

    @staticmethod
    def _sign(timestamp: str, secret: str) -> str:
        """生成飞书机器人要求的 base64 HMAC-SHA256 签名。"""
        import base64
        digest = hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
        return base64.b64encode(digest).decode()
