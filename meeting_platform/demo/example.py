"""统一会议模型的可运行 demo：两个来源归一化成同构 Meeting，双 URL 入库。

    python -m meeting_platform.demo.example
"""

from __future__ import annotations

import json

from meeting_platform.core.models import MeetingSource
from meeting_platform.core.store import MeetingStore
from meeting_platform.ingest.adapters import (
    build_meeting_from_feishu_minutes,
    build_meeting_from_tencent_meeting,
)


def main() -> None:
    store = MeetingStore("data/meetings.json")

    feishu = build_meeting_from_feishu_minutes(
        doc_token="doxcnABC123",
        title="张三_20260803_周例会",
        start_time="2026-08-03T10:00:00+08:00",
        owner="张三",
        participants=["张三", "李四", "杜总"],
        media_url="https://feishu.cn/minutes/mnABC123",
        raw_content="张三：本周进度…… 李四：待办如下……",
    )
    store.upsert(feishu)

    tencent = build_meeting_from_tencent_meeting(
        meeting_record_id="1324567890123456789",
        title="客户需求评审",
        start_time="2026-08-02T15:00:00+08:00",
        owner="王五",
        participants=["王五", "赵六"],
        content_url="https://jindi-resource.oss-cn-shanghai.aliyuncs.com/meeting/1324567890123456789/summary.txt",
        media_url="https://jindi-resource.oss-cn-shanghai.aliyuncs.com/meeting/1324567890123456789/video.mp4",
        raw_content="王五：需求要点…… 赵六：分工……",
    )
    store.upsert(tencent)

    duplicate = build_meeting_from_feishu_minutes(doc_token="doxcnABC123", title="重复的")
    existing = store.find_by_source_ref(MeetingSource.FEISHU_MINUTES, duplicate.source_ref)
    if existing:
        print(f"[去重] 命中已存在会议：{existing.title}，跳过入库")

    print("\n=== 统一会议列表（双 URL 形态）===")
    for item in store.list():
        print(json.dumps({
            "source": item.source.value,
            "title": item.title,
            "content_url": item.content_url,
            "media_url": item.media_url,
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
