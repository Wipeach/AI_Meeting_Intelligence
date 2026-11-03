"""飞书多维表格（Bitable）督办回填。

把已分析会议里的 AI 待办（action_items）回填到「督办事项」多维表：
- 清洗会议主持人姓名（去工号英文/数字前缀）→ 查「人员信息」表，主持人在册才写
- 该会议每条待办写一行：督办事项/事项来源/来源日期/责任人/主持人/要求完成时间
- 用隐藏「唯一键」字段按 来源:source_ref:序号 去重，避免定时重复写入

用应用 tenant_access_token 写（前提：应用是该多维表的协作者-可编辑）。

环境变量：
    FEISHU_APP_ID / FEISHU_APP_SECRET（复用）
    FEISHU_BITABLE_APP_TOKEN        多维表 app_token
    FEISHU_BITABLE_TASK_TABLE       督办事项 table_id
    FEISHU_BITABLE_STAFF_TABLE      人员信息 table_id
    FEISHU_BITABLE_DEDUP_FIELD      去重字段名，默认 唯一键
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

import requests

from meeting_platform.core.models import MeetingSource, MeetingStatus
from meeting_platform.core.store import MeetingStore

log = logging.getLogger("feishu_bitable")

BASE = "https://open.feishu.cn"
DEFAULT_APP_TOKEN = "OdwobaKJga77p8s22KLcRSZNn6d"
DEFAULT_STAFF_TABLE = "tblM4CWr9NuE0Wr6"
DEFAULT_TASK_TABLE = "tblT3ZGxybys2QDN"
STAFF_TABLE_URL = (
    "https://gemdalesh.feishu.cn/base/OdwobaKJga77p8s22KLcRSZNn6d"
    "?table=tblM4CWr9NuE0Wr6&view=vewR0CeD3E"
)
DEDUP_FIELD = os.getenv("FEISHU_BITABLE_DEDUP_FIELD", "唯一键")


def clean_person_name(raw: str) -> str:
    """清理人员姓名：去掉前导工号/英文/数字前缀（如 DGDBF06燕丽 → 燕丽；纯 ASCII 原样返回）。"""
    raw = (raw or "").strip()
    m = re.match(r"^[A-Za-z0-9_\-]*\s*([一-龥].*)$", raw)
    return m.group(1).strip() if m else raw


def _field_text(value: Any) -> str:
    """从多维表字段值里取纯文本。飞书文本字段读回来是富文本数组 [{text:..}]，也可能是字符串/数字。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = []
        for seg in value:
            if isinstance(seg, dict):
                parts.append(seg.get("text") or seg.get("name") or "")
            else:
                parts.append(str(seg))
        return "".join(parts)
    if isinstance(value, dict):
        return value.get("text") or value.get("name") or ""
    return str(value)


class FeishuBitableClient:
    """飞书多维表客户端（tenant_access_token，自动续期）。"""

    def __init__(self):
        self.app_id = os.getenv("FEISHU_APP_ID", "")
        self.app_secret = os.getenv("FEISHU_APP_SECRET", "")
        self.app_token = os.getenv("FEISHU_BITABLE_APP_TOKEN", DEFAULT_APP_TOKEN)
        self._tenant: Optional[str] = None
        self._tenant_expire = 0.0

    # ----- tenant token -----

    def _tenant_token(self) -> str:
        if self._tenant and time.time() < self._tenant_expire - 60:
            return self._tenant
        resp = requests.post(
            f"{BASE}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret}, timeout=30,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 tenant_access_token 失败：{data}")
        self._tenant = data["tenant_access_token"]
        self._tenant_expire = time.time() + int(data.get("expire", 7200))
        return self._tenant

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._tenant_token()}", "Content-Type": "application/json"}

    # ----- 记录 -----

    def list_records(self, table_id: str) -> List[Dict[str, Any]]:
        """列出某表全部记录（用 search 接口分页）。"""
        url = f"{BASE}/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/search"
        items: List[Dict[str, Any]] = []
        page_token = ""
        while True:
            body: Dict[str, Any] = {"page_size": 500}
            if page_token:
                body["page_token"] = page_token
            resp = requests.post(url, headers=self._headers(), json=body, timeout=30)
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"读取多维表失败：{data}")
            d = data.get("data", {}) or {}
            items.extend(d.get("items", []) or [])
            if not d.get("has_more"):
                break
            page_token = d.get("page_token") or ""
            if not page_token:
                break
        return items

    def create_record(self, table_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{BASE}/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}/records"
        resp = requests.post(url, headers=self._headers(), json={"fields": fields}, timeout=30)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"写入多维表失败：{data}")
        return data.get("data", {}) or {}

    # ----- 字段 -----

    def list_field_names(self, table_id: str) -> List[str]:
        url = f"{BASE}/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields"
        resp = requests.get(url, headers=self._headers(), timeout=30)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"读取字段失败：{data}")
        return [f.get("field_name") for f in (data.get("data", {}).get("items", []) or [])]

    def ensure_field(self, table_id: str, field_name: str, *, ftype: int = 1) -> None:
        """确保某文本字段存在，不存在则创建（用于去重字段）。"""
        if field_name in self.list_field_names(table_id):
            return
        url = f"{BASE}/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields"
        resp = requests.post(url, headers=self._headers(),
                             json={"field_name": field_name, "type": ftype}, timeout=30)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"创建字段 {field_name} 失败：{data}")


def _first_existing(fields: set, *names: str) -> Optional[str]:
    for name in names:
        if name in fields:
            return name
    return None


def _put_alias(out: Dict[str, Any], existing: set, value: Any, *names: str) -> None:
    field = _first_existing(existing, *names)
    if field and value not in (None, ""):
        out[field] = value


def staff_name_set(client: Optional[FeishuBitableClient] = None, *, table_id: Optional[str] = None) -> set:
    """Return cleaned staff names from the Feishu personnel table."""
    client = client or FeishuBitableClient()
    table_id = table_id or os.getenv("FEISHU_BITABLE_STAFF_TABLE", DEFAULT_STAFF_TABLE)
    names = set()
    for record in client.list_records(table_id):
        fields = record.get("fields") or {}
        name = (
            _field_text(fields.get("人员名称"))
            or _field_text(fields.get("姓名"))
            or _field_text(fields.get("主持人"))
            or _field_text(fields.get("名称"))
        )
        if name:
            names.add(clean_person_name(name))
    return names


def build_supervision_detail(meeting, *, note: str = "") -> str:
    """Build the supervision detail text, excluding the video/audio URL by design."""
    analysis = meeting.analysis
    parts = [
        f"会议名称：{meeting.title}",
        f"会议召集人：{meeting.owner or ''}",
        f"会议时间：{meeting.start_time or ''}",
        f"纪要链接：{meeting.content_url or ''}",
    ]
    if analysis:
        if analysis.summary:
            parts.append(f"AI摘要：{analysis.summary}")
        if analysis.topics:
            parts.append("议题：" + "；".join(analysis.topics))
        if analysis.decisions:
            parts.append("决议：" + "；".join(analysis.decisions))
    if meeting.raw_content:
        parts.append("纪要原文：\n" + meeting.raw_content)
    if note:
        parts.append("备注：" + note)
    return "\n\n".join(parts)


def create_email_supervision(
    meeting,
    *,
    title: str,
    convener: str,
    responsible: str = "",
    items: List[Dict[str, Any]],
    client: Optional[FeishuBitableClient] = None,
) -> Dict[str, Any]:
    """Create manually confirmed supervision records in the Feishu target table.

    The caller is expected to validate user-editable data first. This function
    still performs defensive validation because writing the Bitable record is
    the side effect boundary.
    """
    title = (title or "").strip()
    convener = (convener or "").strip()
    responsible = clean_person_name(responsible or meeting.owner or convener or "")
    if not title:
        return {"ok": False, "error": "会议主题必填", "written": 0}
    normalized_items = []
    for idx, item in enumerate(items or []):
        content = str(item.get("content") or "").strip()
        quantity = str(item.get("quantity") or "").strip()
        owner = clean_person_name(str(item.get("owner") or "").strip())
        due = str(item.get("due") or "").strip()
        confirmation = str(item.get("confirmation") or "已确定").strip()
        if not content:
            return {"ok": False, "error": f"第 {idx + 1} 条待办内容不可为空", "written": 0}
        if not owner:
            return {"ok": False, "error": f"第 {idx + 1} 条责任人必选", "written": 0}
        normalized_items.append({
            "content": content,
            "quantity": quantity,
            "owner": owner,
            "due": due,
            "confirmation": confirmation,
        })

    client = client or FeishuBitableClient()
    task_table = os.getenv("FEISHU_BITABLE_TASK_TABLE", DEFAULT_TASK_TABLE)
    staff_table = os.getenv("FEISHU_BITABLE_STAFF_TABLE", DEFAULT_STAFF_TABLE)
    if not (client.app_token and task_table and staff_table):
        return {"ok": False, "error": "未配置飞书多维表格 app_token/table_id", "written": 0}

    try:
        people = staff_name_set(client, table_id=staff_table)
        task_fields = set(client.list_field_names(task_table))
        client.ensure_field(task_table, DEDUP_FIELD)
        task_fields.add(DEDUP_FIELD)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"读取飞书多维表格失败：{exc}", "written": 0}

    receiver = responsible if responsible in people else "狄兰"
    missing_owners = [item["owner"] for item in normalized_items if item["owner"] not in people]
    if missing_owners:
        names = "、".join(dict.fromkeys(missing_owners))
        return {"ok": False, "error": f"待办责任人在人员表中不存在：{names}", "written": 0}
    missing_note = ""
    if receiver == "狄兰":
        missing_note = (
            f"该会议主持人查询不到对应会议主持人，请补充并给表链接：{STAFF_TABLE_URL}"
        )
    detail = build_supervision_detail(meeting, note=missing_note)
    rows = normalized_items or [{"content": "", "quantity": "", "owner": responsible, "due": ""}]

    written = failed = 0
    errors: List[str] = []
    for idx, item in enumerate(rows):
        key = f"email:{meeting.source.value}:{meeting.source_ref}:{idx}:{int(time.time())}"
        fields: Dict[str, Any] = {DEDUP_FIELD: key}
        _put_alias(fields, task_fields, title, "会议名称", "会议主题", "事项来源", "来源事项")
        _put_alias(fields, task_fields, convener or responsible, "会议召集人", "召集人", "发起人")
        _put_alias(fields, task_fields, responsible, "会议主持人", "会议责任人")
        _put_alias(fields, task_fields, receiver, "督办人", "收件人", "主持人")
        _put_alias(fields, task_fields, item["owner"], "责任人", "待办责任人")
        _put_alias(fields, task_fields, item["content"], "待办督办项", "督办事项", "待办事项", "事项内容")
        _put_alias(fields, task_fields, item["quantity"], "数量", "待办数量")
        _put_alias(fields, task_fields, item.get("confirmation"), "是否确认会议事项内容", "事项确认", "确认状态")
        _put_alias(fields, task_fields, item["due"], "要求完成时间", "截止时间", "截止日期")
        _put_alias(fields, task_fields, meeting.start_time[:10], "来源日期", "会议日期")
        _put_alias(fields, task_fields, detail, "督办详情", "详情", "备注")
        if missing_note:
            _put_alias(fields, task_fields, missing_note, "异常说明", "备注说明")
        try:
            client.create_record(task_table, fields)
            written += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            errors.append(str(exc))
    return {
        "ok": failed == 0,
        "written": written,
        "failed": failed,
        "receiver": receiver,
        "fallback": bool(missing_note),
        "note": missing_note,
        "errors": errors[:3],
    }


# ============================ 督办回填编排 ============================

def backfill_supervision(
    store: MeetingStore,
    *,
    source: Optional[MeetingSource] = None,
    client: Optional[FeishuBitableClient] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
    on_phase: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """把已分析会议的 AI 待办回填到飞书「督办事项」多维表。

    - source 指定则只处理该来源；否则全部。
    - 主持人(会议主持人)清洗后需在「人员信息」表内，否则整场跳过。
    - 每条待办写一行，按 唯一键 去重。
    """
    app_token = os.getenv("FEISHU_BITABLE_APP_TOKEN", DEFAULT_APP_TOKEN)
    task_table = os.getenv("FEISHU_BITABLE_TASK_TABLE", DEFAULT_TASK_TABLE)
    staff_table = os.getenv("FEISHU_BITABLE_STAFF_TABLE", DEFAULT_STAFF_TABLE)
    if not (app_token and task_table and staff_table):
        return {"error": "未配置 FEISHU_BITABLE_APP_TOKEN/TASK_TABLE/STAFF_TABLE", "written": 0}

    client = client or FeishuBitableClient()
    if on_phase:
        on_phase("读取人员/已有督办")

    # 1) 人员名单（清洗后的姓名集合）
    staff_names = set()
    try:
        for r in client.list_records(staff_table):
            name = _field_text((r.get("fields") or {}).get("人员名称"))
            if name:
                staff_names.add(clean_person_name(name))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"读取人员表失败：{exc}", "written": 0}

    # 2) 确保去重字段存在 + 收集已有唯一键
    existing_keys: set = set()
    try:
        client.ensure_field(task_table, DEDUP_FIELD)
        for r in client.list_records(task_table):
            k = _field_text((r.get("fields") or {}).get(DEDUP_FIELD))
            if k:
                existing_keys.add(str(k))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"读取督办表失败：{exc}", "written": 0}

    # 3) 收集要写的行：遍历已分析、有待办的会议
    targets = [m for m in store.list(status=MeetingStatus.ANALYZED, source=source)
               if m.analysis and m.analysis.action_items]
    pending: List[Dict[str, Any]] = []
    processed_meetings = []  # 主持人在册、已处理待办的会议 → 标记已督办
    skipped_no_person = 0
    for m in targets:
        owner = clean_person_name(m.owner or "")
        if not owner or owner not in staff_names:
            skipped_no_person += 1  # 主持人不在人员名单 → 整场跳过
            continue
        processed_meetings.append(m)
        for idx, ai in enumerate(m.analysis.action_items):
            key = f"{m.source.value}:{m.source_ref}:{idx}"
            if key in existing_keys:
                continue
            pending.append({
                "key": key, "owner": owner,
                "督办事项": ai.content,
                "事项来源": m.title,
                "来源日期": (m.start_time or "")[:10],
                "责任人": clean_person_name(ai.owner or ""),
                "会议召集人": owner,
                "会议主持人": owner,
                "要求完成时间": ai.due or "",
            })

    # 4) 逐条写入 + 进度
    written = failed = 0
    total = len(pending)
    if on_phase:
        on_phase(f"督办写入 0/{total}")
    for i, row in enumerate(pending, 1):
        fields = {k: v for k, v in row.items() if k not in ("key", "owner")}
        fields[DEDUP_FIELD] = row["key"]
        try:
            client.create_record(task_table, fields)
            existing_keys.add(row["key"])
            written += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.warning("写入督办失败 %s：%s", row["key"], exc)
        if on_progress:
            on_progress(i, total)
        if on_phase:
            on_phase(f"督办写入 {i}/{total}")

    # 5) 标记已处理会议为「已督办」
    marked = 0
    for m in processed_meetings:
        if not m.supervised:
            m.supervised = True
            store.upsert(m)
            marked += 1

    return {
        "written": written, "failed": failed, "marked_supervised": marked,
        "skipped_no_person": skipped_no_person, "total_pending": total,
        "analyzed_meetings": len(targets), "staff_count": len(staff_names),
    }
