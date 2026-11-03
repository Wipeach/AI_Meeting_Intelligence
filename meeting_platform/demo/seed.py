"""灌入演示数据，让前端打开即有完整效果。

    python -m meeting_platform.demo.seed
"""

from __future__ import annotations

from meeting_platform.core.models import (
    ActionItem,
    ActionItemStatus,
    AnalysisResult,
    Meeting,
    MeetingSource,
    MeetingStatus,
    utc_now_iso,
)
from meeting_platform.core.store import MeetingStore


def _meeting(*, source, source_ref, title, owner, start_time, participants,
             content_url, media_url, raw_content, analysis=None, status=MeetingStatus.ANALYZED) -> Meeting:
    return Meeting(
        meeting_id=source_ref,
        source=source,
        source_ref=source_ref,
        title=title,
        owner=owner,
        start_time=start_time,
        participants=participants,
        content_url=content_url,
        media_url=media_url,
        raw_content=raw_content,
        status=status if analysis else MeetingStatus.CAPTURED,
        analysis=analysis,
        captured_at=utc_now_iso(),
        updated_at=utc_now_iso(),
    )


def _ai(summary, topics, decisions, items) -> AnalysisResult:
    return AnalysisResult(
        summary=summary,
        topics=topics,
        decisions=decisions,
        action_items=[ActionItem(**it) for it in items],
        analyzed_at=utc_now_iso(),
    )


def build_demo_meetings() -> list:
    return [
        _meeting(
            source=MeetingSource.FEISHU_MINUTES, source_ref="demo_fs_01",
            title="AI 周例会", owner="张三", start_time="2026-08-03T10:00:00+08:00",
            participants=["张三", "李四", "杜总"],
            content_url="https://feishu.cn/docx/demo_fs_01",
            media_url="https://feishu.cn/minutes/demo_fs_01",
            raw_content="张三：本周统一会议数据模型与腾讯采集器完成联调……李四：待办需定稿。杜总：周五要看演示。",
            analysis=_ai(
                summary="本周完成统一数据模型与腾讯会议采集器联调，确定下周接入飞书采集，并在周五向杜总演示。",
                topics=["统一数据模型", "腾讯会议采集器", "飞书妙记接入排期", "周五演示"],
                decisions=["统一以双 URL 形态归一会议", "OSS 作为腾讯录制存储"],
                items=[
                    {"content": "接入飞书妙记采集器", "owner": "张三", "due": "2026-08-08", "status": ActionItemStatus.OPEN},
                    {"content": "补齐统一前端页面", "owner": "李四", "due": "2026-08-07", "status": ActionItemStatus.IN_PROGRESS},
                ],
            ),
        ),
        _meeting(
            source=MeetingSource.TENCENT_MEETING, source_ref="demo_tm_01",
            title="金地楼盘销售方案评审", owner="王五", start_time="2026-08-02T15:00:00+08:00",
            participants=["王五", "赵六"],
            content_url="https://jindi-resource.oss-cn-shanghai.aliyuncs.com/meeting/demo_tm_01/summary.txt",
            media_url="https://jindi-resource.oss-cn-shanghai.aliyuncs.com/meeting/demo_tm_01/video.mp4",
            raw_content="王五：销售话术需要统一……赵六：渠道策略本周定稿。",
            analysis=_ai(
                summary="评审金地楼盘销售方案，明确话术统一与渠道策略定稿时间。",
                topics=["销售话术统一", "渠道策略", "销售手册更新"],
                decisions=["本周内完成销售手册定稿"],
                items=[
                    {"content": "更新销售手册定稿", "owner": "赵六", "due": "2026-08-06", "status": ActionItemStatus.OPEN},
                    {"content": "话术培训排期", "owner": "王五", "due": "2026-08-12", "status": ActionItemStatus.OPEN},
                ],
            ),
        ),
        _meeting(
            source=MeetingSource.FEISHU_MINUTES, source_ref="demo_fs_03",
            title="客户需求对接（待分析）", owner="李四", start_time="2026-08-01T11:00:00+08:00",
            participants=["李四", "客户A"],
            content_url="https://feishu.cn/docx/demo_fs_03",
            media_url="https://feishu.cn/minutes/demo_fs_03",
            raw_content="李四：客户希望本周给出方案……客户A：重点在交付周期和价格。",
            status=MeetingStatus.CAPTURED,
        ),
    ]


def main(store_path: str = "data/meetings.json") -> None:
    store = MeetingStore(store_path)
    meetings = build_demo_meetings()
    for meeting in meetings:
        store.upsert(meeting)
    print(f"已灌入 {len(meetings)} 条演示会议到 {store_path}")


if __name__ == "__main__":
    main()
