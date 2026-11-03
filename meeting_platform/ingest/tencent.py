"""腾讯会议采集器（多线程 + 自动重试 + 部门/会议号合并定向采集）。

采集范围 = 「DG 上海地区公司部门员工创建的会议」 ∪ 「指定会议号(会议室)的录制」，
两者合并去重。视频流式传 OSS，纪要转 txt，路径均为 会议纪要/{日期}/{文件名}。

环境变量：
    TENCENT_SECRET_ID/KEY/OPERATOR_ID/APP_ID/SDK_ID
    TENCENT_DEPARTMENT_PATTERN  部门正则，默认 ^DG.*上海地区公司.*$
    TENCENT_MEETING_CODES       会议号列表(逗号分隔)，默认内置 30 个会议室
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
import random
import re
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import requests

from meeting_platform.core.models import Meeting, MeetingSource, MeetingStatus
from meeting_platform.core.store import MeetingStore
from meeting_platform.ingest.adapters import build_meeting_from_tencent_meeting
from meeting_platform.ingest.oss import OSSUploader
from meeting_platform.ingest.text_extract import extract_text

log = logging.getLogger("tencent_ingest")

BASE_URL = "https://api.meeting.qq.com"

# 部门筛选正则（可由环境变量覆盖）
DEPARTMENT_PATTERN = re.compile(os.getenv("TENCENT_DEPARTMENT_PATTERN", r"^DG.*上海地区公司.*$"))

# 内置 30 个会议室会议号（来自 tencent_meeting_rooms.py），可由环境变量覆盖
_DEFAULT_CODES = (
    "4276856817,7466998032,5692820234,8268577910,4858685650,5503762067,9002734287,"
    "9113931644,7615367265,7402328651,7140641735,5296296800,6305418988,9393287934,"
    "6426569121,4507613083,6755265667,5652245237,9242971267,4342924969,5580561482,"
    "4540024687,6599787678,9236753950,8164703951,6700985635,5224677069,3569616727,"
    "4296252923,4112348623"
)
MEETING_CODES = {c.strip() for c in os.getenv("TENCENT_MEETING_CODES", _DEFAULT_CODES).split(",") if c.strip()}


@dataclass
class TencentConfig:
    secret_id: str = ""
    secret_key: str = ""
    operator_id: str = ""
    app_id: str = ""
    sdk_id: str = ""

    def __post_init__(self):
        self.secret_id = self.secret_id or os.getenv("TENCENT_SECRET_ID", "")
        self.secret_key = self.secret_key or os.getenv("TENCENT_SECRET_KEY", "")
        self.operator_id = self.operator_id or os.getenv("TENCENT_OPERATOR_ID", "")
        self.app_id = self.app_id or os.getenv("TENCENT_APP_ID", "")
        self.sdk_id = self.sdk_id or os.getenv("TENCENT_SDK_ID", "")

    def validate(self) -> None:
        missing = [n for n, v in {
            "TENCENT_SECRET_ID": self.secret_id,
            "TENCENT_SECRET_KEY": self.secret_key,
            "TENCENT_OPERATOR_ID": self.operator_id,
        }.items() if not v]
        if missing:
            raise RuntimeError(f"腾讯会议配置缺失：{', '.join(missing)}")


class TencentClient:
    """腾讯会议 REST API 客户端（官方签名 + 自动重试）。"""

    def __init__(self, config: Optional[TencentConfig] = None):
        self.config = config or TencentConfig()
        self.config.validate()

    # ------------------------------ 签名 ------------------------------

    @staticmethod
    def _sign(secret_id, secret_key, method, nonce, timestamp, uri, body=""):
        tobe_sig = (
            f"{method}\n"
            f"X-TC-Key={secret_id}&X-TC-Nonce={nonce}&X-TC-Timestamp={timestamp}\n"
            f"{uri}\n{body}"
        )
        digest = hmac.new(secret_key.encode("utf-8"), tobe_sig.encode("utf-8"), digestmod="sha256").hexdigest()
        return base64.b64encode(digest.encode("utf-8")).decode("utf-8")

    def _headers(self, signed_uri, method="GET", body=""):
        ts = str(int(time.time()))
        nonce = str(random.randint(100000, 999999999))
        sig = self._sign(self.config.secret_id, self.config.secret_key, method, nonce, ts, signed_uri, body)
        return {
            "Content-Type": "application/json",
            "X-TC-Key": self.config.secret_id,
            "X-TC-Timestamp": ts,
            "X-TC-Nonce": nonce,
            "X-TC-Signature": sig,
            "X-TC-Registered": "1",
            "AppId": self.config.app_id,
            "SdkId": self.config.sdk_id,
        }

    @staticmethod
    def _signed_uri(uri, params):
        return f"{uri}?{urllib.parse.urlencode(params)}"

    # ------------------------------ 请求（带重试） ------------------------------

    def _api_get(self, uri: str, params: Dict[str, Any], *, retries: int = 6) -> Dict[str, Any]:
        """签名 GET，429/5xx 指数退避自动重试；非临时错误立即抛出。"""
        last_error = None
        for retry in range(retries):
            try:
                resp = requests.get(
                    BASE_URL + uri,
                    headers=self._headers(self._signed_uri(uri, params)),
                    params=params,
                    timeout=30,
                )
                try:
                    data = resp.json()
                except Exception:  # noqa: BLE001
                    data = {"raw": resp.text}
                if resp.status_code == 200:
                    return data
                last_error = f"HTTP {resp.status_code}: {data}"
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"{uri} {last_error}")
            except requests.RequestException as exc:
                last_error = str(exc)
            time.sleep(min(2 ** retry + random.uniform(0, 1), 20))
        raise RuntimeError(f"{uri} 请求失败：{last_error}")

    # ------------------------------ 录制 ------------------------------

    def list_records(
        self, *, days: int = 7, page_size: int = 20, polite_sleep: float = 1.0,
        max_records: Optional[int] = None, start_ts: Optional[int] = None, end_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        start_time = start_ts if start_ts is not None else int((datetime.now() - timedelta(days=days)).timestamp())
        end_time = end_ts if end_ts is not None else int(time.time())
        uri = "/v1/corp/records"
        records: List[Dict[str, Any]] = []
        page = 1
        while True:
            data = self._api_get(uri, {
                "start_time": start_time, "end_time": end_time,
                "page": page, "page_size": page_size,
                "operator_id": self.config.operator_id, "operator_id_type": 1,
            })
            records.extend(data.get("record_meetings", []))
            if max_records and len(records) >= max_records:
                log.warning("录制列表达到 max_records=%d 上限被截断（该窗口实际不止 %d 条），"
                            "超出部分的录制将被跳过", max_records, max_records)
                return records[:max_records]
            cp = data.get("current_page", page)
            tp = data.get("total_page", 1)
            if cp >= tp:
                break
            page = cp + 1
            time.sleep(polite_sleep)
        return records

    def get_record_address(self, meeting_record_id: str) -> Dict[str, Any]:
        return self._api_get("/v1/addresses", {
            "meeting_record_id": meeting_record_id,
            "operator_id": self.config.operator_id, "operator_id_type": 1,
        })

    # ------------------------------ 部门 / 员工 ------------------------------

    def list_departments(self, *, page_size: int = 20) -> List[Dict[str, Any]]:
        depts: List[Dict[str, Any]] = []
        page = 1
        while True:
            data = self._api_get("/v1/departments", {
                "userid": self.config.operator_id, "page": page, "page_size": page_size,
            })
            depts.extend(data.get("department_list", []))
            cp = data.get("current_page", page)
            tp = data.get("total_page", 1)
            if cp >= tp:
                break
            page = cp + 1
        return depts

    def list_department_users(self, department_id: str, *, page_size: int = 20) -> List[Dict[str, Any]]:
        users: List[Dict[str, Any]] = []
        page = 1
        uri = f"/v1/departments/{department_id}/users"
        while True:
            data = self._api_get(uri, {
                "userid": self.config.operator_id, "page": page, "page_size": page_size,
            })
            users.extend(data.get("users") or data.get("user_list") or [])
            cp = data.get("current_page", page)
            tp = data.get("total_page", 1)
            if cp >= tp:
                break
            page = cp + 1
        return users

    def collect_department_userids(
        self, *, department_pattern=DEPARTMENT_PATTERN, max_workers: int = 5,
    ) -> Dict[str, str]:
        """返回 {userid: department_name}（多线程查部门员工，按 userid 去重）。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        targets = [d for d in self.list_departments() if department_pattern.fullmatch(d.get("department_name", ""))]
        result: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self.list_department_users, d["department_id"]): d for d in targets}
            for fut in as_completed(futures):
                dept = futures[fut]
                try:
                    for user in fut.result():
                        uid = user.get("userid")
                        if uid and uid not in result:
                            result[uid] = dept.get("department_name", "")
                except Exception:  # noqa: BLE001
                    continue
        return result

    def collect_department_users(
        self, *, department_pattern=DEPARTMENT_PATTERN, max_workers: int = 5,
    ) -> Dict[str, Dict[str, str]]:
        """返回 {userid: {"dept": 部门名, "name": 姓名}}。多线程查部门员工，按 userid 去重。

        部门用户接口返回 user_name（姓名，常带工号前缀如 DGDBF06燕丽），
        顺带建 userid→姓名 映射，供采集时把主持人/参会人的 userid 解析成真实姓名。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        targets = [d for d in self.list_departments() if department_pattern.fullmatch(d.get("department_name", ""))]
        result: Dict[str, Dict[str, str]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self.list_department_users, d["department_id"]): d for d in targets}
            for fut in as_completed(futures):
                dept = futures[fut]
                try:
                    for user in fut.result():
                        uid = user.get("userid")
                        if uid and uid not in result:
                            result[uid] = {
                                "dept": dept.get("department_name", ""),
                                "name": _clean_user_name(
                                    user.get("user_name") or user.get("username") or user.get("name") or ""
                                ),
                            }
                except Exception:  # noqa: BLE001
                    continue
        return result

    # ------------------------------ 下载 ------------------------------

    @staticmethod
    def download_bytes(url: str, *, timeout: int = 120) -> bytes:
        resp = requests.get(url, stream=True, timeout=timeout)
        resp.raise_for_status()
        return resp.content

    @staticmethod
    def download_to_file(url: str, path: str, *, timeout: int = 300) -> None:
        resp = requests.get(url, stream=True, timeout=timeout)
        resp.raise_for_status()
        with open(path, "wb") as handle:
            for chunk in resp.iter_content(1024 * 1024):
                if chunk:
                    handle.write(chunk)


# ---------------------------------------------------------------------------
# 采集编排：两条独立管线（会议号 / 部门）并发跑，最后合并去重
# ---------------------------------------------------------------------------


class _Progress:
    """跨两条管线的聚合进度（线程安全）。"""

    def __init__(self, cb: Optional[Callable[[int, int], None]]):
        self.cb = cb
        self.done = 0
        self.total = 0
        self._lock = threading.Lock()

    def add_total(self, n: int):
        with self._lock:
            self.total += n
            self._emit()

    def inc_done(self):
        with self._lock:
            self.done += 1
            self._emit()

    def _emit(self):
        if self.cb:
            self.cb(self.done, self.total)


def collect_tencent_meetings(
    store: MeetingStore,
    *,
    days: int = 7,
    with_video: bool = True,
    max_workers: int = 4,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    client: Optional[TencentClient] = None,
    uploader: Optional[OSSUploader] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
    on_phase: Optional[Callable[[str], None]] = None,
    department_pattern=DEPARTMENT_PATTERN,
    meeting_codes=MEETING_CODES,
    limit: Optional[int] = 2000,
) -> Dict[str, Any]:
    """两条管线并发采集，结果合并去重：

    管线A「会议号」：按 meeting_codes 筛选录制（会议室录制无 userid，不依赖部门）。
    管线B「部门」：按 department_pattern 匹配部门员工，筛其创建的录制。
    两管线各自多线程下载/上传，并发跑，最后合并（store 按 source_ref 幂等去重）。

    关键：部门员工枚举【前置完成一次】，并把 _department 打到每条命中录制上；
    再把两条管线【互斥切分】——既属于部门、又属于会议号的录制只进部门管线，
    避免会议号管线抢先入库导致 department 字段丢失（保证按部门筛选不漏）。
    """
    from concurrent.futures import ThreadPoolExecutor

    client = client or TencentClient()
    code_set = set(meeting_codes) if meeting_codes else set()
    prog = _Progress(on_progress)
    if on_phase:
        on_phase("拉取录制列表")

    # 1) 录制列表只拉一次，两条管线共用
    records = client.list_records(days=days, max_records=limit, start_ts=start_ts, end_ts=end_ts)

    # 2) 部门员工枚举【前置】：得到 {userid: department_name}
    if on_phase:
        on_phase("枚举部门员工")
    user_map = (
        client.collect_department_users(department_pattern=department_pattern)
        if department_pattern else {}
    )
    userid_to_dept = {uid: info["dept"] for uid, info in user_map.items()}
    # userid → 真实姓名 映射（部门用户接口的 username），供解析主持人/参会人
    name_map = {uid: info["name"] for uid, info in user_map.items() if info.get("name")}

    # 3) 给每条命中部门的录制打上 _department 标记（含与会议号重叠的录制）
    dept_hit = 0
    for r in records:
        uid = r.get("userid")
        if uid and uid in userid_to_dept:
            r["_department"] = userid_to_dept[uid]
            dept_hit += 1

    # 4) 互斥切分：部门命中(含重叠)→部门管线；仅会议号→会议号管线。
    #    这样重叠录制只被处理一次（由部门管线带 department 入库），杜绝重复下载与字段丢失。
    dept_records: List[Dict[str, Any]] = []
    code_records: List[Dict[str, Any]] = []
    dept_ids = set()
    for r in records:
        if not r.get("meeting_record_id"):
            continue
        if r.get("_department"):
            dept_records.append(r)
            dept_ids.add(r["meeting_record_id"])
            continue
        if code_set and (r.get("meeting_code") or "").replace(" ", "") in code_set:
            code_records.append(r)

    if on_phase:
        on_phase(f"并发采集：会议号 {len(code_records)} + 部门 {len(dept_records)}")

    # 5) 两管线并发下载/上传/入库（带 name_map 解析姓名）
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_code = pool.submit(
            _run_batch, store, client, uploader, code_records, with_video, max_workers, "会议号", prog, name_map,
        )
        fut_dept = pool.submit(
            _run_batch, store, client, uploader, dept_records, with_video, max_workers, "部门", prog, name_map,
        )
        code_result = fut_code.result()
        dept_result = fut_dept.result()

    items = [{"_info": f"会议号管线: 命中 {len(code_records)} 条, 入库 {code_result['created']}"},
             {"_info": f"部门管线: 匹配 {len(userid_to_dept)} 人, 命中录制 {dept_hit} 条(含与会议号重叠), 入库 {dept_result['created']}"}]
    items += code_result["items"] + dept_result["items"]
    return {
        "created": code_result["created"] + dept_result["created"],
        "skipped": code_result["skipped"] + dept_result["skipped"],
        "failed": code_result["failed"] + dept_result["failed"],
        "items": items,
        "by_code": code_result["created"],
        "by_dept": dept_result["created"],
        "dept_users": len(userid_to_dept),
    }


def _run_batch(store, client, uploader, records, with_video, max_workers, tag, prog, name_map=None) -> Dict[str, Any]:
    """多线程处理一批录制：去重 → 下载/上传 → 入库。name_map 用于把 userid 解析成姓名。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    pending: List[Dict[str, Any]] = []
    skipped = 0
    for r in records:
        mid = r.get("meeting_record_id")
        if not mid:
            continue
        if store.find_by_source_ref(MeetingSource.TENCENT_MEETING, str(mid)):
            skipped += 1
            continue
        pending.append(r)

    prog.add_total(len(pending))
    created, failed = 0, 0
    items: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {pool.submit(_process_one, client, uploader, r, with_video, name_map): r for r in pending}
        for fut in as_completed(futures):
            r = futures[fut]
            mid = r.get("meeting_record_id")
            try:
                meeting = fut.result()
                if meeting is None:
                    skipped += 1
                else:
                    store.upsert(meeting)
                    created += 1
                    items.append({"tag": tag, "meeting_record_id": mid, "title": meeting.title, "created": True})
            except Exception as exc:  # noqa: BLE001
                failed += 1
                items.append({"tag": tag, "meeting_record_id": mid, "error": str(exc)[:120]})
            finally:
                prog.inc_done()
    return {"created": created, "skipped": skipped, "failed": failed, "items": items}


def _process_one(client: TencentClient, uploader: Optional[OSSUploader], record: Dict[str, Any], with_video: bool, name_map=None) -> Optional[Meeting]:
    mid = str(record["meeting_record_id"])
    address = client.get_record_address(mid)
    subject = address.get("subject") or record.get("subject") or record.get("meeting_id") or "未知会议"
    date = _meeting_date(record, address)
    content_url, media_url, raw_content = _extract_assets(
        client, address, uploader, date=date, title=subject, with_video=with_video,
    )
    if not raw_content and not media_url:
        return None
    # 主持人/参会人：优先用 userid→姓名 映射解析；查不到回退原 userid
    nm = name_map or {}
    owner_id = record.get("host_user_id") or record.get("userid") or ""
    owner = nm.get(owner_id, owner_id)
    raw_participants = [p for p in (address.get("participants") or []) if p]
    participants = [nm.get(p, p) for p in raw_participants] or ([owner] if owner else [])
    return build_meeting_from_tencent_meeting(
        meeting_record_id=mid,
        title=subject,
        start_time=_to_iso(date),
        owner=owner,
        participants=participants,
        meeting_code=str(record.get("meeting_code") or ""),
        department=str(record.get("_department") or ""),
        content_url=content_url,
        media_url=media_url,
        raw_content=raw_content,
    )


def _meeting_date(record: Dict[str, Any], address: Dict[str, Any]) -> str:
    for source in (record, address):
        for field in ("media_start_time", "start_time", "end_time"):
            value = source.get(field)
            if not value:
                continue
            try:
                ts = int(value)
                if ts > 1_000_000_000_000:
                    ts //= 1000
                return datetime.fromtimestamp(ts).strftime("%Y%m%d")
            except (TypeError, ValueError, OSError, OverflowError):
                continue
    return datetime.now().strftime("%Y%m%d")


def _to_iso(date_yyyymmdd: str) -> str:
    if not date_yyyymmdd or len(date_yyyymmdd) != 8:
        return ""
    return f"{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}"


def _safe_name(title: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", str(title or "")).strip("_")
    return cleaned or "meeting"


def _clean_user_name(raw: str) -> str:
    """清理部门用户姓名：去掉前导工号/编码前缀（如 DGDBF06燕丽 → 燕丽）。纯 ASCII 原样返回。"""
    raw = (raw or "").strip()
    m = re.match(r"^[A-Za-z0-9_\-]*[\s]*([一-龥].*)$", raw)
    return m.group(1).strip() if m else raw


def _upload_video(url: str, client: "TencentClient", uploader: OSSUploader, key: str) -> str:
    """视频上传 OSS。优先【流式直传】：下载响应的 raw 流分片灌进 OSS，不落本地磁盘，
    下载(入站)与上传(出站)两条管道并行、省一次磁盘读写。

    用 multipart 分片上传，不依赖 Content-Length、不需源可 seek。
    万一直传途中异常，【退回本地临时文件】中转——保证永远不比原来更脆弱。
    """
    # 1) 直传路径
    try:
        resp = requests.get(url, stream=True, timeout=300)
        resp.raise_for_status()
        resp.raw.decode_content = True
        try:
            return uploader.upload_stream(resp.raw, key)
        finally:
            resp.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("视频直传失败，退回本地中转 %s：%s", key, exc)

    # 2) 兜底：本地临时文件（mkstemp 唯一名，杜绝并发同名撞文件）
    fd, tmp_path = tempfile.mkstemp(suffix=".mp4", prefix="tencent_")
    os.close(fd)
    try:
        client.download_to_file(url, tmp_path)
        return uploader.upload_file(tmp_path, key)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _extract_assets(client: TencentClient, address: Dict[str, Any], uploader: Optional[OSSUploader], *, date: str, title: str, with_video: bool = True) -> tuple:
    content_url = media_url = ""
    raw_content = ""
    name = _safe_name(title)
    for file in address.get("record_files", []) or []:
        video = file.get("download_address")
        if video and uploader and with_video and not media_url:
            media_url = _upload_video(video, client, uploader, uploader.prefixed_key(f"{date}/{name}_录制.mp4"))
        summaries = file.get("meeting_summary", []) or []
        ordered = sorted(summaries, key=lambda s: (s.get("file_type") or "").lower() != "txt")
        for summary in ordered:
            url = summary.get("download_address")
            if not url:
                continue
            filetype = summary.get("file_type", "txt")
            raw_content = extract_text(client.download_bytes(url), filetype)
            if raw_content:
                if uploader:
                    content_url = uploader.upload_bytes(
                        raw_content.encode("utf-8"),
                        uploader.prefixed_key(f"{date}/{name}_纪要.txt"),
                    )
                break
    return content_url, media_url, raw_content
