"""飞书妙记（Minutes）采集：搜索 → 元信息 → 文字记录 → 录音 → 入统一库（移植自 feishu_oauth_wh）。

每条妙记转成统一 ``Meeting``（source=FEISHU_MINUTES, source_ref=minute_token），
录音/文字传 OSS（统一命名 会议纪要/{会议日期}/{stem}_录制.{ext} 与 _纪要.txt），
按 minute_token 幂等去重（复用 MeetingStore）。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from meeting_platform.core.models import MeetingSource
from meeting_platform.core.store import MeetingStore
from meeting_platform.ingest.adapters import build_meeting_from_feishu_minutes
from meeting_platform.ingest.feishu_oauth import FeishuOAuthService
from meeting_platform.ingest.oss import OSSUploader
from meeting_platform.ingest.text_extract import extract_text

log = logging.getLogger("feishu_ingest")

CN_TZ = timezone(timedelta(hours=8))

MINUTES_SEARCH_URL = "https://open.feishu.cn/open-apis/minutes/v1/minutes/search"
MINUTES_BASE_URL = "https://open.feishu.cn/open-apis/minutes/v1/minutes"


class FeishuMinutesClient:
    """飞书妙记 API：搜索、元信息、导出文字记录、下载录音。全程用 user_access_token。"""

    MAX_SEARCH_PAGE_SIZE = 30

    def __init__(self):
        pass

    # ----- 搜索某用户名下妙记 -----

    def search_minutes_by_owner(
        self, *, access_token: str, owner_id: str,
        start_time_iso: Optional[str] = None, end_time_iso: Optional[str] = None,
        max_pages: int = 5,
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page_token = ""
        for _ in range(max_pages):
            body: Dict[str, Any] = {"sorter": "create_time_desc"}
            flt: Dict[str, Any] = {"owner_ids": [owner_id]} if owner_id else {}
            if start_time_iso or end_time_iso:
                flt["create_time"] = {}
                if start_time_iso:
                    flt["create_time"]["start_time"] = start_time_iso
                if end_time_iso:
                    flt["create_time"]["end_time"] = end_time_iso
            if flt:
                body["filter"] = flt
            params = {"page_size": self.MAX_SEARCH_PAGE_SIZE, "user_id_type": "open_id"}
            if page_token:
                params["page_token"] = page_token
            result = self._request_json("post", MINUTES_SEARCH_URL,
                                        access_token=access_token, params=params, json_body=body)
            data = result.get("data", {}) or {}
            for item in data.get("items") or []:
                items.append({"token": item.get("token")})
            if not data.get("has_more"):
                break
            page_token = data.get("page_token") or ""
            if not page_token:
                break
        return items

    # ----- 单条元信息 -----

    def get_minute_meta(self, *, access_token: str, minute_token: str) -> Dict[str, Any]:
        result = self._request_json("get", f"{MINUTES_BASE_URL}/{minute_token}",
                                    access_token=access_token, params={"user_id_type": "open_id"})
        minute = (result.get("data") or {}).get("minute") or {}
        return {
            "token": minute.get("token") or minute_token,
            "owner_id": minute.get("owner_id"),
            "create_time": minute.get("create_time"),  # 毫秒时间戳字符串
            "title": minute.get("title"),
            "duration": minute.get("duration"),
            "url": minute.get("url"),
        }

    # ----- 文字记录 -----

    def get_transcript(self, *, access_token: str, minute_token: str,
                       file_format: str = "txt", need_speaker: bool = True) -> bytes:
        resp = requests.get(
            f"{MINUTES_BASE_URL}/{minute_token}/transcript",
            headers=self._auth_headers(access_token),
            params={"file_format": file_format, "need_speaker": str(need_speaker).lower()},
            timeout=60,
        )
        self._raise_if_json_error(resp, label="transcript")
        return resp.content

    # ----- 录音/录像 -----

    def get_media_download_url(self, *, access_token: str, minute_token: str) -> str:
        result = self._request_json("get", f"{MINUTES_BASE_URL}/{minute_token}/media",
                                    access_token=access_token)
        download_url = (result.get("data") or {}).get("download_url")
        if not download_url:
            raise RuntimeError(f"飞书 media 接口未返回 download_url: {result}")
        return download_url

    def download_media(self, download_url: str) -> Tuple[bytes, str]:
        resp = requests.get(download_url, timeout=120)
        if resp.status_code != 200:
            raise RuntimeError(f"下载飞书录音失败：HTTP={resp.status_code}, body={resp.text[:200]}")
        ext = self._ext_from_content_type(resp.headers.get("Content-Type", ""))
        return resp.content, ext

    # ----- 内部 -----

    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=utf-8"}

    def _request_json(self, method: str, url: str, *, access_token: str,
                      params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = requests.request(method, url, headers=self._auth_headers(access_token),
                                params=params, json=json_body, timeout=30)
        try:
            result = resp.json()
        except ValueError:
            raise RuntimeError(f"飞书妙记接口返回非 JSON：HTTP={resp.status_code}, body={resp.text[:200]}")
        if resp.status_code != 200 or result.get("code") != 0:
            raise RuntimeError(f"飞书妙记接口失败：HTTP={resp.status_code}, result={result}")
        return result

    def _raise_if_json_error(self, resp: requests.Response, *, label: str) -> None:
        ct = resp.headers.get("Content-Type", "")
        if "json" not in ct and resp.status_code == 200:
            return
        try:
            result = resp.json()
        except ValueError:
            raise RuntimeError(f"{label} 下载失败：HTTP={resp.status_code}, body={resp.text[:200]}")
        raise RuntimeError(f"{label} 下载失败：HTTP={resp.status_code}, result={result}")

    @staticmethod
    def _ext_from_content_type(content_type: str) -> str:
        ct = content_type.lower()
        for needle, ext in [("audio/mp4", ".m4a"), ("audio/mpeg", ".mp3"), ("audio/ogg", ".ogg"),
                            ("audio/wav", ".wav"), ("audio/aac", ".aac"), ("video/mp4", ".mp4"),
                            ("video/quicktime", ".mov"), ("application/octet-stream", ".m4a")]:
            if needle in ct:
                return ext
        return ".m4a"


# ============================ 采集编排 ============================

def collect_feishu_minutes(
    store: MeetingStore,
    *,
    oauth_service: FeishuOAuthService,
    client: Optional[FeishuMinutesClient] = None,
    uploader: OSSUploader,
    start_ts: int,
    end_ts: int,
    user_keys: Optional[List[str]] = None,
    with_media: bool = True,
    on_progress: Optional[Callable[[int, int], None]] = None,
    on_phase: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """遍历授权用户 → 搜其名下妙记 → 文字+录音传 OSS → 入统一库（按 minute_token 去重）。"""
    client = client or FeishuMinutesClient()
    users = oauth_service.store.list_users()
    if user_keys:
        wanted = set(user_keys)
        users = {k: v for k, v in users.items() if k in wanted}
    if not users:
        return {"created": 0, "skipped": 0, "failed": 0, "by_user": [], "users": 0}

    start_iso = datetime.fromtimestamp(start_ts, CN_TZ).isoformat()
    end_iso = datetime.fromtimestamp(end_ts, CN_TZ).isoformat()

    created = skipped = failed = 0
    done = total = 0
    by_user: List[Dict[str, Any]] = []

    for idx, (user_key, record) in enumerate(users.items(), 1):
        user_info = record.get("user_info") or {}
        owner_id = user_info.get("open_id") or user_info.get("user_id") or user_key
        name = user_info.get("name") or user_info.get("en_name") or user_key
        if on_phase:
            on_phase(f"飞书采集 {idx}/{len(users)}：{name}")

        # 取可用 token
        try:
            session = oauth_service.ensure_valid_access_token(user_key)
            access_token = session["tokens"]["access_token"]
        except Exception as exc:  # noqa: BLE001
            failed += 1
            by_user.append({"user_key": user_key, "name": name, "ok": False, "error": str(exc),
                            "created": 0, "skipped": 0, "failed": 1})
            continue

        # 搜索
        try:
            minutes = client.search_minutes_by_owner(
                access_token=access_token, owner_id=owner_id,
                start_time_iso=start_iso, end_time_iso=end_iso, max_pages=5,
            )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            by_user.append({"user_key": user_key, "name": name, "ok": False, "error": str(exc),
                            "created": 0, "skipped": 0, "failed": 1})
            continue

        total += len(minutes)
        if on_progress:
            on_progress(done, total)

        u_created = u_skipped = u_failed = 0
        for minute in minutes:
            minute_token = minute.get("token")
            if not minute_token:
                u_skipped += 1
                continue
            # 平台幂等去重
            if store.find_by_source_ref(MeetingSource.FEISHU_MINUTES, str(minute_token)):
                u_skipped += 1
                done += 1
                if on_progress:
                    on_progress(done, total)
                continue
            try:
                meeting = _process_one_feishu(
                    client=client, access_token=access_token, minute_token=str(minute_token),
                    owner_name=name, uploader=uploader, with_media=with_media,
                )
                if meeting is None:
                    u_skipped += 1
                else:
                    store.upsert(meeting)
                    u_created += 1
            except Exception as exc:  # noqa: BLE001
                u_failed += 1
                log.warning("飞书妙记 %s 处理失败：%s", minute_token, exc)
            finally:
                done += 1
                if on_progress:
                    on_progress(done, total)
                time.sleep(0.25)  # 限频 5次/秒

        created += u_created
        skipped += u_skipped
        failed += u_failed
        by_user.append({"user_key": user_key, "name": name, "ok": True, "searched": len(minutes),
                        "created": u_created, "skipped": u_skipped, "failed": u_failed})

    return {"created": created, "skipped": skipped, "failed": failed, "by_user": by_user,
            "users": len(users)}


def _process_one_feishu(*, client: FeishuMinutesClient, access_token: str, minute_token: str,
                        owner_name: str, uploader: OSSUploader, with_media: bool = True):
    """下载一条妙记的文字+录音，传 OSS，构造统一 Meeting。无文字且无录音返回 None。"""
    meta = client.get_minute_meta(access_token=access_token, minute_token=minute_token)
    title = meta.get("title") or minute_token
    create_time_ms = meta.get("create_time")
    date_folder, start_time = _date_from_ms(create_time_ms)

    stem = _safe_stem(f"{owner_name}_{title}")

    # 1) 文字记录 → raw_content + OSS
    raw_content = ""
    content_url = ""
    try:
        tbytes = client.get_transcript(access_token=access_token, minute_token=minute_token)
        raw_content = extract_text(tbytes, "txt").strip()
        if raw_content and uploader:
            content_url = uploader.upload_bytes(
                raw_content.encode("utf-8"),
                uploader.prefixed_key(f"{date_folder}/{stem}_纪要.txt"),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("飞书妙记 %s 文字记录失败：%s", minute_token, exc)

    # 2) 录音 → OSS（流式直传，失败兜底 bytes）
    media_url = ""
    if with_media and uploader:
        try:
            media_url = _upload_media(
                client=client, access_token=access_token, minute_token=minute_token,
                uploader=uploader, key=uploader.prefixed_key(f"{date_folder}/{stem}_录制"),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("飞书妙记 %s 录音失败：%s", minute_token, exc)

    if not raw_content and not media_url:
        return None

    return build_meeting_from_feishu_minutes(
        doc_token=minute_token,
        title=title,
        start_time=start_time,
        owner=owner_name,
        participants=[],
        content_url=content_url,
        media_url=media_url,
        raw_content=raw_content,
    )


def _upload_media(*, client: FeishuMinutesClient, access_token: str, minute_token: str,
                  uploader: OSSUploader, key: str) -> str:
    """录音传 OSS：拿 download_url → 优先流式直传（不落本地），失败退回整包 bytes。"""
    download_url = client.get_media_download_url(access_token=access_token, minute_token=minute_token)
    # 直传：下载响应流直接灌进 OSS，按 Content-Type 推断扩展名拼到 key
    try:
        resp = requests.get(download_url, stream=True, timeout=120)
        resp.raise_for_status()
        ext = FeishuMinutesClient._ext_from_content_type(resp.headers.get("Content-Type", ""))
        full_key = key + ext
        resp.raw.decode_content = True
        try:
            return uploader.upload_stream(resp.raw, full_key)
        finally:
            resp.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("飞书录音直传失败，退回整包 %s：%s", minute_token, exc)
    # 兜底：整包下载到内存再传
    media_bytes, ext = client.download_media(download_url)
    return uploader.upload_bytes(media_bytes, key + ext)


def _date_from_ms(ms) -> Tuple[str, str]:
    """毫秒时间戳 → (YYYYMMDD 文件夹, YYYY-MM-DD start_time)。非法则用今天。"""
    try:
        if ms is None or ms == "":
            raise ValueError("empty")
        dt = datetime.fromtimestamp(int(ms) / 1000, CN_TZ)
        return dt.strftime("%Y%m%d"), dt.strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        today = datetime.now(CN_TZ)
        return today.strftime("%Y%m%d"), today.strftime("%Y-%m-%d")


def _safe_stem(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", str(value or "")).strip("_")
    return cleaned[:60] or "meeting"
