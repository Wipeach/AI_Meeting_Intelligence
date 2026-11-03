"""一键真实跑通：腾讯会议定向采集（部门 ∪ 会议号）→ OSS → 入库 → DeepSeek 分析。

采集范围 = DG上海地区公司部门员工会议 ∪ 内置30个会议号会议室录制（合并去重）。
所有内容传 OSS 的 会议纪要/{日期}/，不下本地。凭证从 .env 读取。

用法：
    python -m meeting_platform.ops.runner                       # 最近 7 天
    python -m meeting_platform.ops.runner --since 20260803      # 上线：从 20260803 爬到今天
    python -m meeting_platform.ops.runner --no-video            # 只取纪要
    python -m meeting_platform.ops.runner --fresh               # 先清空旧数据
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone

from meeting_platform.analysis.analyzer import analyze_pending
from meeting_platform.core.store import MeetingStore
from meeting_platform.ingest.oss import OSSUploader
from meeting_platform.ingest.tencent import collect_tencent_meetings

TZ = timezone(timedelta(hours=8))


def _since_to_ts(date_str: str):
    """'20260803' → 当天 00:00 (东八区) 的秒级时间戳。"""
    return int(datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=TZ).timestamp())


def main() -> None:
    parser = argparse.ArgumentParser(description="一键真实采集 + 分析（部门∪会议号）")
    parser.add_argument("--days", type=int, default=7, help="采集最近 N 天（与 --since 二选一）")
    parser.add_argument("--since", default="", help="从该日起爬到今天，如 20260803")
    parser.add_argument("--limit", type=int, default=2000, help="安全上限（防海量）")
    parser.add_argument("--workers", type=int, default=4, help="并发线程数")
    parser.add_argument("--no-video", action="store_true", help="只取纪要，不取视频")
    parser.add_argument("--fresh", action="store_true", help="先清空旧数据")
    parser.add_argument("--store", default="data/meetings.json", help="存储文件路径")
    args = parser.parse_args()

    if args.fresh and os.path.exists(args.store):
        os.remove(args.store)
        print(f"已清空旧数据：{args.store}")

    store = MeetingStore(args.store)

    start_ts = _since_to_ts(args.since) if args.since else None
    end_ts = int(datetime.now(TZ).timestamp()) if args.since else None
    window = f"{args.since} 至今" if args.since else f"最近 {args.days} 天"

    print(f"=== 1) 定向采集腾讯会议（部门DG上海地区公司 ∪ 会议号，合并去重）窗口：{window} ===")
    result = collect_tencent_meetings(
        store,
        days=args.days,
        limit=args.limit,
        with_video=not args.no_video,
        max_workers=args.workers,
        start_ts=start_ts,
        end_ts=end_ts,
        uploader=OSSUploader(),
    )
    print(f"新建 {result['created']} | 跳过(去重/无内容) {result['skipped']} | 失败 {result['failed']}")
    for item in result["items"]:
        if item.get("_info"):
            print(f"  {item['_info']}")
        elif item.get("error"):
            print(f"  [失败] {item.get('meeting_record_id')}: {str(item.get('error'))[:80]}")

    print("\n=== 2) DeepSeek AI 分析（纪要 → 摘要/议题/决议/待办）===")
    analysis = analyze_pending(store)
    print(f"分析 {analysis['analyzed']} | 失败 {analysis['failed']}")

    print(f"\n=== 3) 完成，库中共 {len(store.list())} 条会议 ===")
    print("查看：python -m meeting_platform.web.app  →  http://127.0.0.1:5050")


if __name__ == "__main__":
    main()
