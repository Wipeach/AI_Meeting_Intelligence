"""Manual email supervision workflow.

This module is intentionally side-effectful only from explicit send/submit
functions: review request emails are sent manually, and Feishu records are
created only after the responsible person submits the edit page.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

from meeting_platform.core.models import Meeting, MeetingSource, MeetingStatus, utc_now_iso
from meeting_platform.core.store import MeetingStore
from meeting_platform.ingest.feishu_bitable import (
    DEFAULT_STAFF_TABLE,
    FeishuBitableClient,
    _field_text,
    clean_person_name,
    create_email_supervision,
)


EMAIL_FIELDS = ("邮箱", "邮箱地址", "邮件", "电子邮箱", "Email", "email", "mail")
NAME_FIELDS = ("人员名称", "姓名", "主持人", "名称", "name")
FALLBACK_NAME = os.getenv("SUPERVISION_FALLBACK_NAME", "狄兰")


def _smtp_bool(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _staff_directory(client: FeishuBitableClient) -> Dict[str, Dict[str, str]]:
    table_id = os.getenv("FEISHU_BITABLE_STAFF_TABLE", DEFAULT_STAFF_TABLE)
    out: Dict[str, Dict[str, str]] = {}
    for record in client.list_records(table_id):
        fields = record.get("fields") or {}
        name = ""
        email = ""
        for field in NAME_FIELDS:
            name = _field_text(fields.get(field))
            if name:
                break
        for field in EMAIL_FIELDS:
            email = _field_text(fields.get(field))
            if email:
                break
        cleaned = clean_person_name(name)
        if cleaned:
            out[cleaned] = {"name": cleaned, "email": email.strip()}
    return out


def list_staff_options(client: Optional[FeishuBitableClient] = None) -> List[Dict[str, str]]:
    client = client or FeishuBitableClient()
    return sorted(_staff_directory(client).values(), key=lambda item: item["name"])


def resolve_recipient(convener: str, *, client: Optional[FeishuBitableClient] = None) -> Dict[str, Any]:
    client = client or FeishuBitableClient()
    directory = _staff_directory(client)
    cleaned = clean_person_name(convener)
    fallback = False
    note = ""
    person = directory.get(cleaned)
    if not person:
        fallback = True
        person = directory.get(FALLBACK_NAME)
        note = "该会议主持人查询不到对应会议主持人，请补充并给表链接"
    if not person or not person.get("email"):
        target = FALLBACK_NAME if fallback else cleaned
        raise RuntimeError(f"人员表中未找到 {target} 的邮箱")
    return {"name": person["name"], "email": person["email"], "fallback": fallback, "note": note}


def build_email_body(
    meeting: Meeting,
    *,
    title: str,
    convener: str,
    items: List[Dict[str, Any]],
    fallback_note: str = "",
) -> str:
    lines = [
        "您好，",
        "",
        "以下会议事项请协助督办。",
        "",
        f"会议主题：{title}",
        f"会议召集人：{convener}",
        f"会议时间：{meeting.start_time or ''}",
        f"纪要链接：{meeting.content_url or ''}",
    ]
    if fallback_note:
        lines.extend(["", fallback_note])
        lines.append("人员表链接：https://gemdalesh.feishu.cn/base/OdwobaKJga77p8s22KLcRSZNn6d?table=tblM4CWr9NuE0Wr6&view=vewR0CeD3E")
    lines.extend(["", "待办督办项："])
    if items:
        for idx, item in enumerate(items, 1):
            qty = str(item.get("quantity") or "").strip()
            owner = str(item.get("owner") or "").strip()
            due = str(item.get("due") or "").strip()
            suffix = []
            if qty:
                suffix.append(f"数量：{qty}")
            if owner:
                suffix.append(f"责任人：{owner}")
            if due:
                suffix.append(f"截止：{due}")
            confirmation = str(item.get("confirmation") or "").strip()
            if confirmation:
                suffix.append(f"事项确认：{confirmation}")
            tail = f"（{'；'.join(suffix)}）" if suffix else ""
            lines.append(f"{idx}. {str(item.get('content') or '').strip()}{tail}")
    else:
        lines.append("无")
    analysis = meeting.analysis
    if analysis:
        lines.extend(["", "AI摘要：", analysis.summary or ""])
        if analysis.topics:
            lines.extend(["", "议题：", "；".join(analysis.topics)])
        if analysis.decisions:
            lines.extend(["", "决议：", "；".join(analysis.decisions)])
    if meeting.raw_content:
        lines.extend(["", "纪要原文：", meeting.raw_content])
    return "\n".join(lines)


def build_review_request_body(meeting: Meeting, *, edit_url: str, fallback_note: str = "") -> str:
    lines = [
        "您好，",
        "",
        "请打开下面链接，对会议详情和待办督办项进行修改/确认：",
        edit_url,
        "",
        f"会议名称：{meeting.title or ''}",
        f"会议主持人：{meeting.owner or ''}",
        f"会议时间：{meeting.start_time or ''}",
        f"视频或录音链接：{meeting.media_url or ''}",
    ]
    if fallback_note:
        lines.extend(["", fallback_note])
        lines.append("人员表链接：https://gemdalesh.feishu.cn/base/OdwobaKJga77p8s22KLcRSZNn6d?table=tblM4CWr9NuE0Wr6&view=vewR0CeD3E")
    return "\n".join(lines)


def send_mail(*, to_addr: str, subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST", "")
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    from_addr = os.getenv("SMTP_FROM", user)
    timeout = int(os.getenv("SMTP_TIMEOUT", "20"))
    if not (host and user and password and from_addr):
        raise RuntimeError("SMTP 配置不完整")

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    if _smtp_bool("SMTP_USE_SSL", "true"):
        with smtplib.SMTP_SSL(host, port, timeout=timeout) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)


def validate_items(items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    clean: List[Dict[str, str]] = []
    for idx, item in enumerate(items or []):
        content = str(item.get("content") or "").strip()
        quantity = str(item.get("quantity") or "").strip()
        owner = str(item.get("owner") or "").strip()
        due = str(item.get("due") or "").strip()
        confirmation = str(item.get("confirmation") or "已确定").strip()
        if not content:
            raise ValueError(f"第 {idx + 1} 条待办内容不可为空")
        if not owner:
            raise ValueError(f"第 {idx + 1} 条责任人必选")
        clean.append({
            "content": content,
            "quantity": quantity,
            "owner": owner,
            "due": due,
            "confirmation": confirmation,
        })
    return clean


def validate_item_owners_exist(items: List[Dict[str, str]], client: FeishuBitableClient) -> None:
    if not items:
        return
    directory = _staff_directory(client)
    missing = []
    for item in items:
        owner = clean_person_name(item.get("owner") or "")
        if owner and owner not in directory:
            missing.append(owner)
    if missing:
        names = "、".join(dict.fromkeys(missing))
        raise ValueError(f"待办责任人在人员表中不存在：{names}")


def send_supervision(
    meeting: Meeting,
    *,
    title: str,
    convener: str,
    items: List[Dict[str, Any]],
    client: Optional[FeishuBitableClient] = None,
) -> Dict[str, Any]:
    title = (title or "").strip()
    convener = (convener or "").strip()
    if not title:
        return {"ok": False, "error": "会议主题必填"}
    items = validate_items(items)
    client = client or FeishuBitableClient()
    validate_item_owners_exist(items, client)
    recipient = resolve_recipient(convener, client=client)
    body = build_email_body(
        meeting,
        title=title,
        convener=convener,
        items=items,
        fallback_note=recipient["note"],
    )
    send_mail(to_addr=recipient["email"], subject=f"会议督办：{title}", body=body)
    written = create_email_supervision(meeting, title=title, convener=convener, items=items, client=client)
    if not written.get("ok"):
        return {"ok": False, "error": written.get("error") or "邮件已发送，但多维表格写入失败", **written}
    return {"ok": True, "email_to": recipient["email"], "receiver": recipient["name"], **written}


def send_review_request(
    meeting: Meeting,
    *,
    edit_url: str,
    client: Optional[FeishuBitableClient] = None,
) -> Dict[str, Any]:
    client = client or FeishuBitableClient()
    recipient = resolve_recipient(meeting.owner or "", client=client)
    body = build_review_request_body(meeting, edit_url=edit_url, fallback_note=recipient["note"])
    send_mail(to_addr=recipient["email"], subject=f"请确认会议督办事项：{meeting.title or ''}", body=body)
    return {
        "ok": True,
        "email_to": recipient["email"],
        "receiver": recipient["name"],
        "fallback": recipient["fallback"],
    }


def review_request_sent(meeting: Meeting) -> bool:
    payload = meeting.supervision_payload or {}
    return bool(payload.get("edit_request_sent_at"))


def request_review_edits_for_range(
    store: MeetingStore,
    *,
    date_from: str = "",
    date_to: str = "",
    sources: Optional[List[MeetingSource]] = None,
    base_url: str,
) -> Dict[str, Any]:
    picked = sources or [MeetingSource.TENCENT_MEETING, MeetingSource.FEISHU_MINUTES]
    meetings: List[Meeting] = []
    for src in picked:
        meetings.extend(store.list(source=src, date_from=date_from or None, date_to=date_to or None))

    base = base_url.rstrip("/")
    sent = skipped_sent = skipped_submitted = skipped_not_analyzed = failed = 0
    items = []
    for meeting in meetings:
        if meeting.supervised:
            skipped_submitted += 1
            continue
        if review_request_sent(meeting):
            skipped_sent += 1
            continue
        if meeting.status != MeetingStatus.ANALYZED:
            skipped_not_analyzed += 1
            continue
        try:
            result = send_review_request(meeting, edit_url=f"{base}/supervision/{meeting.meeting_id}")
            meeting.supervision_payload = {
                **(meeting.supervision_payload or {}),
                "edit_request_sent_at": utc_now_iso(),
                "edit_request_receiver": result.get("receiver", ""),
                "edit_request_email": result.get("email_to", ""),
            }
            store.upsert(meeting)
            sent += 1
            items.append({
                "meeting_id": meeting.meeting_id,
                "title": meeting.title,
                "sent": True,
                "receiver": result.get("receiver", ""),
            })
        except Exception as exc:  # noqa: BLE001
            failed += 1
            items.append({"meeting_id": meeting.meeting_id, "title": meeting.title, "error": str(exc)[:160]})
    return {
        "ok": failed == 0,
        "sent": sent,
        "failed": failed,
        "skipped_sent": skipped_sent,
        "skipped_submitted": skipped_submitted,
        "skipped_not_analyzed": skipped_not_analyzed,
        "items": items,
    }


def submit_supervision(
    meeting: Meeting,
    *,
    title: str,
    convener: str,
    responsible: str = "",
    items: List[Dict[str, Any]],
    client: Optional[FeishuBitableClient] = None,
) -> Dict[str, Any]:
    title = (title or "").strip()
    convener = (convener or "").strip()
    if not title:
        return {"ok": False, "error": "会议主题必填"}
    items = validate_items(items)
    client = client or FeishuBitableClient()
    validate_item_owners_exist(items, client)
    written = create_email_supervision(
        meeting,
        title=title,
        convener=convener,
        responsible=responsible,
        items=items,
        client=client,
    )
    if not written.get("ok"):
        return {"ok": False, "error": written.get("error") or "多维表格写入失败", **written}
    return {"ok": True, **written}
