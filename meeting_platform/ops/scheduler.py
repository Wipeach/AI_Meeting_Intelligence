"""采集调度：每天凌晨定时爬取昨天的会议，并提供网页「实时更新」用的按需采集。

- ``MeetingScheduler``：常驻守护线程，每天 02:00 爬取昨天的腾讯会议（视频+纪要→OSS）并分析。
- ``EmailSupervisionScheduler``：常驻守护线程，每天 05:00 对昨天已分析且未发送的会议发起邮件督办。
- ``CrawlRunner``：网页「实时更新」按钮调用，按需爬取今天的内容；后台线程执行 + 进度查询。
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from meeting_platform.analysis.analyzer import analyze_pending
from meeting_platform.core.models import MeetingSource
from meeting_platform.core.store import MeetingStore
from meeting_platform.ops.email_supervision import request_review_edits_for_range
from meeting_platform.ingest.feishu import FeishuMinutesClient, collect_feishu_minutes
from meeting_platform.ingest.feishu_bitable import backfill_supervision
from meeting_platform.ingest.feishu_oauth import get_feishu_oauth_service
from meeting_platform.ingest.oss import OSSUploader
from meeting_platform.ingest.tencent import collect_tencent_meetings

log = logging.getLogger("meeting_scheduler")

# 来源字符串 → 枚举
_SRC_MAP = {"tencent": MeetingSource.TENCENT_MEETING, "feishu": MeetingSource.FEISHU_MINUTES}

TZ = timezone(timedelta(hours=8))  # 东八区


def day_range(date_str: str) -> tuple:
    """'YYYYMMDD' → 该自然日 [00:00, 次日00:00) 的秒级时间戳（本地时区）。"""
    d = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=TZ)
    return int(d.timestamp()), int((d + timedelta(days=1)).timestamp())


def range_ts(start_str: str, end_str: str) -> tuple:
    """两个 'YYYYMMDD' → [start 00:00, end 次日00:00) 的秒级时间戳（闭区间含 end 当天全天）。"""
    s = datetime.strptime(start_str, "%Y%m%d").replace(tzinfo=TZ)
    e = datetime.strptime(end_str, "%Y%m%d").replace(tzinfo=TZ)
    if e < s:
        s, e = e, s
    return int(s.timestamp()), int((e + timedelta(days=1)).timestamp())


def _norm_day(value: str) -> str:
    """'YYYY-MM-DD' 或 'YYYYMMDD' → 'YYYYMMDD'；非法返回空串。"""
    cleaned = (value or "").strip().replace("-", "")
    if len(cleaned) == 8 and cleaned.isdigit():
        return cleaned
    return ""


def today_str() -> str:
    return datetime.now(TZ).strftime("%Y%m%d")


def yesterday_str() -> str:
    return (datetime.now(TZ) - timedelta(days=1)).strftime("%Y%m%d")


def yesterday_date_str() -> str:
    return (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")


def _default_base_url() -> str:
    """督办/邮件里链接回平台的 base URL。

    优先级：SUPERVISION_BASE_URL > MEETING_WEB_BASE_URL > 本地端口兜底。
    服务器部署务必设置 SUPERVISION_BASE_URL（如 https://ai.gemdalesh.cn），
    否则邮件链接会指向 127.0.0.1（仅本机可开）。
    """
    return (
        os.getenv("SUPERVISION_BASE_URL")
        or os.getenv("MEETING_WEB_BASE_URL")
        or f"http://127.0.0.1:{os.getenv('PORT', '3022')}"
    ).rstrip("/")


def _crawl_window(
    start_str: str, end_str: str, store_path: str, *, with_video: bool, limit: int,
    on_progress=None, on_phase=None,
) -> Dict[str, Any]:
    """爬取 [start, end] 闭区间内各自然日的录制并分析。"""
    start_ts, end_ts = range_ts(start_str, end_str)
    store = MeetingStore(store_path)
    result = collect_tencent_meetings(
        store,
        start_ts=start_ts,
        end_ts=end_ts,
        limit=limit,
        with_video=with_video,
        uploader=OSSUploader(),
        on_progress=on_progress,
        on_phase=on_phase,
    )
    if on_phase:
        on_phase("AI 分析中")

    def _ana_progress(done: int, total: int) -> None:
        if on_phase and total:
            on_phase(f"AI 分析中 {done}/{total}")

    analysis = analyze_pending(store, on_progress=_ana_progress)
    if analysis.get("failed"):
        log.warning("[腾讯] 分析失败 %d 条：%s", analysis["failed"],
                    [i.get("error") for i in analysis.get("items", []) if i.get("error")])
    log.info("[腾讯] 分析完成：分析 %d，跳过(无原文) %d，失败 %d",
             analysis.get("analyzed", 0), analysis.get("skipped", 0), analysis.get("failed", 0))
    window = start_str if start_str == end_str else f"{start_str}~{end_str}"
    return {"date": window, **result, "analyzed": analysis["analyzed"], "analysis_failed": analysis.get("failed", 0)}


def _feishu_crawl_window(
    start_str: str, end_str: str, store_path: str, *,
    user_keys=None, with_media: bool = True,
    on_progress=None, on_phase=None,
) -> Dict[str, Any]:
    """飞书妙记采集 [start, end] 闭区间并分析（授权用户名下妙记 → 文字+录音→OSS→入库）。"""
    start_ts, end_ts = range_ts(start_str, end_str)
    store = MeetingStore(store_path)
    oauth_service = get_feishu_oauth_service()
    result = collect_feishu_minutes(
        store,
        oauth_service=oauth_service,
        client=FeishuMinutesClient(),
        uploader=OSSUploader(),
        start_ts=start_ts,
        end_ts=end_ts,
        user_keys=user_keys,
        with_media=with_media,
        on_progress=on_progress,
        on_phase=on_phase,
    )
    if on_phase:
        on_phase("AI 分析中")

    def _ana_progress(done: int, total: int) -> None:
        if on_phase and total:
            on_phase(f"AI 分析中 {done}/{total}")

    analysis = analyze_pending(store, on_progress=_ana_progress)
    if analysis.get("failed"):
        log.warning("[飞书] 分析失败 %d 条：%s", analysis["failed"],
                    [i.get("error") for i in analysis.get("items", []) if i.get("error")])
    log.info("[飞书] 分析完成：分析 %d，跳过(无原文) %d，失败 %d",
             analysis.get("analyzed", 0), analysis.get("skipped", 0), analysis.get("failed", 0))
    window = start_str if start_str == end_str else f"{start_str}~{end_str}"
    return {"date": window, "source": "feishu", **result, "analyzed": analysis["analyzed"], "analysis_failed": analysis.get("failed", 0)}


class CrawlRunner:
    """网页按需采集（指定日期范围，默认今天），后台线程执行 + 进度查询。"""

    def __init__(self, store_path: str, *, with_video: bool = True, limit: int = 2000):
        self.store_path = store_path
        self.with_video = with_video
        self.limit = limit
        self.running = False
        self.last: Optional[Dict[str, Any]] = None
        self.progress = {"done": 0, "total": 0}
        self.phase = ""
        self._lock = threading.Lock()

    def start_today(self) -> bool:
        """采集今天（保持向后兼容）。"""
        return self.start_range(today_str(), today_str())

    def start_date(self, date_str: str) -> bool:
        """采集指定自然日（单日 = 起止相同的范围）。"""
        cleaned = _norm_day(date_str) or today_str()
        return self.start_range(cleaned, cleaned)

    def start_range(self, start_str: str, end_str: str) -> bool:
        """采集 [start, end] 闭区间（'YYYYMMDD'）。非法/空回落到今天。"""
        s = _norm_day(start_str)
        e = _norm_day(end_str)
        if not s and not e:
            s = e = today_str()
        elif not s:
            s = e
        elif not e:
            e = s
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.progress = {"done": 0, "total": 0}
            self.phase = "启动中"
            self.last = None
        threading.Thread(target=self._work, args=(s, e), daemon=True).start()
        return True

    def status(self) -> Dict[str, Any]:
        return {"running": self.running, "last": self.last, "progress": self.progress, "phase": self.phase}

    def _work(self, start_str: str, end_str: str) -> None:
        def _cb(done: int, total: int) -> None:
            self.progress = {"done": done, "total": total}

        def _phase(name: str) -> None:
            self.phase = name

        try:
            self.last = _crawl_window(
                start_str, end_str, self.store_path, with_video=self.with_video, limit=self.limit,
                on_progress=_cb, on_phase=_phase,
            )
            log.info("按需采集完成：%s", self.last)
        except Exception as exc:  # noqa: BLE001
            log.exception("按需采集失败")
            self.last = {"error": str(exc)}
        finally:
            self.running = False
            self.phase = ""


class FeishuCrawlRunner:
    """网页飞书按需采集（日期范围 + 指定用户，默认全部），后台线程 + 进度查询。"""

    def __init__(self, store_path: str, *, with_media: bool = True):
        self.store_path = store_path
        self.with_media = with_media
        self.running = False
        self.last: Optional[Dict[str, Any]] = None
        self.progress = {"done": 0, "total": 0}
        self.phase = ""
        self._lock = threading.Lock()

    def start_range(self, start_str: str, end_str: str, user_keys=None) -> bool:
        s = _norm_day(start_str)
        e = _norm_day(end_str)
        if not s and not e:
            s = e = today_str()
        elif not s:
            s = e
        elif not e:
            e = s
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.progress = {"done": 0, "total": 0}
            self.phase = "启动中"
            self.last = None
        threading.Thread(target=self._work, args=(s, e, user_keys), daemon=True).start()
        return True

    def status(self) -> Dict[str, Any]:
        return {"running": self.running, "last": self.last, "progress": self.progress, "phase": self.phase}

    def _work(self, start_str: str, end_str: str, user_keys=None) -> None:
        def _cb(done: int, total: int) -> None:
            self.progress = {"done": done, "total": total}

        def _phase(name: str) -> None:
            self.phase = name

        try:
            self.last = _feishu_crawl_window(
                start_str, end_str, self.store_path, user_keys=user_keys,
                with_media=self.with_media, on_progress=_cb, on_phase=_phase,
            )
            log.info("飞书按需采集完成：%s", self.last)
        except Exception as exc:  # noqa: BLE001
            log.exception("飞书按需采集失败")
            self.last = {"error": str(exc)}
        finally:
            self.running = False
            self.phase = ""


class UnifiedCrawlRunner:
    """统一按需采集（主仪表盘用）：按来源选择跑腾讯/飞书/两者，后台线程 + 进度查询。"""

    def __init__(self, store_path: str, *, with_video: bool = True):
        self.store_path = store_path
        self.with_video = with_video
        self.running = False
        self.last: Optional[Dict[str, Any]] = None
        self.progress = {"done": 0, "total": 0}
        self.phase = ""
        self._lock = threading.Lock()

    def start(self, start_str: str, end_str: str, sources) -> bool:
        s = _norm_day(start_str)
        e = _norm_day(end_str)
        if not s and not e:
            s = e = today_str()
        elif not s:
            s = e
        elif not e:
            e = s
        srcs = [x for x in (sources or []) if x in ("tencent", "feishu")] or ["tencent", "feishu"]
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.progress = {"done": 0, "total": 0}
            self.phase = "启动中"
            self.last = None
        threading.Thread(target=self._work, args=(s, e, srcs), daemon=True).start()
        return True

    def status(self) -> Dict[str, Any]:
        return {"running": self.running, "last": self.last, "progress": self.progress, "phase": self.phase}

    def _work(self, start_str: str, end_str: str, sources) -> None:
        def _cb(done: int, total: int) -> None:
            self.progress = {"done": done, "total": total}

        def _phase(name: str) -> None:
            self.phase = name

        acc = {"created": 0, "skipped": 0, "failed": 0, "analyzed": 0, "sources": []}
        try:
            if "tencent" in sources:
                _phase("腾讯 采集中")
                r = _crawl_window(start_str, end_str, self.store_path,
                                  with_video=self.with_video, limit=2000,
                                  on_progress=_cb, on_phase=_phase)
                acc["created"] += r.get("created", 0)
                acc["skipped"] += r.get("skipped", 0)
                acc["failed"] += r.get("failed", 0)
                acc["analyzed"] += r.get("analyzed", 0)
                acc["sources"].append({"source": "tencent", **{k: r.get(k) for k in ("created", "skipped", "failed", "analyzed")}})
            if "feishu" in sources:
                _phase("飞书 采集中")
                rf = _feishu_crawl_window(start_str, end_str, self.store_path,
                                          with_media=self.with_video, on_progress=_cb, on_phase=_phase)
                acc["created"] += rf.get("created", 0)
                acc["skipped"] += rf.get("skipped", 0)
                acc["failed"] += rf.get("failed", 0)
                acc["analyzed"] += rf.get("analyzed", 0)
                acc["sources"].append({"source": "feishu", **{k: rf.get(k) for k in ("created", "skipped", "failed", "analyzed")}})
            self.last = {"date": start_str if start_str == end_str else f"{start_str}~{end_str}", **acc}
            log.info("统一采集完成：%s", self.last)
        except Exception as exc:  # noqa: BLE001
            log.exception("统一采集失败")
            self.last = {"error": str(exc)}
        finally:
            self.running = False
            self.phase = ""


class SupervisionRunner:
    """网页「督办该事项」手动触发：回填 AI 待办到飞书督办表，后台线程 + 进度查询。"""

    def __init__(self, store_path: str):
        self.store_path = store_path
        self.running = False
        self.last: Optional[Dict[str, Any]] = None
        self.progress = {"done": 0, "total": 0}
        self.phase = ""
        self._lock = threading.Lock()

    def start(self, source: Optional[str] = None) -> bool:
        """source: 'tencent' / 'feishu' / None(全部)。"""
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.progress = {"done": 0, "total": 0}
            self.phase = "启动中"
            self.last = None
        threading.Thread(target=self._work, args=(source,), daemon=True).start()
        return True

    def status(self) -> Dict[str, Any]:
        return {"running": self.running, "last": self.last, "progress": self.progress, "phase": self.phase}

    def _work(self, source: Optional[str]) -> None:
        store = MeetingStore(self.store_path)
        src = _SRC_MAP.get(source) if source else None

        def _cb(done: int, total: int) -> None:
            self.progress = {"done": done, "total": total}

        def _phase(name: str) -> None:
            self.phase = name

        try:
            self.last = backfill_supervision(store, source=src, on_progress=_cb, on_phase=_phase)
            log.info("督办回填完成：%s", self.last)
        except Exception as exc:  # noqa: BLE001
            log.exception("督办回填失败")
            self.last = {"error": str(exc)}
        finally:
            self.running = False
            self.phase = ""


class MeetingScheduler:
    """每天定时爬取昨天的会议并分析。在 Web 服务进程内常驻。"""

    def __init__(self, store_path: str, *, hour: int = 2, minute: int = 0, with_video: bool = True, limit: int = 2000):
        # limit：录制列表安全上限。企业版单日录制可达 600+，曾用 500 截断导致
        # 位置 500 之后的录制静默丢失（2026-10-27 丢 3 条），与手动采集统一为 2000。
        self.store_path = store_path
        self.hour = hour
        self.minute = minute
        self.with_video = with_video
        self.limit = limit
        self._started = False
        self._stop = threading.Event()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, name="meeting-scheduler", daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(TZ)
            next_run = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            wait = (next_run - now).total_seconds()
            log.info("下次定时采集：%s（约 %.0f 秒后）", next_run.strftime("%Y-%m-%d %H:%M"), wait)
            if self._stop.wait(wait):
                break
            try:
                date_str = yesterday_str()
                log.info("开始定时采集昨天：%s（腾讯）", date_str)
                _crawl_window(date_str, date_str, self.store_path, with_video=self.with_video, limit=self.limit)
            except Exception:  # noqa: BLE001
                log.exception("定时采集（腾讯）失败")
            # 飞书妙记：爬昨天所有授权用户（失败不影响腾讯侧结果）
            try:
                log.info("开始定时采集昨天：%s（飞书）", date_str)
                _feishu_crawl_window(date_str, date_str, self.store_path)
            except Exception:  # noqa: BLE001
                log.exception("定时采集（飞书）失败")
            # 督办写入需要用户在网页编辑确认后手动发送，定时任务不自动写入。

            # 采集+分析完成后，自动把昨天会议同步到 Notion（失败不影响次日采集）
            # 注意：sync_day_to_notion 需要 "%Y-%m-%d"（带横杠）格式——既用作 Notion 日期页标题，
            # 又要和 start_time(ISO "2026-08-11T...") 做字符串比较筛当天会议。
            # 不能用上面的 date_str("%Y%m%d")，否则页名变 20260811 且 store.list 筛不到当天会议。
            try:
                from meeting_platform.ops.notion_sync import sync_day_to_notion
                notion_date = yesterday_date_str()  # "%Y-%m-%d"
                log.info("开始定时 Notion 同步：%s", notion_date)
                res = sync_day_to_notion(MeetingStore(self.store_path), notion_date)
                log.info("定时 Notion 同步完成：%s",
                         {k: v for k, v in res.items() if k != "items"})
            except Exception:  # noqa: BLE001
                log.exception("定时 Notion 同步失败")


class EmailSupervisionScheduler:
    """每天定时对昨天会议发起邮件督办，只发送填写链接，不直接写入飞书督办表。"""

    def __init__(self, store_path: str, *, hour: int = 5, minute: int = 0, base_url: str = ""):
        self.store_path = store_path
        self.hour = hour
        self.minute = minute
        self.base_url = (base_url or _default_base_url()).rstrip("/")
        self._started = False
        self._stop = threading.Event()
        self.last: Optional[Dict[str, Any]] = None

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, name="email-supervision-scheduler", daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(TZ)
            next_run = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            wait = (next_run - now).total_seconds()
            log.info("下次定时邮件督办：%s（约 %.0f 秒后）", next_run.strftime("%Y-%m-%d %H:%M"), wait)
            if self._stop.wait(wait):
                break
            date_str = yesterday_date_str()
            try:
                log.info("开始定时发起昨天会议邮件督办：%s", date_str)
                self.last = request_review_edits_for_range(
                    MeetingStore(self.store_path),
                    date_from=date_str,
                    date_to=date_str,
                    sources=[MeetingSource.TENCENT_MEETING, MeetingSource.FEISHU_MINUTES],
                    base_url=self.base_url,
                )
                self.last["date"] = date_str
                self.last["base_url"] = self.base_url
                log.info("定时邮件督办完成：%s", self.last)
            except Exception as exc:  # noqa: BLE001
                log.exception("定时邮件督办失败")
                self.last = {"date": date_str, "base_url": self.base_url, "error": str(exc)}
