"""把当天会议（纪要原文 + AI 智能纪要）同步到 Notion。

每天 23:30 由 ``NotionSyncScheduler`` 触发，也可由网页/CLI 手动触发补跑。

页面结构（父页面 = NOTION_PARENT_PAGE_ID）::

  固定父页面
  └─ 2026-08-11            ← 日期 page（每天一个）
     ├─ 【腾讯会议】<会议名> - 纪要原文
     ├─ 【腾讯会议】<会议名> - AI摘要
     ├─ 【飞书妙记】<会议名> - 纪要原文
     └─ 【飞书妙记】<会议名> - AI摘要

- 每场会议拆成 2 个 page：纪要原文（纯文本段落）+ AI摘要（markdown→结构化 block）。
- 纪要原文优先上传术语纠错后的 corrected_content（含确定类替换）；未分析或纠错不可用时回退 raw_content。页面命名不变。
- 日期 page 复用；同一天重跑时，已存在的同标题会议 page 先归档再重建（覆盖重写）。
- 会议名 = 展示标题（原标题_AI议题名）；同名同日会议由日期 page 内位置区分。
- 没有纪要原文的会议跳过；有原文无 AI摘要只写纪要原文。
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from meeting_platform.core.common import call_with_retry, load_config
from meeting_platform.ingest import notion_service as ns
from notion_client import Client

from meeting_platform.core.models import MeetingSource, display_title
from meeting_platform.core.store import MeetingStore

log = logging.getLogger("notion_sync")

TZ = timezone(timedelta(hours=8))  # 东八区

_SRC_LABEL = {
    MeetingSource.TENCENT_MEETING: "腾讯会议",
    MeetingSource.FEISHU_MINUTES: "飞书妙记",
}


def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _iter_days(date_from: str, date_to: str) -> List[str]:
    """[date_from, date_to] 闭区间逐天枚举，格式 YYYY-MM-DD。"""
    d0 = datetime.strptime(date_from, "%Y-%m-%d")
    d1 = datetime.strptime(date_to, "%Y-%m-%d")
    days: List[str] = []
    cur = d0
    while cur <= d1:
        days.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return days


# --------------------------- 命名 ---------------------------

def _source_label(source: MeetingSource) -> str:
    return _SRC_LABEL.get(source, getattr(source, "value", "未知"))


def _meeting_name(meeting) -> str:
    """会议显示名 = 展示标题（原标题_AI议题名）。不再追加时间/ID 后缀。"""
    return (display_title(meeting).strip() or "未命名")[:90]


def _page_title(meeting, kind: str) -> str:
    """kind: '纪要原文' / 'AI摘要'。命名 = 【来源】会议名 - 类型。"""
    return f"【{_source_label(meeting.source)}】{_meeting_name(meeting)} - {kind}"


# --------------------------- Notion 子页面查找 ---------------------------

def _list_child_pages(notion: Client, block_id: str, config) -> List[Dict[str, str]]:
    """列出某 page 下所有 child_page（id + title），自动翻页。"""
    pages: List[Dict[str, str]] = []
    cursor = None
    while True:
        resp = call_with_retry(
            "list child pages",
            config.retry_max_attempts,
            config.retry_base_delay_sec,
            lambda cursor=cursor: notion.blocks.children.list(
                block_id=block_id, start_cursor=cursor, page_size=100
            ),
        )
        for b in resp.get("results", []):
            if b.get("type") == "child_page":
                pages.append({"id": b["id"], "title": (b.get("child_page") or {}).get("title", "")})
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return pages


def _find_page_by_title(notion: Client, parent_id: str, title: str, config):
    for p in _list_child_pages(notion, parent_id, config):
        if p["title"].strip() == title.strip():
            return p["id"]
    return None


def _archive_page(notion: Client, page_id: str, config) -> None:
    call_with_retry(
        "archive page",
        config.retry_max_attempts,
        config.retry_base_delay_sec,
        lambda: notion.pages.update(page_id, archived=True),
    )


def _create_child_page(notion: Client, parent_id: str, title: str, config) -> Dict[str, Any]:
    title = (title or "").strip() or "Untitled"
    return call_with_retry(
        "create child page",
        config.retry_max_attempts,
        config.retry_base_delay_sec,
        lambda: notion.pages.create(
            parent={"type": "page_id", "page_id": parent_id},
            properties={"title": {"title": ns.text_rich(title)}},
        ),
    )


def _ensure_date_page(notion: Client, parent_id: str, date_str: str, config) -> str:
    """找/建当天日期 page，返回 page_id。"""
    existing = _find_page_by_title(notion, parent_id, date_str, config)
    if existing:
        log.info("复用日期 page：%s (%s)", date_str, existing)
        return existing
    page = _create_child_page(notion, parent_id, date_str, config)
    log.info("新建日期 page：%s (%s)", date_str, page["id"])
    return page["id"]


# --------------------------- 写入 ---------------------------

def _write_transcript(notion: Client, page_id: str, raw: str, config) -> None:
    """纪要原文 → 纯文本段落 block。"""
    if not raw.strip():
        return
    ns.append_text_blocks_to_page(notion, page_id, raw, config)


def _write_summary(notion: Client, page_id: str, summary_md: str, config) -> None:
    """AI摘要(markdown) → 结构化 block。"""
    if not summary_md.strip():
        return
    ns.append_markdown_blocks_to_page(notion, page_id, summary_md, config)


# --------------------------- 主流程 ---------------------------

def sync_day_to_notion(
    store: MeetingStore,
    date_str: str,
    *,
    notion: Optional[Client] = None,
    parent_id: Optional[str] = None,
    on_progress=None,
    on_phase=None,
) -> Dict[str, Any]:
    """把指定日期的所有会议同步到 Notion。

    - date_str: 'YYYY-MM-DD'。
    - 取该日所有【有纪要原文】的会议。
    """
    config = load_config()
    notion = notion or ns.build_notion_client()
    parent_id = (parent_id or os.getenv("NOTION_PARENT_PAGE_ID", "")).strip()
    if not parent_id:
        raise RuntimeError("缺少 NOTION_PARENT_PAGE_ID")

    meetings = [
        m for m in store.list(date_from=date_str, date_to=date_str)
        if m.raw_content and m.raw_content.strip()
    ]
    if not meetings:
        # 当天没有纪要原文：不创建空日期页，直接返回
        log.info("%s 无纪要原文，跳过 Notion 同步", date_str)
        return {
            "date": date_str, "parent_page_id": parent_id, "date_page_id": None,
            "meetings_total": 0, "meetings_written": 0, "pages_created": 0,
            "failed": 0, "items": [],
        }
    if on_phase:
        on_phase(f"同步 {date_str}（{len(meetings)} 场）")

    date_page_id = _ensure_date_page(notion, parent_id, date_str, config)

    written = 0
    pages_created = 0
    items: List[Dict[str, Any]] = []
    failed = 0
    total = len(meetings)

    for idx, m in enumerate(meetings, 1):
        try:
            # 覆盖重写：先归档同标题的旧 page
            for kind in ("纪要原文", "AI摘要"):
                old = _find_page_by_title(notion, date_page_id, _page_title(m, kind), config)
                if old:
                    _archive_page(notion, old, config)

            rec: Dict[str, Any] = {
                "meeting_id": m.meeting_id,
                "title": m.title,
                "source": _source_label(m.source),
                "raw_page_id": None,
                "summary_page_id": None,
            }

            # 1) 纪要原文（优先纠错后文本；未分析/纠错不可用时回退原文）
            raw_title = _page_title(m, "纪要原文")
            raw_page = _create_child_page(notion, date_page_id, raw_title, config)
            rec["raw_page_id"] = raw_page["id"]
            _write_transcript(notion, raw_page["id"], m.corrected_content or m.raw_content, config)
            pages_created += 1

            # 2) AI摘要（有才写）
            summary = m.analysis.summary if m.analysis else ""
            if summary and summary.strip():
                sum_title = _page_title(m, "AI摘要")
                sum_page = _create_child_page(notion, date_page_id, sum_title, config)
                rec["summary_page_id"] = sum_page["id"]
                _write_summary(notion, sum_page["id"], summary, config)
                pages_created += 1
            else:
                rec["summary_skipped"] = True

            written += 1
            items.append(rec)
            log.info("[%d/%d] %s 原文✓ 摘要%s", idx, total, m.title[:24],
                     "✓" if rec.get("summary_page_id") else "—")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            items.append({"meeting_id": m.meeting_id, "title": m.title, "error": str(exc)[:160]})
            log.exception("同步会议失败：%s", m.meeting_id)

        if on_progress:
            on_progress(idx, total)

    result = {
        "date": date_str,
        "parent_page_id": parent_id,
        "date_page_id": date_page_id,
        "meetings_total": total,
        "meetings_written": written,
        "pages_created": pages_created,
        "failed": failed,
        "items": items,
    }
    log.info("Notion 同步完成：%s", {k: v for k, v in result.items() if k != "items"})
    return result


# --------------------------- 手动触发 Runner（网页用）---------------------------

class NotionSyncRunner:
    """网页「同步到 Notion」手动触发：指定日期范围（默认今天），后台线程 + 进度查询。"""

    def __init__(self, store_path: str):
        self.store_path = store_path
        self.running = False
        self.last: Optional[Dict[str, Any]] = None
        self.progress = {"done": 0, "total": 0}
        self.phase = ""
        self._lock = threading.Lock()

    def start(self, date_from: Optional[str] = None, date_to: Optional[str] = None) -> bool:
        df = (date_from or "").strip() or today_str()
        dt = (date_to or "").strip() or df
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.progress = {"done": 0, "total": 0}
            self.phase = "启动中"
            self.last = None
        threading.Thread(target=self._work, args=(df, dt), daemon=True).start()
        return True

    def status(self) -> Dict[str, Any]:
        return {"running": self.running, "last": self.last,
                "progress": self.progress, "phase": self.phase}

    def _work(self, date_from: str, date_to: str) -> None:
        agg: Dict[str, Any] = {"days": [], "meetings_written": 0, "pages_created": 0,
                               "failed": 0, "per_day": []}
        try:
            days = _iter_days(date_from, date_to)  # 格式非法会抛 ValueError，必须在 try 内
            agg["days"] = days
            for di, ds in enumerate(days, 1):
                self.phase = f"{ds}（第 {di}/{len(days)} 天）"

                def _cb(done: int, total: int, _ds=ds):
                    self.progress = {"done": done, "total": total}

                def _phase(name: str, _ds=ds):
                    self.phase = f"{_ds}（第 {di}/{len(days)} 天）· {name}"

                res = sync_day_to_notion(
                    MeetingStore(self.store_path), ds,
                    on_progress=_cb, on_phase=_phase,
                )
                agg["meetings_written"] += res.get("meetings_written", 0)
                agg["pages_created"] += res.get("pages_created", 0)
                agg["failed"] += res.get("failed", 0)
                agg["per_day"].append({
                    "date": ds,
                    "meetings_written": res.get("meetings_written", 0),
                    "pages_created": res.get("pages_created", 0),
                    "failed": res.get("failed", 0),
                    "error": res.get("error"),
                })
            self.last = agg
            log.info("手动 Notion 同步完成：%s", {k: v for k, v in agg.items() if k != "per_day"})
        except Exception as exc:  # noqa: BLE001
            log.exception("手动 Notion 同步失败")
            self.last = {"days": agg.get("days", []), "error": str(exc)}
        finally:
            self.running = False
            self.phase = ""


# --------------------------- 定时调度 ---------------------------

class NotionSyncScheduler:
    """每天 23:30 把当天的会议同步到 Notion。在 Web 服务进程内常驻。"""

    def __init__(self, store_path: str, *, hour: int = 23, minute: int = 30):
        self.store_path = store_path
        self.hour = hour
        self.minute = minute
        self._started = False
        self._stop = threading.Event()
        self.last: Optional[Dict[str, Any]] = None

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, name="notion-sync-scheduler", daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(TZ)
            next_run = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            wait = (next_run - now).total_seconds()
            log.info("下次 Notion 同步：%s（约 %.0f 秒后）", next_run.strftime("%Y-%m-%d %H:%M"), wait)
            if self._stop.wait(wait):
                break
            date_str = today_str()
            try:
                log.info("开始定时 Notion 同步：%s", date_str)
                self.last = sync_day_to_notion(MeetingStore(self.store_path), date_str)
                self.last["date"] = date_str
                log.info("定时 Notion 同步完成：%s", self.last)
            except Exception as exc:  # noqa: BLE001
                log.exception("定时 Notion 同步失败")
                self.last = {"date": date_str, "error": str(exc)}
