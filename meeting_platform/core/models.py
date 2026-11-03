"""统一会议数据模型。

无论会议来自飞书妙记还是腾讯会议，最终都归一化成这里的 ``Meeting``。
每个 Meeting 都带两个 URL，构成统一的「双 URL」形态：

- ``content_url``：纪要 / 转写文本的访问地址（飞书文档链接 或 OSS 纪要链接）
- ``media_url``：音视频录制的访问地址（飞书妙记链接 或 OSS 录制链接）

这样下游的 AI 分析、督办、前端展示都不用关心来源差异。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class MeetingSource(str, Enum):
    """会议来源。新增来源（如钉钉、Zoom）只需在此追加一项。"""

    FEISHU_MINUTES = "feishu_minutes"
    TENCENT_MEETING = "tencent_meeting"


class MeetingStatus(str, Enum):
    """会议在流水线中的处理阶段。"""

    CAPTURED = "captured"
    ANALYZED = "analyzed"
    ARCHIVED = "archived"


class ActionItemStatus(str, Enum):
    """待办（督办项）状态。"""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    OVERDUE = "overdue"


@dataclass
class ActionItem:
    """一条会议待办，供督办环节跟踪。"""

    content: str
    owner: str = ""
    due: str = ""
    status: ActionItemStatus = ActionItemStatus.OPEN


@dataclass
class AnalysisResult:
    """AI 对会议内容的结构化分析结果。"""

    summary: str = ""
    meeting_topic: str = ""  # AI 生成的整场会议核心议题名（8-15 汉字），用于拼接展示标题
    topics: List[str] = field(default_factory=list)
    decisions: List[str] = field(default_factory=list)
    action_items: List[ActionItem] = field(default_factory=list)
    analyzed_at: str = ""
    corrections: int = 0  # 分析前术语纠错的替换处数（0 = 未启用词表或无命中）


@dataclass
class Meeting:
    """统一会议实体，是整个平台的数据中枢。"""

    meeting_id: str
    source: MeetingSource
    source_ref: str

    meeting_code: str = ""                               # 腾讯会议号，如 4276856817
    department: str = ""                                 # 部门/地区，如 DG上海地区公司XX

    title: str = ""
    subject: str = ""
    start_time: str = ""
    end_time: str = ""
    owner: str = ""
    participants: List[str] = field(default_factory=list)

    content_url: str = ""
    media_url: str = ""

    raw_content: str = ""
    corrected_content: str = ""  # 术语纠错后的文本（raw_content 永远保留原文，AI 分析用此字段）

    status: MeetingStatus = MeetingStatus.CAPTURED
    supervised: bool = False                                   # AI 待办是否已回填到督办表
    analysis: Optional[AnalysisResult] = None
    supervision_payload: Dict[str, Any] = field(default_factory=dict)
    supervision_sent_at: str = ""

    captured_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return _stringify(asdict(self))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Meeting":
        status = MeetingStatus(data.get("status", MeetingStatus.CAPTURED))
        source = MeetingSource(data["source"])

        analysis = None
        raw_analysis = data.get("analysis")
        if isinstance(raw_analysis, dict):
            action_items = [
                ActionItem(
                    content=item.get("content", ""),
                    owner=item.get("owner", ""),
                    due=item.get("due", ""),
                    status=ActionItemStatus(item.get("status", ActionItemStatus.OPEN)),
                )
                for item in raw_analysis.get("action_items", [])
            ]
            analysis = AnalysisResult(
                summary=raw_analysis.get("summary", ""),
                meeting_topic=raw_analysis.get("meeting_topic", ""),
                topics=list(raw_analysis.get("topics", [])),
                decisions=list(raw_analysis.get("decisions", [])),
                action_items=action_items,
                analyzed_at=raw_analysis.get("analyzed_at", ""),
                corrections=int(raw_analysis.get("corrections", 0) or 0),
            )

        return cls(
            meeting_id=data.get("meeting_id") or uuid.uuid4().hex,
            source=source,
            source_ref=data["source_ref"],
            meeting_code=data.get("meeting_code", ""),
            department=data.get("department", ""),
            title=data.get("title", ""),
            subject=data.get("subject", ""),
            start_time=data.get("start_time", ""),
            end_time=data.get("end_time", ""),
            owner=data.get("owner", ""),
            participants=list(data.get("participants", [])),
            content_url=data.get("content_url", ""),
            media_url=data.get("media_url", ""),
            raw_content=data.get("raw_content", ""),
            corrected_content=data.get("corrected_content", ""),
            status=status,
            supervised=data.get("supervised", False),
            analysis=analysis,
            supervision_payload=dict(data.get("supervision_payload") or {}),
            supervision_sent_at=data.get("supervision_sent_at", ""),
            captured_at=data.get("captured_at", ""),
            updated_at=data.get("updated_at", ""),
        )


def display_title(meeting: "Meeting") -> str:
    """展示用标题：有 AI 议题名时拼成「原标题_AI议题名」，否则回退原标题。"""
    base = (meeting.title or "").strip()
    topic = ""
    if meeting.analysis:
        topic = (getattr(meeting.analysis, "meeting_topic", "") or "").strip()
    return f"{base}_{topic}" if topic else base


def _stringify(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {key: _stringify(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_stringify(item) for item in obj]
    return obj


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
