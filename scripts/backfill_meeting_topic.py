"""回填 meeting_topic —— 并发版。

对【已分析、有纪要原文、且尚未生成 meeting_topic】的会议并发重跑 AI 分析。
- 会议级并发（max_workers 路），每场会议内部的分块仍串行（LLM 顺序依赖）。
- 续跑：已有 meeting_topic 的自动跳过，中断后重跑不浪费。
- 写库在主线程串行执行，避免 SQLite 并发写锁。
- 输出实时 flush，后台跑也能看进度。

用法（项目根目录）：
    python -m scripts.backfill_meeting_topic              # 并发重跑待处理会议
    python -m scripts.backfill_meeting_topic -j 8         # 指定并发数
    python -m scripts.backfill_meeting_topic --force      # 强制全部重跑（含已有 topic 的）
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认 GBK，打印 ✓/✗ 或某些字符会 UnicodeEncodeError 崩溃；强制 UTF-8 输出。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from meeting_platform.core.models import MeetingStatus, display_title  # noqa: E402
from meeting_platform.core.store import MeetingStore  # noqa: E402
from meeting_platform.analysis.analyzer import analyze_meeting  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _has_topic(m) -> bool:
    return bool(m.analysis and (m.analysis.meeting_topic or "").strip())


def _analyze_one(meeting):
    """仅做 LLM 分析（慢、I/O 密集），不碰数据库。返回 (meeting, elapsed) 或抛异常。"""
    t0 = time.time()
    analyze_meeting(meeting)  # 短则整体、长则 map-reduce，自动写 meeting.analysis / status
    return meeting, time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", "--jobs", type=int, default=5, help="并发数（默认 5）")
    ap.add_argument("--force", action="store_true", help="强制全部重跑（忽略已有 topic）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全部）")
    ap.add_argument("--store", default=os.getenv("MEETING_STORE_PATH", "data/meetings.db"))
    args = ap.parse_args()

    store = MeetingStore(args.store)
    meetings = store.list()
    candidates = [
        m for m in meetings
        if m.status == MeetingStatus.ANALYZED and m.raw_content and m.raw_content.strip()
    ]
    if args.force:
        targets = candidates
    else:
        targets = [m for m in candidates if not _has_topic(m)]
    if args.limit:
        targets = targets[: args.limit]

    print(f"待重跑：{len(targets)} 条（并发={args.jobs}, store={args.store}, force={args.force}）", flush=True)
    if not targets:
        print("无可处理会议。", flush=True)
        return 0

    ok = failed = 0
    start = time.time()
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futures = {ex.submit(_analyze_one, m): m.meeting_id for m in targets}
        for fut in as_completed(futures):
            mid = futures[fut]
            try:
                m, elapsed = fut.result()
                store.upsert(m)  # 主线程串行写库，避免 SQLite 锁冲突
                ok += 1
                topic = (m.analysis.meeting_topic or "").strip()
                print(f"[{ok + failed}/{len(targets)}] OK {elapsed:5.1f}s | {topic}", flush=True)
                print(f"            display={display_title(m)}", flush=True)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"[{ok + failed}/{len(targets)}] ERR {mid} -> {exc}", flush=True)

    print(f"\n完成：成功 {ok}，失败 {failed}，总耗时 {time.time() - start:.0f}s", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
