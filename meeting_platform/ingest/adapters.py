"""数据源适配器：把各来源的原始数据归一化成统一的 ``Meeting``。

新增一个会议来源时，在这里加一个 ``build_meeting_from_xxx`` 函数即可。
"""

from __future__ import annotations

from typing import Optional

import uuid as _uuid

from meeting_platform.core.models import Meeting, MeetingSource, MeetingStatus


def build_meeting_from_feishu_minutes(
    *,
    doc_token: str,
    title: str = "",
    subject: str = "",
    start_time: str = "",
    end_time: str = "",
    owner: str = "",
    participants: Optional[list] = None,
    content_url: str = "",
    media_url: str = "",
    raw_content: str = "",
) -> Meeting:
    """飞书妙记文档 → 统一 Meeting。"""
    return Meeting(
        meeting_id=_uuid.uuid4().hex,
        source=MeetingSource.FEISHU_MINUTES,
        source_ref=doc_token,
        title=title,
        subject=subject,
        start_time=start_time,
        end_time=end_time,
        owner=owner,
        participants=list(participants or []),
        content_url=content_url or f"https://feishu.cn/docx/{doc_token}",
        media_url=media_url,
        raw_content=raw_content,
        status=MeetingStatus.CAPTURED,
    )


def build_meeting_from_tencent_meeting(
    *,
    meeting_record_id: str,
    title: str = "",
    start_time: str = "",
    end_time: str = "",
    owner: str = "",
    participants: Optional[list] = None,
    meeting_code: str = "",
    department: str = "",
    content_url: str = "",
    media_url: str = "",
    raw_content: str = "",
) -> Meeting:
    """腾讯会议录制 → 统一 Meeting。"""
    return Meeting(
        meeting_id=_uuid.uuid4().hex,
        source=MeetingSource.TENCENT_MEETING,
        source_ref=meeting_record_id,
        meeting_code=meeting_code,
        department=department,
        title=title,
        subject="",
        start_time=start_time,
        end_time=end_time,
        owner=owner,
        participants=list(participants or []),
        content_url=content_url,
        media_url=media_url,
        raw_content=raw_content,
        status=MeetingStatus.CAPTURED,
    )
