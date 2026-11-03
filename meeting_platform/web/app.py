"""统一会议管理平台 Web 仪表盘。

一个轻量 Flask 应用，把统一库里的会议以「双 URL + AI 分析」形态可视化：
- 顶部统计 + 搜索 + 来源/状态筛选
- 会议列表：来源徽标、标题、时间、主持人、待办数、状态
- 详情面板：内嵌视频播放、AI 摘要/议题/决议/待办、纪要原文、OSS 链接
- 操作：单条/全部一键分析（无 Key 时自动 Mock）

运行：

    python -m meeting_platform.web.app
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from html import escape as html_escape
from datetime import timedelta
from typing import Optional

from flask import Flask, jsonify, redirect, request, session, url_for

from meeting_platform.analysis.analyzer import analyze_meeting, analyze_pending
from meeting_platform.analysis.llm import LLMClient, MockLLMClient, OpenAICompatibleClient
from meeting_platform.core.models import ActionItem, ActionItemStatus, MeetingSource, MeetingStatus, display_title, utc_now_iso
from meeting_platform.core.store import MeetingStore
from meeting_platform.ingest.feishu_oauth import REQUIRED_MINUTES_SCOPES, get_feishu_oauth_service
from meeting_platform.ingest.oss import OSSUploader
from meeting_platform.ingest.text_extract import extract_text
from meeting_platform.ops.email_supervision import (
    list_staff_options,
    request_review_edits_for_range,
    review_request_sent,
    send_review_request,
    submit_supervision,
)
from meeting_platform.ops.scheduler import CrawlRunner, EmailSupervisionScheduler, FeishuCrawlRunner, MeetingScheduler, SupervisionRunner
from meeting_platform.ops.notion_sync import NotionSyncRunner
from meeting_platform.web.auth import _user_store

log = logging.getLogger("web")

STORE_PATH = os.getenv("MEETING_STORE_PATH", "data/meetings.db")
# 登录会话 secret：优先环境变量，否则持久化到 data/.session_secret（重启不掉线）
_SESSION_SECRET_FILE = os.path.join(os.path.dirname(STORE_PATH) or ".", ".session_secret")


def _load_secret_key() -> str:
    env = os.getenv("SESSION_SECRET", "").strip()
    if env:
        return env
    try:
        with open(_SESSION_SECRET_FILE, "r", encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(_SESSION_SECRET_FILE) or ".", exist_ok=True)
        with open(_SESSION_SECRET_FILE, "w", encoding="utf-8") as f:
            f.write(key)
        try:  # 限制为仅属主可读写（服务器侧最小权限；Windows 下为 no-op）
            os.chmod(_SESSION_SECRET_FILE, 0o600)
        except OSError:
            pass
    except OSError:
        pass
    return key

# 进程级单例：按来源独立的按需采集器 + 定时调度器 + 督办回填 + Notion 手动同步
# 腾讯/飞书各自一个 Runner，互不阻塞，可同时采集
_runners: dict = {}
_scheduler: Optional[MeetingScheduler] = None
_email_supervision_scheduler: Optional[EmailSupervisionScheduler] = None
_supervise_runner: Optional[SupervisionRunner] = None
_notion_sync_runner: Optional[NotionSyncRunner] = None


def _runner_for(source: str):
    """按来源取/建独立 Runner（腾讯用 CrawlRunner，飞书用 FeishuCrawlRunner）。"""
    if source not in _runners:
        _runners[source] = CrawlRunner(STORE_PATH) if source == "tencent" else FeishuCrawlRunner(STORE_PATH)
    return _runners[source]


def _store() -> MeetingStore:
    return MeetingStore(STORE_PATH)


def _llm() -> LLMClient:
    """配置了 LLM_API_KEY 用真实模型，否则用 Mock（演示零门槛）。"""
    if os.getenv("LLM_API_KEY") and os.getenv("LLM_BASE_URL") and os.getenv("LLM_MODEL"):
        return OpenAICompatibleClient()
    return MockLLMClient()


def _llm_mode() -> str:
    return "真实模型" if os.getenv("LLM_API_KEY") else "Mock(演示)"


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = _load_secret_key()
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=int(os.getenv("LOGIN_SESSION_DAYS", "7")))
    # Cookie 安全加固：SameSite=Lax 防 CSRF 跨站携带；HttpOnly 防 JS 读 cookie。
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

    @app.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    # 全站登录守卫：除登录/登出/飞书 OAuth 回调/健康检查/静态资源外，未登录一律拦截
    _PUBLIC_PATHS = {"/login", "/logout", "/callback", "/health", "/favicon.ico"}

    @app.before_request
    def _login_guard():
        if request.path in _PUBLIC_PATHS or request.path.startswith("/static/"):
            return None
        # 督办填写页 /supervision/<id> 及其提交端点对外公开：外部会议主持人凭邮件里的
        # 链接填写、提交，没有平台账号。链接中的 meeting_id 是不可猜测的能力令牌（32位 hex）。
        # 注意用 "/supervision/" 带斜杠前缀匹配，避免误放行内部管理页 /supervision-range。
        if request.path.startswith("/supervision/") or request.path.endswith("/email-supervision"):
            return None
        if session.get("user"):
            return None
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error="未登录"), 401
        return redirect(url_for("login"))

    @app.get("/health")
    def health():
        """无鉴权健康检查端点，供 nginx/负载均衡探活。"""
        return jsonify(ok=True, status="ok")

    @app.get("/login")
    def login():
        if session.get("user"):  # 已登录再访问登录页 → 直接进首页
            return redirect(url_for("index"))
        return _login_html(error="")

    @app.post("/login")
    def login_post():
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""
        if _user_store.verify(u, p):
            session.permanent = True
            session["user"] = u
            nxt = request.form.get("next") or "/"
            # 仅允许站内相对路径，阻断 //evil.com 形式的开放重定向
            nxt = nxt if (str(nxt).startswith("/") and not str(nxt).startswith("//")) else "/"
            return redirect(nxt)
        return _login_html(error="用户名或密码错误"), 401

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        return _dashboard_html()

    @app.get("/owner-stats")
    def owner_stats_page():
        return _owner_stats_html()

    @app.get("/api/meetings")
    def api_meetings():
        source = request.args.get("source")
        status = request.args.get("status")
        owner = request.args.get("owner")
        date_from = request.args.get("date_from") or ""
        date_to = request.args.get("date_to") or ""
        meetings = _store().list(
            source=MeetingSource(source) if source else None,
            status=MeetingStatus(status) if status else None,
            owner=owner or None,
            date_from=date_from or None,
            date_to=date_to or None,
        )
        payload = [_brief(m) for m in meetings]
        return jsonify(ok=True, count=len(payload), meetings=payload)

    @app.get("/api/owners")
    def api_owners():
        return jsonify(ok=True, owners=_store().owners())

    @app.get("/api/staff")
    def api_staff():
        try:
            return jsonify(ok=True, staff=list_staff_options())
        except Exception as exc:  # noqa: BLE001
            return jsonify(ok=False, error=str(exc), staff=[]), 500

    @app.get("/api/meetings/<meeting_id>")
    def api_detail(meeting_id: str):
        meeting = _store().get(meeting_id)
        if not meeting:
            return jsonify(ok=False, error="meeting not found"), 404
        d = meeting.to_dict()
        d["display_title"] = display_title(meeting)
        return jsonify(ok=True, meeting=d)

    @app.post("/api/meetings/<meeting_id>/analyze")
    def api_analyze_one(meeting_id: str):
        store = _store()
        meeting = store.get(meeting_id)
        if not meeting:
            return jsonify(ok=False, error="meeting not found"), 404
        try:
            analyze_meeting(meeting, llm=_llm())
            store.upsert(meeting)
        except Exception as exc:  # noqa: BLE001
            log.exception("分析会议失败 meeting_id=%s 原文长度=%d", meeting_id, len(meeting.raw_content or ""))
            return jsonify(ok=False, error=str(exc)), 500
        d = meeting.to_dict()
        d["display_title"] = display_title(meeting)
        return jsonify(ok=True, meeting=d)

    @app.get("/supervision/<meeting_id>")
    def supervision_page(meeting_id: str):
        meeting = _store().get(meeting_id)
        if not meeting:
            return "<h3>meeting not found</h3>", 404
        return _supervision_html(meeting)

    @app.get("/supervision-range")
    def supervision_range_page():
        return _supervision_range_html(
            date_from=request.args.get("start") or "",
            date_to=request.args.get("end") or "",
            sources=request.args.get("sources") or "tencent,feishu",
        )

    @app.post("/api/meetings/<meeting_id>/email-supervision")
    def api_email_supervision(meeting_id: str):
        store = _store()
        meeting = store.get(meeting_id)
        if not meeting:
            return jsonify(ok=False, error="meeting not found"), 404
        if meeting.supervised:
            return jsonify(ok=False, error="每个会议邮件督办只能填写并发送一次"), 400
        body = request.get_json(silent=True) or {}
        title = str(body.get("title") or "").strip()
        convener = str(body.get("convener") or "").strip()
        items = body.get("items") or []
        if not title:
            return jsonify(ok=False, error="会议主题必填"), 400
        if not convener:
            return jsonify(ok=False, error="会议召集人必填"), 400
        # 会议主持人优先取填写页从人员名单选择的值，为空则回退系统采集的 owner；两者皆空则拒绝
        responsible = str(body.get("responsible") or "").strip() or str(meeting.owner or "").strip()
        if not responsible:
            return jsonify(ok=False, error="会议主持人必填"), 400
        try:
            current_payload = {
                "title": title,
                "convener": convener,
                "responsible": responsible,
                "items": items,
            }
            result = submit_supervision(meeting, title=title, convener=convener, responsible=responsible, items=items)
            if result.get("ok"):
                meeting.title = title
                _sync_confirmed_items_to_meeting(meeting, items)
                meeting.supervised = True
                meeting.supervision_payload = current_payload
                meeting.supervision_sent_at = utc_now_iso()
                store.upsert(meeting)
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify(ok=False, error=str(exc)), 500
        status = 200 if result.get("ok") else 400
        return jsonify(result), status

    @app.post("/api/meetings/<meeting_id>/request-supervision-edit")
    def api_request_supervision_edit(meeting_id: str):
        store = _store()
        meeting = store.get(meeting_id)
        if not meeting:
            return jsonify(ok=False, error="meeting not found"), 404
        if meeting.supervised:
            return jsonify(ok=False, error="该会议已填写并写入督办表"), 400
        if _review_request_sent(meeting):
            return jsonify(ok=False, error="该会议已发送给主持人修改"), 400
        try:
            base_url = request.host_url.rstrip("/")
            result = send_review_request(meeting, edit_url=f"{base_url}/supervision/{meeting.meeting_id}")
            meeting.supervision_payload = {
                **(meeting.supervision_payload or {}),
                "edit_request_sent_at": utc_now_iso(),
                "edit_request_receiver": result.get("receiver", ""),
                "edit_request_email": result.get("email_to", ""),
            }
            store.upsert(meeting)
        except Exception as exc:  # noqa: BLE001
            return jsonify(ok=False, error=str(exc)), 500
        return jsonify(result)

    @app.post("/api/request-supervision-edits")
    def api_request_supervision_edits():
        body = request.get_json(silent=True) or {}
        date_from = request.args.get("start") or body.get("start") or ""
        date_to = request.args.get("end") or body.get("end") or ""
        sources = request.args.get("sources") or body.get("sources") or "tencent,feishu"
        source_map = {"tencent": MeetingSource.TENCENT_MEETING, "feishu": MeetingSource.FEISHU_MINUTES}
        picked = [s for s in str(sources).split(",") if s in source_map] or ["tencent", "feishu"]
        base_url = request.host_url.rstrip("/")
        result = request_review_edits_for_range(
            _store(),
            date_from=date_from or "",
            date_to=date_to or "",
            sources=[source_map[s] for s in picked],
            base_url=base_url,
        )
        # 落日志：HTTP 200 看不出真发了没。sent=0 多半是已发过/未分析/已提交被跳过。
        log.info("发起邮件督办 %s~%s sources=%s：发送 %d，失败 %d，"
                 "跳过(已发 %d / 已提交 %d / 未分析 %d)；base_url=%s",
                 date_from, date_to, picked, result.get("sent", 0), result.get("failed", 0),
                 result.get("skipped_sent", 0), result.get("skipped_submitted", 0),
                 result.get("skipped_not_analyzed", 0), base_url)
        for it in result.get("items", []):
            if it.get("error"):
                log.warning("督办邮件发送失败：%s %s", it.get("meeting_id"), it.get("error"))
        return jsonify(result)

    @app.post("/api/analyze-all")
    def api_analyze_all():
        source = request.args.get("source")
        date_from = request.args.get("start") or request.args.get("date_from") or ""
        date_to = request.args.get("end") or request.args.get("date_to") or ""
        source_alias = {
            "tencent": MeetingSource.TENCENT_MEETING,
            "feishu": MeetingSource.FEISHU_MINUTES,
            "tencent_meeting": MeetingSource.TENCENT_MEETING,
            "feishu_minutes": MeetingSource.FEISHU_MINUTES,
        }
        source_filter = source_alias.get(source) if source else None
        result = analyze_pending(_store(), llm=_llm(),
                                 source=source_filter,
                                 date_from=date_from or None,
                                 date_to=date_to or None)
        if result.get("failed"):
            log.warning("批量分析失败 %d 条：%s", result["failed"],
                        [{"meeting_id": i.get("meeting_id"), "error": i.get("error")}
                         for i in result.get("items", []) if i.get("error")])
        else:
            log.info("批量分析完成：分析 %d，跳过 %d，失败 0",
                     result.get("analyzed", 0), result.get("skipped", 0))
        return jsonify(ok=True, **result)

    @app.post("/api/meetings/<meeting_id>/transcript")
    def api_upload_transcript(meeting_id: str):
        """上传/覆盖纪要原文：支持文件(.txt/.docx/.pdf 自动提取) 或 JSON {text}。
        覆盖 raw_content → 清空旧分析 → 状态回已采集（可重新分析）；尽量同步到 OSS。"""
        store = _store()
        meeting = store.get(meeting_id)
        if not meeting:
            return jsonify(ok=False, error="meeting not found"), 404
        text = ""
        upload = request.files.get("file")
        if upload:
            text = extract_text(upload.read(), upload.filename or "")
        else:
            body = request.get_json(silent=True) or {}
            text = (body.get("text") or "").strip()
        if not text.strip():
            return jsonify(ok=False, error="纪要内容为空（或文件解析失败）"), 400
        meeting.raw_content = text
        meeting.analysis = None
        meeting.status = MeetingStatus.CAPTURED
        meeting.supervised = False  # 纪要被覆盖 → 旧督办标记作废，需重新分析+督办
        meeting.supervision_payload = {}
        meeting.supervision_sent_at = ""
        try:  # 同步 OSS 保持双URL一致；失败不阻断（raw_content 已在即可分析）
            up = OSSUploader()
            meeting.content_url = up.upload_bytes(
                text.encode("utf-8"),
                up.prefixed_key(f"upload/{meeting.meeting_id}_纪要.txt"),
            )
        except Exception:  # noqa: BLE001
            pass
        store.upsert(meeting)
        d = meeting.to_dict()
        d["display_title"] = display_title(meeting)
        return jsonify(ok=True, meeting=d)

    @app.delete("/api/meetings/<meeting_id>/transcript")
    def api_delete_transcript(meeting_id: str):
        """删除纪要原文：清空 raw_content / content_url / analysis，状态回已采集。"""
        store = _store()
        meeting = store.get(meeting_id)
        if not meeting:
            return jsonify(ok=False, error="meeting not found"), 404
        meeting.raw_content = ""
        meeting.content_url = ""
        meeting.analysis = None
        meeting.status = MeetingStatus.CAPTURED
        meeting.supervised = False
        meeting.supervision_payload = {}
        meeting.supervision_sent_at = ""
        store.upsert(meeting)
        d = meeting.to_dict()
        d["display_title"] = display_title(meeting)
        return jsonify(ok=True, meeting=d)

    @app.post("/api/crawl")
    def api_crawl():
        """按来源独立采集：?start=&end=&sources=tencent,feishu。
        每个来源各自一个 Runner，可并发、互不阻塞；已在跑的来源返回 started=False。"""
        start = request.args.get("start") or ""
        end = request.args.get("end") or start
        sources = [s.strip() for s in (request.args.get("sources") or "tencent,feishu").split(",") if s.strip()]
        started = {}
        for s in sources:
            started[s] = _runner_for(s).start_range(start, end)
        return jsonify(ok=True, started=started)

    @app.get("/api/crawl/status")
    def api_crawl_status():
        """返回所有来源 Runner 的状态 + 是否有任一在跑。"""
        runners = {s: r.status() for s, r in _runners.items()}
        any_running = any(st.get("running") for st in runners.values())
        return jsonify(ok=True, runners=runners, running=any_running)

    # ===================== 飞书 OAuth + 妙记采集 =====================

    @app.get("/feishu")
    def feishu_page():
        return _feishu_page_html()

    @app.get("/feishu/login")
    def feishu_login():
        return redirect(get_feishu_oauth_service().build_auth_url())

    @app.get("/callback")
    def feishu_callback():
        svc = get_feishu_oauth_service()
        code = (request.args.get("code") or "").strip()
        error = (request.args.get("error") or "").strip()
        if error:
            return f"<h3>授权失败</h3><pre>{error}</pre>", 400
        if not code:
            return "<h3>未拿到 code</h3>", 400
        try:
            token_payload = svc.exchange_code(code)
            user_info = svc.get_user_info(token_payload["access_token"])
            user_key = svc.persist_user_session(token_payload, user_info)
        except Exception as exc:  # noqa: BLE001
            # 同一个 code 只能换一次 token，重放（刷新回调页/点返回）必然抛错 500。
            # 这里记录到日志，便于区分"真授权失败"与"code 重放"。
            log.exception("飞书回调换 token 失败（code 可能已被消费）：code=%s", code[:8])
            return f"<h3>授权异常</h3><pre>{exc}</pre>", 500
        name = user_info.get("name") or "你的飞书账号"
        log.info("飞书授权成功：name=%s user_key=%s", name, user_key)
        scope_ok = all(s in (token_payload.get("scope") or "").split() for s in REQUIRED_MINUTES_SCOPES)
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"/>
<title>授权成功</title><style>body{{margin:0;min-height:100vh;display:grid;place-items:center;
font-family:Arial;background:#f6f8fb;color:#111}}.p{{width:min(520px,calc(100vw - 32px));background:#fff;
border:1px solid #dbe3ef;border-radius:12px;padding:28px;box-shadow:0 8px 24px rgba(0,0,0,.06);text-align:center}}
.pill{{display:inline-block;padding:4px 12px;border-radius:999px;font-size:13px;margin-top:8px}}
.ok{{background:#ecfdf5;color:#047857}}.bad{{background:#fef2f2;color:#b91c1c}}</style></head>
<body><div class="p"><h2>✅ 授权成功</h2><p>{name} 已完成飞书授权，可关闭此页。</p>
<span class="pill {'ok' if scope_ok else 'bad'}">{'妙记权限齐全' if scope_ok else '妙记权限缺失，需在后台开通 4 个 scope 后重新授权'}</span>
<p style="margin-top:16px"><a href="/feishu">返回飞书管理</a> · <a href="/">回仪表盘</a></p></div></body></html>"""

    @app.get("/api/feishu/users")
    def feishu_users():
        svc = get_feishu_oauth_service()
        out = []
        for user_key, record in svc.store.list_users().items():
            info = record.get("user_info") or {}
            tokens = record.get("tokens") or {}
            scope = (tokens.get("scope") or "").split()
            out.append({
                "user_key": user_key,
                "name": info.get("name"),
                "open_id": info.get("open_id"),
                "access_token_expires_at": tokens.get("access_token_expires_at"),
                "refresh_token_expires_at": tokens.get("refresh_token_expires_at"),
                "has_minutes_scope": all(s in scope for s in REQUIRED_MINUTES_SCOPES),
            })
        return jsonify(ok=True, count=len(out), users=out)

    @app.patch("/api/feishu/users/<user_key>")
    def feishu_rename(user_key: str):
        name = str((request.get_json(silent=True) or {}).get("name") or "").strip()
        if not name:
            return jsonify(ok=False, error="name required"), 400
        try:
            get_feishu_oauth_service().store.update_user_info(user_key, user_info={"name": name})
        except KeyError:
            return jsonify(ok=False, error="not found"), 404
        return jsonify(ok=True, user_key=user_key, name=name)

    @app.delete("/api/feishu/users/<user_key>")
    def feishu_delete(user_key: str):
        existed = get_feishu_oauth_service().store.delete_user(user_key)
        if not existed:
            return jsonify(ok=False, error="not found"), 404
        return jsonify(ok=True, user_key=user_key)

    @app.post("/api/feishu/users/refresh-tokens")
    def feishu_refresh_tokens():
        result = get_feishu_oauth_service().refresh_all_access_tokens()
        # 把成功/失败明细落到日志，便于排查"刷新了没用"——只看 HTTP 200 看不出哪条失败。
        log.info("手动刷新 token：成功 %d，失败 %d", result.get("refreshed", 0), result.get("failed", 0))
        for r in result.get("results", []):
            if not r.get("ok"):
                log.warning("刷新失败 name=%s error=%s", r.get("name"), r.get("error"))
        return jsonify(ok=True, **result)

    # ===================== 督办回填 =====================

    @app.post("/api/supervise")
    def api_supervise():
        """手动触发督办回填：?source=tencent/feishu，缺省=全部来源。"""
        global _supervise_runner
        if _supervise_runner is None:
            _supervise_runner = SupervisionRunner(STORE_PATH)
        source = request.args.get("source") or None
        started = _supervise_runner.start(source)
        return jsonify(ok=True, started=started, message="已在运行" if not started else "已开始督办回填")

    @app.get("/api/supervise/status")
    def api_supervise_status():
        global _supervise_runner
        if _supervise_runner is None:
            return jsonify(ok=True, running=False, last=None)
        return jsonify(ok=True, **_supervise_runner.status())

    # ===================== Notion 同步 =====================

    @app.post("/api/notion/sync")
    def api_notion_sync():
        """手动触发 Notion 同步：?start=&end=（YYYY-MM-DD），缺省=今天。"""
        global _notion_sync_runner
        if _notion_sync_runner is None:
            _notion_sync_runner = NotionSyncRunner(STORE_PATH)
        date_from = (request.args.get("start") or "").strip() or None
        date_to = (request.args.get("end") or "").strip() or None
        started = _notion_sync_runner.start(date_from, date_to)
        return jsonify(ok=True, started=started, message="已在运行" if not started else "已开始同步到 Notion")

    @app.get("/api/notion/sync/status")
    def api_notion_sync_status():
        global _notion_sync_runner
        if _notion_sync_runner is None:
            return jsonify(ok=True, running=False, last=None, progress={"done": 0, "total": 0}, phase="")
        return jsonify(ok=True, **_notion_sync_runner.status())

    return app


def start_background_services() -> None:
    """启动定时调度器（每天凌晨爬取昨天的腾讯+飞书会议，分析完自动同步 Notion）+ 飞书 token 自动刷新。"""
    global _scheduler, _email_supervision_scheduler
    if _scheduler is None:
        _scheduler = MeetingScheduler(STORE_PATH)
        _scheduler.start()
    if _email_supervision_scheduler is None:
        _email_supervision_scheduler = EmailSupervisionScheduler(STORE_PATH)
        _email_supervision_scheduler.start()
    # 飞书 token 每小时自动刷新（access_token 约 2 小时过期）
    get_feishu_oauth_service().start_token_refresh_scheduler()


def _brief(meeting) -> dict:
    a = meeting.analysis
    payload = meeting.supervision_payload or {}
    return {
        "meeting_id": meeting.meeting_id,
        "source": meeting.source.value,
        "title": meeting.title,
        "display_title": display_title(meeting),
        "owner": meeting.owner,
        "convener": payload.get("convener", ""),
        "start_time": meeting.start_time,
        "participants": meeting.participants,
        "content_url": meeting.content_url,
        "media_url": meeting.media_url,
        "status": meeting.status.value,
        "analyzed": bool(a),
        "action_count": len(a.action_items) if a else 0,
        "supervised": meeting.supervised,
        "edit_request_sent": _review_request_sent(meeting),
    }


def _html_escape(value) -> str:
    return html_escape(str(value or ""), quote=True)


def _review_request_sent(meeting) -> bool:
    return review_request_sent(meeting)


def _sync_confirmed_items_to_meeting(meeting, items) -> None:
    if not meeting.analysis:
        return
    cleaned = []
    for item in items or []:
        content = str(item.get("content") or "").strip()
        owner = str(item.get("owner") or "").strip()
        due = str(item.get("due") or "").strip()
        if content:
            cleaned.append(ActionItem(content=content, owner=owner, due=due, status=ActionItemStatus.OPEN))
    meeting.analysis.action_items = cleaned


# ---------------------------------------------------------------------------
# 仪表盘 HTML（内嵌，无构建步骤）
# ---------------------------------------------------------------------------


def _login_html(error: str = "") -> str:
    err_html = f'<div class="err">{html_escape(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · 会议管理平台</title>
<style>
  :root{{--bg:#0f172a;--card:#1e293b;--border:#334155;--muted:#94a3b8;--primary:#0f766e;--text:#e2e8f0}}
  *{{box-sizing:border-box}}
  body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:radial-gradient(1200px 600px at 50% -10%,#134e4a,#0f172a 60%);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:var(--text)}}
  .card{{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:34px 32px;width:340px;max-width:92vw;box-shadow:0 20px 50px rgba(0,0,0,.45)}}
  .logo{{width:46px;height:46px;border-radius:12px;background:var(--primary);color:#fff;display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:700;margin:0 auto 14px}}
  h1{{font-size:19px;text-align:center;margin:0 0 4px}}
  .sub{{text-align:center;color:var(--muted);font-size:12px;margin-bottom:22px}}
  label{{display:block;font-size:12px;color:var(--muted);margin:14px 0 6px}}
  input{{width:100%;padding:11px 13px;font:inherit;font-size:14px;border:1px solid var(--border);border-radius:10px;background:#0f172a;color:#fff;outline:none}}
  input:focus{{border-color:var(--primary)}}
  .pwd{{position:relative}}
  .pwd input{{padding-right:42px}}
  .pwd .eye{{position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;cursor:pointer;font-size:18px;color:var(--muted);padding:4px;line-height:1}}
  .pwd .eye:hover{{color:#fff}}
  .btn{{width:100%;margin-top:22px;padding:12px;font:inherit;font-size:15px;font-weight:600;border:none;border-radius:10px;background:var(--primary);color:#fff;cursor:pointer}}
  .btn:hover{{filter:brightness(1.08)}}
  .err{{background:#7f1d1d33;border:1px solid #7f1d1d;color:#fecaca;padding:9px 12px;border-radius:9px;font-size:13px;margin-top:16px;text-align:center}}
</style>
</head>
<body>
  <form class="card" method="post" action="/login" autocomplete="off">
    <div class="logo">会</div>
    <h1>会议管理平台</h1>
    <div class="sub">飞书妙记 + 腾讯会议 · 统一采集 · AI 分析</div>
    <label for="username">用户名</label>
    <input id="username" name="username" autofocus required>
    <label for="password">密码</label>
    <div class="pwd">
      <input id="password" name="password" type="password" required>
      <button class="eye" type="button" onclick="var p=document.getElementById('password');p.type=p.type==='password'?'text':'password';this.textContent=p.type==='password'?'👁':'🙈'" title="显示/隐藏密码">👁</button>
    </div>
    <button class="btn" type="submit">登 录</button>
    {err_html}
  </form>
</body>
</html>"""


def _dashboard_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>会议管理平台</title>
<style>
  :root{
    --bg:#eef2f7; --card:#fff; --text:#0f172a; --muted:#64748b;
    --primary:#2563eb; --primary-d:#1d4ed8; --border:#e2e8f0;
    --feishu:#0bbf7a; --tencent:#2563eb;
    --open:#2563eb; --ip:#d97706; --done:#059669; --over:#dc2626;
    --shadow:0 1px 3px rgba(15,23,42,.06),0 1px 2px rgba(15,23,42,.04);
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,"Segoe UI","Microsoft YaHei",Arial,sans-serif;background:var(--bg);color:var(--text)}
  .topbar{background:#0f172a;color:#fff;padding:14px 22px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:10;box-shadow:0 2px 8px rgba(0,0,0,.15)}
  .topbar .brand{display:flex;align-items:center;gap:10px}
  .topbar .logo{width:30px;height:30px;border-radius:8px;background:linear-gradient(135deg,#3b82f6,#06b6d4);display:grid;place-items:center;font-weight:800}
  .topbar h1{margin:0;font-size:17px;font-weight:700}
  .topbar .sub{font-size:11px;color:#94a3b8;margin-top:2px}
  .topbar .right{display:flex;align-items:center;gap:10px}
  .pill-mode{font-size:11px;color:#cbd5e1;background:#1e293b;padding:4px 10px;border-radius:999px}
  .ddate{font:inherit;font-size:12px;padding:7px 9px;border:1px solid #334155;border-radius:8px;background:#1e293b;color:#fff}
  .drange{display:inline-flex;align-items:center;gap:5px}
  .drange span{color:#94a3b8;font-size:12px}
  .fdate{font:inherit;padding:7px 9px;border:1px solid var(--border);border-radius:8px;background:#fff;font-size:12px}
  .wrap{max-width:1340px;margin:0 auto;padding:18px 18px 60px}
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
  .stat{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow)}
  .stat .label{color:var(--muted);font-size:11px}
  .stat .value{font-size:24px;font-weight:700;margin-top:3px}
  .stat .value small{font-size:12px;color:var(--muted);font-weight:500}
  .toolbar{display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:10px 12px;box-shadow:var(--shadow)}
  .toolbar input[type=search]{flex:1;min-width:200px;font:inherit;padding:8px 12px;border:1px solid var(--border);border-radius:8px}
  .toolbar select{font:inherit;padding:8px 10px;border:1px solid var(--border);border-radius:8px;background:#fff}
  .btn{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--border);background:#fff;cursor:pointer;font-weight:600}
  .btn:hover{background:#f8fafc}
  .btn.primary{background:var(--primary);color:#fff;border-color:var(--primary)}
  .btn.primary:hover{background:var(--primary-d)}
  .layout{display:grid;grid-template-columns:1fr 1.25fr;gap:16px;align-items:start}
  @media(max-width:1000px){.layout{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}}
  .list{display:flex;flex-direction:column;gap:8px;max-height:78vh;overflow:auto;padding-right:4px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px 14px;cursor:pointer;transition:.15s;box-shadow:var(--shadow)}
  .card:hover{border-color:var(--primary);transform:translateY(-1px)}
  .card.active{border-color:var(--primary);background:#eff6ff;box-shadow:0 0 0 2px rgba(37,99,235,.15)}
  .card .t1{font-weight:700;font-size:14px;display:flex;align-items:center;gap:7px;flex-wrap:wrap}
  .card .t1 .ttl{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .card .meta{color:var(--muted);font-size:11px;margin-top:5px;display:flex;gap:10px;flex-wrap:wrap}
  .badge{display:inline-block;padding:2px 7px;border-radius:999px;font-size:10px;font-weight:700;color:#fff}
  .b-feishu{background:var(--feishu)} .b-tencent{background:var(--tencent)}
  .b-captured{background:#94a3b8} .b-analyzed{background:var(--done)} .b-archived{background:#6366f1}
  .icons{font-size:11px;display:inline-flex;gap:6px;align-items:center;color:var(--muted)}
  .dot{width:7px;height:7px;border-radius:50%;display:inline-block}
  .detail{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px;box-shadow:var(--shadow);min-height:400px}
  .detail h2{margin:0;font-size:19px;line-height:1.35}
  .dmeta{color:var(--muted);font-size:12px;margin:7px 0 14px}
  .player{margin:10px 0 14px}
  .player video{width:100%;border-radius:10px;background:#000;max-height:340px}
  .linkrow{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
  .linkrow a{font-size:12px;color:var(--primary);text-decoration:none;border:1px solid var(--border);padding:5px 10px;border-radius:7px;background:#f8fafc;font-weight:600}
  .linkrow a:hover{background:#eff6ff}
  .sec{margin-top:16px}
  .sec h3{margin:0 0 7px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;font-weight:700}
  .chips{display:flex;flex-wrap:wrap;gap:6px}
  .chip{background:#f1f5f9;border:1px solid var(--border);border-radius:7px;padding:3px 9px;font-size:12px}
  ol.dec{margin:0;padding-left:18px} ol.dec li{margin:4px 0;font-size:14px}
  table{width:100%;border-collapse:collapse;font-size:13px;margin-top:4px}
  th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--border);vertical-align:top}
  th{color:var(--muted);font-weight:600;font-size:11px}
  .status{display:inline-block;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:700;color:#fff}
  .s-open{background:var(--open)} .s-in_progress{background:var(--ip)} .s-done{background:var(--done)} .s-over{background:var(--over)}
  .summary{font-size:14px;line-height:1.7;background:#f8fafc;border:1px solid var(--border);border-radius:9px;padding:11px 13px}
  .summary-card{background:#f8fafc;border:1px solid var(--border);border-radius:9px}
  .summary-card .sum-bar{display:flex;justify-content:space-between;align-items:center;padding:7px 11px;border-bottom:1px solid var(--border)}
  .summary-card .sum-bar .hint{font-size:11px;color:var(--muted)}
  .summary-card .summary{border:0;border-radius:0;max-height:220px;overflow:auto;white-space:normal}
  .summary-md h1{font-size:17px;margin:10px 0 6px} .summary-md h2{font-size:15px;margin:12px 0 5px;border-left:3px solid var(--primary);padding-left:8px} .summary-md h3{font-size:14px;margin:10px 0 4px;color:#334155}
  .summary-md p{margin:5px 0} .summary-md ul,.summary-md ol{margin:5px 0;padding-left:20px} .summary-md li{margin:3px 0}
  .summary-md strong{color:#0f172a} .summary-md code{background:#e2e8f0;padding:1px 5px;border-radius:4px;font-size:12px}
  .summary-md table{margin:6px 0} .summary-md th,.summary-md td{font-size:12px}
  .modal{position:fixed;inset:0;background:rgba(15,23,42,.55);display:none;z-index:100;align-items:center;justify-content:center;padding:24px}
  .modal.show{display:flex}
  .modal-box{background:#fff;border-radius:14px;max-width:880px;width:100%;max-height:88vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.3)}
  .modal-head{display:flex;justify-content:space-between;align-items:center;padding:13px 18px;border-bottom:1px solid var(--border)}
  .modal-head .t{font-weight:700;font-size:15px}
  .modal-close{background:#f1f5f9;border:1px solid var(--border);border-radius:8px;width:30px;height:30px;cursor:pointer;font-size:15px;color:#475569}
  .modal-close:hover{background:#e2e8f0}
  .modal-body{padding:18px 22px;overflow:auto;line-height:1.75;font-size:14px}
  .raw{white-space:pre-wrap;font-size:12px;line-height:1.6;color:#475569;max-height:240px;overflow:auto;background:#0f172a;color:#e2e8f0;padding:12px;border-radius:9px}
  .empty{color:var(--muted);text-align:center;padding:70px 0}
  .actions{margin-top:16px;display:flex;gap:8px;flex-wrap:wrap}
  .toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:#0f172a;color:#fff;padding:9px 18px;border-radius:9px;font-size:13px;opacity:0;transition:.2s;pointer-events:none;z-index:50}
  .toast.show{opacity:1}
  .spin{display:inline-block;width:13px;height:13px;border:2px solid #fff3;border-top:#fff;border-radius:50%;animation:s .7s linear infinite;vertical-align:-2px}
  @keyframes s{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="topbar">
  <div class="brand">
    <div class="logo">会</div>
    <div><h1>会议管理平台</h1><div class="sub">飞书妙记 + 腾讯会议 · 统一采集 · AI 分析 · 双 URL</div></div>
  </div>
  <div class="right">
    <a class="btn" href="/feishu" target="_blank" style="text-decoration:none">飞书授权管理</a>
    <a class="btn" href="https://gemdalesh.feishu.cn/base/OdwobaKJga77p8s22KLcRSZNn6d?table=tblT3ZGxybys2QDN&view=vewzp0sT5r" target="_blank" rel="noopener" style="text-decoration:none">📊 飞书多维表格</a>
    <a class="btn" href="/owner-stats" target="_blank" style="text-decoration:none">会议召集人统计</a>
    <span class="pill-mode">AI: __LLM__</span>
    <span class="pill-mode" style="background:#0f766e">👤 __USER__</span>
    <a class="btn" href="/logout" style="text-decoration:none;padding:6px 12px;font-size:12px">退出</a>
    <select id="srcPick" class="ddate" title="采集/分析的来源" style="cursor:pointer">
      <option value="tencent,feishu">全部来源</option>
      <option value="tencent">仅腾讯会议</option>
      <option value="feishu">仅飞书妙记</option>
    </select>
    <span class="drange">
      <input type="date" id="crawlStart" class="ddate" title="采集起始日期">
      <span>~</span>
      <input type="date" id="crawlEnd" class="ddate" title="采集截止日期">
    </span>
    <button class="btn" id="btnCrawl">采集该范围</button>
    <button class="btn primary" id="btnAll">一键分析全部</button>
    <button class="btn" id="btnEmailSupervision">发起邮件督办</button>
    <button class="btn" id="btnNotionSync" onclick="notionSyncRun()">同步到 Notion</button>
  </div>
</div>

<div class="wrap">
  <div class="stats" id="stats"></div>

  <div class="toolbar">
    <input type="search" id="search" placeholder="搜索会议标题 / 召集人 / 主持人 / 参会人…">
    <span class="drange">
      <input type="date" id="fDateFrom" class="fdate" title="筛选起始日期">
      <span>~</span>
      <input type="date" id="fDateTo" class="fdate" title="筛选截止日期">
    </span>
    <select id="fSource">
      <option value="">全部来源</option>
      <option value="feishu_minutes">飞书妙记</option>
      <option value="tencent_meeting">腾讯会议</option>
    </select>
    <select id="fStatus">
      <option value="">全部状态</option>
      <option value="captured">已采集</option>
      <option value="analyzed">已分析</option>
    </select>
    <span style="color:var(--muted);font-size:12px" id="count"></span>
  </div>

  <div class="layout">
    <div class="list" id="list"></div>
    <div class="detail" id="detail"><div class="empty">点击左侧会议查看详情</div></div>
  </div>
</div>
<div class="toast" id="toast"></div>
<div class="modal" id="sumModal" onclick="if(event.target===this)closeZoomSummary()">
  <div class="modal-box">
    <div class="modal-head"><span class="t">📋 AI 智能会议纪要</span><span style="display:inline-flex;gap:6px"><button class="modal-close" onclick="copySummary()" style="width:auto;padding:5px 12px;font-size:12px">📋 复制全文</button><button class="modal-close" onclick="closeZoomSummary()">✕</button></span></div>
    <div class="modal-body summary-md" id="sumModalBody"></div>
  </div>
</div>

<script>
let ALL=[], currentId=null, _curSumMd='';
const SRC={feishu_minutes:'飞书妙记',tencent_meeting:'腾讯会议'};
const ST={captured:'已采集',analyzed:'已分析',archived:'已归档',open:'待处理',in_progress:'进行中',done:'已完成',overdue:'已逾期'};

// 全局拦截 fetch：会话过期(401)自动跳登录页，避免列表静默变空
(function(){
  const _f=window.fetch;
  window.fetch=function(){
    return _f.apply(this,arguments).then(function(r){
      if(r.status===401 && location.pathname!=='/login'){location.href='/login';}
      return r;
    });
  };
})();

async function load(){
  try{
    const d=await (await fetch('/api/meetings')).json();
    ALL=d.meetings||[];
  }catch(e){
    ALL=[];
    document.getElementById('list').innerHTML='<div class="empty">加载失败，请确认服务在运行（端口是否对）</div>';
  }
  renderStats(); renderList();
}
function renderStats(){
  let feishu=0,tencent=0,todo=0,ana=0;
  ALL.forEach(m=>{if(m.source==='feishu_minutes')feishu++;else tencent++;todo+=m.action_count||0;if(m.analyzed)ana++;});
  document.getElementById('stats').innerHTML=[
    st('会议总数',ALL.length,''),
    st('飞书妙记',feishu,''),
    st('腾讯会议',tencent,''),
    st('待办合计',todo,''),
  ].join('');
}
function st(l,v,u){return `<div class="stat"><div class="label">${l}</div><div class="value">${v}<small> ${u}</small></div></div>`;}

function filteredMeetings(){
  const q=document.getElementById('search').value.trim().toLowerCase();
  const fs=document.getElementById('fSource').value;
  const fst=document.getElementById('fStatus').value;
  const df=document.getElementById('fDateFrom').value||'';
  const dt=document.getElementById('fDateTo').value||'';
  let ms=ALL.filter(m=>(!fs||m.source===fs)&&(!fst||m.status===fst));
  if(df)ms=ms.filter(m=>(m.start_time||'')>=df);   // start_time 形如 2026-08-03，字典序即日期序
  if(dt)ms=ms.filter(m=>(m.start_time||'')<=dt);
  if(q){ms=ms.filter(m=>((m.display_title||m.title)+(m.convener||'')+m.owner+(m.participants||[]).join('')).toLowerCase().includes(q));}
  return ms;
}

function renderList(){
  let ms=filteredMeetings();
  document.getElementById('count').textContent=`共 ${ms.length} 条`;
  const box=document.getElementById('list');
  if(!ms.length){box.innerHTML='<div class="empty">没有符合条件的会议</div>';return;}
  ms.sort((a,b)=>(b.start_time||'').localeCompare(a.start_time||''));
  box.innerHTML=ms.map(m=>{
    const d=(m.start_time||'').slice(0,10);
    return `<div class="card ${m.meeting_id===currentId?'active':''}" onclick="sel('${m.meeting_id}')">
      <div class="t1">
        <span class="badge b-${m.source}">${SRC[m.source]}</span>
        <span class="ttl">${esc(m.display_title||m.title)}</span>
        <span class="badge b-${m.status}">${ST[m.status]}</span>
      </div>
      <div class="meta">
        <span>${d||'—'}</span><span>召集人：${esc(m.convener||'—')}</span><span>主持人：${esc(m.owner||'—')}</span>
        <span class="icons">
          ${m.media_url?'<span class="dot" style="background:#2563eb"></span>视频':''}
          ${m.content_url?'<span class="dot" style="background:#0bbf7a"></span>纪要':''}
          ${m.analyzed?`<span class="dot" style="background:#d97706"></span>待办${m.action_count}`:''}
          ${m.edit_request_sent&&!m.supervised?'<span class="dot" style="background:#6366f1"></span><b style="color:#6366f1">已发修改邮件</b>':''}
          ${m.supervised?'<span class="dot" style="background:#059669"></span><b style="color:#059669">已督办</b>':''}
        </span>
      </div>
    </div>`;}).join('');
}

async function sel(id){
  currentId=id; renderList();
  let d;
  try{ d=await (await fetch('/api/meetings/'+id)).json(); }
  catch(e){ document.getElementById('detail').innerHTML='<div class="empty">加载详情失败</div>'; return; }
  if(!d||!d.ok||!d.meeting){ document.getElementById('detail').innerHTML='<div class="empty">未找到该会议（可能已被清理）</div>'; return; }
  const m=d.meeting, a=m.analysis||{};
  _curSumMd=a.summary||'';
  const convener=(m.supervision_payload&&m.supervision_payload.convener)||m.convener||'';
  const todos=(a.action_items&&a.action_items.length)?a.action_items.map(it=>
    `<tr><td>${esc(it.content)}</td><td>${esc(it.owner||'—')}</td><td>${esc(it.due||'—')}</td><td><span class="status s-${it.status}">${ST[it.status]||it.status}</span></td></tr>`
  ).join(''):`<tr><td colspan="4" style="color:var(--muted)">暂无待办</td></tr>`;
  const video=m.media_url?`<div class="player"><video controls preload="metadata" src="${m.media_url}"></video></div>`:'';
  document.getElementById('detail').innerHTML=`
    <h2>${esc(m.display_title||m.title)}</h2>
    <div class="dmeta">会议召集人：${esc(convener||'—')} · 会议主持人：${esc(m.owner||'—')} · 参会：${esc((m.participants||[]).join('、')||'—')} · 时间：${(m.start_time||'').slice(0,16).replace('T',' ')||'—'}</div>
    ${video}
    <div class="linkrow">
      ${m.media_url?`<a href="${m.media_url}" target="_blank">🎬 录制(OSS)</a>`:''}
      ${m.content_url?`<a href="${m.content_url}" target="_blank">📄 纪要(OSS)</a>`:''}
    </div>
    <div class="sec"><h3>AI 智能纪要</h3>
      ${a.summary?`<div class="summary-card">
        <div class="sum-bar"><span class="hint">默认预览（可上下滚动）· 点击右侧放大查看完整纪要</span><button class="btn" style="padding:4px 10px;font-size:12px" onclick="zoomSummary()">🔍 放大</button></div>
        <div class="summary summary-md" id="summaryBox">${mdToHtml(a.summary)}</div>
      </div>`:'<div class="summary"><span style="color:var(--muted)">尚未分析，点下方按钮一键生成</span></div>'}</div>
    <div class="sec"><h3>议题</h3><div class="chips">${(a.topics||[]).map(t=>`<span class="chip">${esc(t)}</span>`).join('')||'<span style="color:var(--muted)">—</span>'}</div></div>
    <div class="sec"><h3>决议</h3><ol class="dec">${(a.decisions||[]).map(x=>`<li>${esc(x)}</li>`).join('')||'<li style="color:var(--muted)">无</li>'}</ol></div>
    <div class="sec"><h3>待办（督办项）</h3><table><thead><tr><th>事项</th><th>责任人</th><th>截止</th><th>状态</th></tr></thead><tbody>${todos}</tbody></table></div>
    <div class="sec">
      <h3>纪要原文</h3>
      <div style="font-size:12px;margin-bottom:8px">${m.raw_content?('已有 '+m.raw_content.length+' 字'+((a.corrections>0)?` · <span style="color:#059669">✓ 术语纠错 ${a.corrections} 处</span>`:'')):'<span style="color:#dc2626">无纪要，无法分析。上传原文后即可一键分析</span>'}</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
        <input type="file" id="trFile" accept=".txt,.docx,.doc,.pdf" style="font-size:12px">
        <button class="btn primary" onclick="uploadTranscriptFile('${m.meeting_id}')">上传覆盖</button>
        <button class="btn" onclick="toggleEditTranscript()">粘贴/编辑</button>
        ${m.raw_content?`<button class="btn" onclick="deleteTranscript('${m.meeting_id}')" style="color:#dc2626">删除纪要</button>`:''}
      </div>
      <div id="trEdit" style="display:none">
        <textarea id="trText" rows="8" style="width:100%;font-size:12px;padding:8px;border:1px solid var(--border);border-radius:8px;box-sizing:border-box" placeholder="粘贴纪要原文…">${esc(m.raw_content||'')}</textarea>
        <button class="btn primary" onclick="saveTranscriptText('${m.meeting_id}')" style="margin-top:6px">保存</button>
      </div>
      ${m.raw_content?`<details><summary style="cursor:pointer;color:var(--muted);font-size:12px">查看原文预览</summary><div class="raw" style="margin-top:8px">${esc(m.raw_content.slice(0,4000))}${m.raw_content.length>4000?'\\n…(截断)':''}</div></details>`:''}
      ${m.corrected_content?`<details><summary style="cursor:pointer;color:var(--muted);font-size:12px">查看纠错后文本（AI 分析所用）</summary><div class="raw" style="margin-top:8px">${esc(m.corrected_content.slice(0,4000))}${m.corrected_content.length>4000?'\\n…(截断)':''}</div></details>`:''}
    </div>
    <div class="actions">
      ${(m.status!=='analyzed'&&m.raw_content)?`<button class="btn primary" onclick="analyzeOne('${m.meeting_id}',this)">一键 AI 分析</button>`:(m.status!=='analyzed'&&!m.raw_content)?`<span style="color:var(--muted);font-size:12px;align-self:center">需先上传纪要</span>`:''}
      <button class="btn" onclick="sel('${m.meeting_id}')">刷新</button>
    </div>`;
  // 暂停其他视频
  document.querySelectorAll('video').forEach(v=>{if(v.src!==m.media_url)v.pause();});
}

async function analyzeOne(id,btn){
  if(!btn)return;
  btn.disabled=true;btn.innerHTML='<span class="spin"></span> 分析中';
  try{
    const d=await (await fetch(`/api/meetings/${id}/analyze`,{method:'POST'})).json();
    if(d.ok){toast('分析完成');await load();await sel(id);}
    else toast('失败：'+(d.error||''));
  }catch(e){toast('请求失败');}
  finally{btn.disabled=false;btn.textContent='一键 AI 分析';}
}

// —— 纪要原文管理：上传文件(覆盖) / 粘贴保存 / 删除 ——
function toggleEditTranscript(){
  const el=document.getElementById('trEdit');
  el.style.display=(el.style.display==='none'?'block':'none');
}
async function uploadTranscriptFile(id){
  const f=document.getElementById('trFile');
  if(!f||!f.files[0]){toast('请先选择文件');return;}
  const fd=new FormData();fd.append('file',f.files[0]);
  try{
    const d=await (await fetch(`/api/meetings/${id}/transcript`,{method:'POST',body:fd})).json();
    if(d.ok){toast('纪要已覆盖');await load();await sel(id);}
    else toast('失败：'+(d.error||''));
  }catch(e){toast('请求失败');}
}
async function saveTranscriptText(id){
  const t=document.getElementById('trText').value;
  if(!t.trim()){toast('内容为空');return;}
  try{
    const d=await (await fetch(`/api/meetings/${id}/transcript`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})})).json();
    if(d.ok){toast('纪要已保存');await load();await sel(id);}
    else toast('失败：'+(d.error||''));
  }catch(e){toast('请求失败');}
}
async function deleteTranscript(id){
  if(!confirm('确定删除该会议的纪要原文？删除后将无法分析（OSS 上的文件保留）。'))return;
  try{
    const d=await (await fetch(`/api/meetings/${id}/transcript`,{method:'DELETE'})).json();
    if(d.ok){toast('纪要已删除');await load();await sel(id);}
    else toast('失败：'+(d.error||''));
  }catch(e){toast('请求失败');}
}
async function analyzeAll(){
  const b=document.getElementById('btnAll');b.disabled=true;b.innerHTML='<span class="spin"></span> 分析中';
  const src=document.getElementById('srcPick').value;
  const s=document.getElementById('crawlStart').value||'';
  const e=document.getElementById('crawlEnd').value||'';
  const qs=new URLSearchParams();
  if(src.split(',').length===1)qs.set('source',src);
  if(s)qs.set('start',s);
  if(e)qs.set('end',e);
  const r=await fetch('/api/analyze-all'+(qs.toString()?'?'+qs:''),{method:'POST'});
  const d=await r.json();
  b.disabled=false;b.textContent='一键分析全部';
  toast(`完成：分析 ${d.analyzed}，失败 ${d.failed}`);
  await load(); if(currentId) await sel(currentId);
}

async function openEmailSupervision(){
  const s=document.getElementById('crawlStart').value||'';
  const e=document.getElementById('crawlEnd').value||'';
  const src=document.getElementById('srcPick').value;
  const qs=new URLSearchParams();
  if(s)qs.set('start',s);
  if(e)qs.set('end',e);
  qs.set('sources',src);
  if(!confirm('确定向当前范围内未发送的会议主持人批量发送修改邮件吗？'))return;
  const b=document.getElementById('btnEmailSupervision');
  const old=b.textContent;
  b.disabled=true;
  b.innerHTML='<span class="spin"></span> 发送中';
  try{
    const d=await (await fetch('/api/request-supervision-edits?'+qs.toString(),{method:'POST'})).json();
    if(d.ok){
      toast(`发送完成：已发送 ${d.sent}，已发送跳过 ${d.skipped_sent}，已写入跳过 ${d.skipped_submitted}，未分析跳过 ${d.skipped_not_analyzed}`);
    }else{
      toast(`发送完成：已发送 ${d.sent}，失败 ${d.failed}`);
    }
    await load(); if(currentId) await sel(currentId);
  }catch(e){toast('发送失败，请确认服务和邮件配置');}
  finally{b.disabled=false;b.textContent=old;}
}

async function crawlRange(){
  const s=document.getElementById('crawlStart').value||'';
  const e=document.getElementById('crawlEnd').value||'';
  const src=document.getElementById('srcPick').value;
  const qs=new URLSearchParams();
  if(s)qs.set('start',s);
  if(e)qs.set('end',e);
  qs.set('sources',src);
  try{
    const d=await (await fetch('/api/crawl'+(qs.toString()?'?'+qs:''),{method:'POST'})).json();
    const st=d.started||{};
    const cn=k=>k==='tencent'?'腾讯':'飞书';
    const started=Object.keys(st).filter(k=>st[k]);
    const busy=Object.keys(st).filter(k=>!st[k]);
    let msg='';
    if(started.length) msg+='已开始采集 '+started.map(cn).join('、');
    if(busy.length) msg+=(msg?'；':'')+busy.map(cn).join('、')+'已在运行';
    toast(msg||'无操作');
    startStatusLoop();
  }catch(err){toast('请求失败，请确认服务在运行');}
}
// 腾讯/飞书各自独立 Runner，可并发；这里轮询所有来源状态，按钮始终可点（不 disable）
let statusTimer=null, prevRunning=false;
function startStatusLoop(){ if(statusTimer) return; statusTimer=setInterval(pollStatus,2000); pollStatus(); }
async function pollStatus(){
  try{
    const d=await (await fetch('/api/crawl/status')).json();
    const rs=d.runners||{};
    const b=document.getElementById('btnCrawl');
    const cn=k=>k==='tencent'?'腾讯':'飞书';
    // 只看【当前选中】的来源；切选择器即切换显示
    const picked=document.getElementById('srcPick').value.split(',').filter(Boolean);
    const run=Object.entries(rs).filter(([k,v])=>v.running && picked.includes(k));
    if(run.length){
      const parts=run.map(([k,v])=>{
        const p=v.progress||{done:0,total:0}; const ph=v.phase||'';
        if(ph.indexOf('分析')>=0) return cn(k)+' '+ph;
        return cn(k)+' '+(p.total?(p.done+'/'+p.total):'…');
      });
      b.innerHTML='<span class="spin"></span> '+parts.join(' · ');
    }else{
      b.textContent='采集该范围';
    }
    await load(); if(currentId) await sel(currentId);
    // 全部停下时给一次完成汇总（任一在跑→全停 才提示）
    if(prevRunning && !d.running){
      const sums=Object.entries(rs).map(([k,v])=>{
        const l=v.last||{};
        return cn(k)+':新增'+(l.created||0)+'/跳过'+(l.skipped||0)+(l.error?'/失败':'');
      }).join('；');
      toast('采集完成 → '+sums);
    }
    prevRunning=d.running;
    if(!d.running && statusTimer){ clearInterval(statusTimer); statusTimer=null; }
  }catch(e){/* 网络抖动，下次再试 */}
}

// —— 督办回填：手动触发，按来源选择，显示 督办 3/11 进度 ——
async function superviseRun(){
  const src=document.getElementById('srcPick').value.split(',').filter(Boolean);
  const srcParam = src.length>1 ? '' : ('?source='+src[0]);
  try{
    const d=await (await fetch('/api/supervise'+srcParam,{method:'POST'})).json();
    if(!d.started){toast('督办已在运行中…');return;}
    toast('开始督办回填（'+(src.length>1?'全部':(src[0]==='tencent'?'腾讯':'飞书'))+'）');
    pollSupervise();
  }catch(e){toast('请求失败');}
}
let supTimer=null;
function pollSupervise(){
  const b=document.getElementById('btnSup'); if(supTimer) return;
  supTimer=setInterval(async()=>{
    try{
      const d=await (await fetch('/api/supervise/status')).json();
      const p=d.progress||{done:0,total:0}, ph=d.phase||'';
      b.innerHTML = p.total ? ('<span class="spin"></span> 督办 '+p.done+'/'+p.total) : (ph?'<span class="spin"></span> '+ph:'<span class="spin"></span> 督办中');
      if(!d.running){
        clearInterval(supTimer); supTimer=null;
        b.textContent='一键全部督办';
        const l=d.last||{};
        if(l.error){toast('督办失败：'+l.error);}
        else{toast('督办完成：写入 '+(typeof l.written==='number'?l.written:0)+'，标记已督办 '+(l.marked_supervised||0)+'，无人员跳过 '+(l.skipped_no_person||0));}
      }
    }catch(e){}
  },2000);
}

// —— Notion 同步：复用前面日期范围（默认今天）纪要原文+AI摘要写入 Notion ——
function _todayStr(){const d=new Date();return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');}
async function notionSyncRun(){
  const b=document.getElementById('btnNotionSync'); if(b.disabled) return;
  const s=document.getElementById('crawlStart').value||'';
  const e=document.getElementById('crawlEnd').value||'';
  const qs=new URLSearchParams();
  if(s)qs.set('start',s); if(e)qs.set('end',e);
  const ds = s||e ? (s||e) : '今天';
  try{
    const d=await (await fetch('/api/notion/sync'+(qs.toString()?'?'+qs:''),{method:'POST'})).json();
    if(!d.started){toast('Notion 同步已在运行中…');return;}
    toast('开始同步 '+ds+' 到 Notion…');
    pollNotionSync();
  }catch(err){toast('请求失败，请确认服务在运行');}
}
let notionTimer=null;
function pollNotionSync(){
  const b=document.getElementById('btnNotionSync'); if(notionTimer) return;
  b.disabled=true;
  notionTimer=setInterval(async()=>{
    try{
      const d=await (await fetch('/api/notion/sync/status')).json();
      const p=d.progress||{done:0,total:0}, ph=d.phase||'';
      b.innerHTML = p.total ? ('<span class="spin"></span> 同步 '+p.done+'/'+p.total) : (ph?'<span class="spin"></span> '+ph:'<span class="spin"></span> 同步中');
      if(!d.running){
        clearInterval(notionTimer); notionTimer=null;
        b.disabled=false; b.textContent='同步到 Notion';
        const l=d.last||{};
        if(l.error){toast('同步失败：'+l.error);}
        else{toast('同步完成：'+(l.meetings_written||0)+' 场 / '+(l.pages_created||0)+' 页'+(l.failed?('，失败 '+(l.failed||0)):''));}
      }
    }catch(e){}
  },2000);
}

function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1700);}
// —— 轻量 Markdown 渲染（仅支持纪要用到的子集：标题/加粗/列表/表格/段落/行内代码）——
function mdToHtml(md){
  if(!md) return '';
  const esc2=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const inline=s=>esc2(s).replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/`([^`]+)`/g,'<code>$1</code>');
  const lines=String(md).split(String.fromCharCode(10));
  const out=[]; let i=0;
  const stripMark=s=>s.replace(/^\s*([-*]\s+|\d+\.\s+)/,'');
  while(i<lines.length){
    const line=lines[i];
    if(!line.trim()){i++;continue;}
    const h=line.match(/^(#{1,4})\s+(.*)$/);
    if(h){const lv=Math.min(h[1].length,4);out.push('<h'+lv+'>'+inline(h[2])+'</h'+lv+'>');i++;continue;}
    if(/^\s*\|.*\|\s*$/.test(line)){
      const rows=[];while(i<lines.length&&/^\s*\|.*\|\s*$/.test(lines[i])){rows.push(lines[i]);i++;}
      let cells=rows.map(r=>r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>c.trim()));
      cells=cells.filter(r=>!(r.length&&r.every(c=>/^:?-+:?$/.test(c))));
      if(cells.length){const th=cells[0],tb=cells.slice(1);
        out.push('<table><thead><tr>'+th.map(c=>'<th>'+inline(c)+'</th>').join('')+'</tr></thead><tbody>'+tb.map(r=>'<tr>'+r.map(c=>'<td>'+inline(c)+'</td>').join('')+'</tr>').join('')+'</tbody></table>');}
      continue;
    }
    if(/^\s*[-*]\s+/.test(line)){const it=[];while(i<lines.length&&/^\s*[-*]\s+/.test(lines[i])){it.push(lines[i]);i++;}out.push('<ul>'+it.map(x=>'<li>'+inline(stripMark(x))+'</li>').join('')+'</ul>');continue;}
    if(/^\s*\d+\.\s+/.test(line)){const it=[];while(i<lines.length&&/^\s*\d+\.\s+/.test(lines[i])){it.push(lines[i]);i++;}out.push('<ol>'+it.map(x=>'<li>'+inline(stripMark(x))+'</li>').join('')+'</ol>');continue;}
    const para=[];while(i<lines.length&&lines[i].trim()&&!/^(#{1,4}\s|\s*[-*]\s+|\s*\d+\.\s+|\s*\|)/.test(lines[i])){para.push(lines[i]);i++;}
    if(para.length)out.push('<p>'+inline(para.join(' '))+'</p>');
  }
  return out.join(String.fromCharCode(10));
}
function zoomSummary(){
  const box=document.getElementById('summaryBox'); if(!box)return;
  document.getElementById('sumModalBody').innerHTML=box.innerHTML;
  document.getElementById('sumModal').classList.add('show');
  document.body.style.overflow='hidden';
}
function closeZoomSummary(){
  document.getElementById('sumModal').classList.remove('show');
  document.body.style.overflow='';
}
async function copySummary(){
  const txt=_curSumMd||'';
  if(!txt){toast('无内容');return;}
  try{
    await navigator.clipboard.writeText(txt);
    toast('已复制全文 '+txt.length+' 字');
  }catch(e){
    const ta=document.createElement('textarea');ta.value=txt;ta.style.position='fixed';ta.style.top='-9999px';
    document.body.appendChild(ta);ta.focus();ta.select();
    try{document.execCommand('copy');toast('已复制全文 '+txt.length+' 字');}catch(e2){toast('复制失败，请手动选中');}
    document.body.removeChild(ta);
  }
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeZoomSummary();});
Object.assign(window,{
  sel,analyzeOne,toggleEditTranscript,uploadTranscriptFile,saveTranscriptText,
  deleteTranscript,analyzeAll,crawlRange,pollStatus,startStatusLoop,
  superviseRun,pollSupervise,openEmailSupervision
});
document.getElementById('btnCrawl').addEventListener('click',crawlRange);
document.getElementById('btnAll').addEventListener('click',analyzeAll);
document.getElementById('btnEmailSupervision').addEventListener('click',openEmailSupervision);
document.getElementById('srcPick').addEventListener('change',pollStatus);
document.getElementById('search').addEventListener('input',renderList);
document.getElementById('fDateFrom').addEventListener('change',renderList);
document.getElementById('fDateTo').addEventListener('change',renderList);
document.getElementById('fSource').addEventListener('change',renderList);
document.getElementById('fStatus').addEventListener('change',renderList);
// 采集范围默认今天（本地时区，避免 UTC 偏差）
const _now=new Date();
const _iso=_now.getFullYear()+'-'+String(_now.getMonth()+1).padStart(2,'0')+'-'+String(_now.getDate()).padStart(2,'0');
document.getElementById('crawlStart').value=_iso;
document.getElementById('crawlEnd').value=_iso;
load();
</script>
</body>
</html>""".replace("__LLM__", _llm_mode()).replace("__USER__", html_escape(session.get("user", "")))


# ---------------------------------------------------------------------------
# 飞书授权与妙记采集 管理页
# ---------------------------------------------------------------------------

def _owner_stats_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>会议召集人统计</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
  :root{--bg:#f6f8fb;--card:#fff;--text:#172033;--muted:#64748b;--primary:#2563eb;--border:#dbe3ef;--shadow:0 1px 3px rgba(15,23,42,.06),0 1px 2px rgba(15,23,42,.04)}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI","Microsoft YaHei",Arial,sans-serif}
  .top{background:#0f172a;color:#fff;padding:13px 22px;display:flex;justify-content:space-between;align-items:center;gap:12px}
  .top h1{font-size:16px;margin:0}.top a{color:#e2e8f0;text-decoration:none;font-size:13px}
  .wrap{max-width:1180px;margin:20px auto;padding:0 16px 48px}
  .toolbar{background:#fff;border:1px solid var(--border);border-radius:10px;padding:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;box-shadow:var(--shadow);margin-bottom:12px}
  input,select{font:inherit;font-size:13px;padding:8px 9px;border:1px solid var(--border);border-radius:8px;background:#fff}
  .btn{font:inherit;padding:8px 13px;border-radius:8px;border:1px solid var(--border);background:#fff;cursor:pointer;font-weight:650}
  .btn.primary{background:var(--primary);color:#fff;border-color:var(--primary)}
  .panel{background:#fff;border:1px solid var(--border);border-radius:12px;padding:14px;box-shadow:var(--shadow);margin-bottom:12px}
  #chart{height:420px;width:100%}
  table{width:100%;border-collapse:collapse;font-size:13px;background:#fff}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border)}
  th{font-size:11px;color:var(--muted);font-weight:700}
  .muted{color:var(--muted);font-size:12px}
</style>
</head>
<body>
<div class="top"><h1>会议召集人统计</h1><a href="/">返回会议管理</a></div>
<div class="wrap">
  <div class="toolbar">
    <select id="owner" onchange="render()"><option value="">全部召集人</option></select>
    <input type="date" id="dateFrom" onchange="render()">
    <span class="muted">~</span>
    <input type="date" id="dateTo" onchange="render()">
    <select id="source" onchange="render()">
      <option value="">全部来源</option>
      <option value="feishu_minutes">飞书妙记</option>
      <option value="tencent_meeting">腾讯会议</option>
    </select>
    <button class="btn primary" onclick="exportData()">导出数据</button>
    <span class="muted" id="count"></span>
  </div>
  <div class="panel"><div id="chart"></div></div>
  <div class="panel">
    <table><thead><tr><th>召集人</th><th>会议数</th><th>已分析</th><th>已督办</th><th>待办数</th></tr></thead><tbody id="tbody"></tbody></table>
  </div>
</div>
<script>
let ALL=[], chart=null;
const SRC={feishu_minutes:'飞书妙记',tencent_meeting:'腾讯会议'};
(function(){const _f=window.fetch;window.fetch=function(){return _f.apply(this,arguments).then(function(r){if(r.status===401&&location.pathname!=='/login'){location.href='/login';}return r;});};})();
async function load(){
  const d=await (await fetch('/api/meetings')).json();
  ALL=d.meetings||[];
  const owners=[...new Set(ALL.map(m=>m.convener||'未填写'))].sort();
  document.getElementById('owner').innerHTML='<option value="">全部召集人</option>'+owners.map(o=>`<option value="${esc(o)}">${esc(o)}</option>`).join('');
  chart=echarts.init(document.getElementById('chart'));
  render();
}
function filtered(){
  const owner=document.getElementById('owner').value;
  const source=document.getElementById('source').value;
  const df=document.getElementById('dateFrom').value||'';
  const dt=document.getElementById('dateTo').value||'';
  let ms=ALL.slice();
  if(owner)ms=ms.filter(m=>(m.convener||'未填写')===owner);
  if(source)ms=ms.filter(m=>m.source===source);
  if(df)ms=ms.filter(m=>(m.start_time||'')>=df);
  if(dt)ms=ms.filter(m=>(m.start_time||'')<=dt);
  return ms;
}
function grouped(ms){
  const map={};
  ms.forEach(m=>{
    const k=m.convener||'未填写';
    if(!map[k])map[k]={owner:k,count:0,analyzed:0,supervised:0,todos:0};
    map[k].count++;
    if(m.analyzed)map[k].analyzed++;
    if(m.supervised)map[k].supervised++;
    map[k].todos+=m.action_count||0;
  });
  return Object.values(map).sort((a,b)=>b.count-a.count||a.owner.localeCompare(b.owner));
}
function render(){
  const ms=filtered(), rows=grouped(ms);
  document.getElementById('count').textContent='共 '+ms.length+' 场会议，'+rows.length+' 位召集人';
  chart.setOption({
    tooltip:{trigger:'axis'},
    legend:{top:0},
    grid:{left:45,right:18,top:42,bottom:90},
    xAxis:{type:'category',data:rows.map(r=>r.owner),axisLabel:{rotate:35}},
    yAxis:{type:'value',minInterval:1},
    series:[
      {name:'会议数',type:'bar',data:rows.map(r=>r.count),itemStyle:{color:'#2563eb'}},
      {name:'待办数',type:'bar',data:rows.map(r=>r.todos),itemStyle:{color:'#d97706'}}
    ]
  });
  document.getElementById('tbody').innerHTML=rows.map(r=>`<tr><td>${esc(r.owner)}</td><td>${r.count}</td><td>${r.analyzed}</td><td>${r.supervised}</td><td>${r.todos}</td></tr>`).join('');
}
function exportData(){
  const ms=filtered();
  const header=['会议ID','来源','会议名称','会议召集人','开始时间','状态','待办数','已督办','纪要链接','录制链接'];
  const rows=ms.map(m=>[m.meeting_id,SRC[m.source]||m.source,(m.display_title||m.title),m.convener||'',m.start_time,m.status,m.action_count||0,m.supervised?'是':'否',m.content_url,m.media_url]);
  const csv=[header,...rows].map(r=>r.map(v=>`"${String(v==null?'':v).replace(/"/g,'""')}"`).join(',')).join('\\n');
  const blob=new Blob(['\\ufeff'+csv],{type:'text/csv;charset=utf-8'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='会议召集人统计.csv';a.click();URL.revokeObjectURL(a.href);
}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
load();
</script>
</body>
</html>"""


def _supervision_range_html(*, date_from: str, date_to: str, sources: str) -> str:
    source_map = {"tencent": MeetingSource.TENCENT_MEETING, "feishu": MeetingSource.FEISHU_MINUTES}
    picked = [s for s in (sources or "tencent,feishu").split(",") if s in source_map]
    if not picked:
        picked = ["tencent", "feishu"]

    meetings = []
    store = _store()
    for src in picked:
        meetings.extend(store.list(
            source=source_map[src],
            date_from=date_from or None,
            date_to=date_to or None,
        ))
    meetings.sort(key=lambda m: m.start_time or "", reverse=True)

    rows = []
    for m in meetings:
        if m.supervised:
            state = '<span class="tag done">已督办</span>'
            action = '<span class="muted">已填写并发送</span>'
        elif m.status != MeetingStatus.ANALYZED:
            state = '<span class="tag wait">未分析</span>'
            action = '<span class="muted">请先分析会议</span>'
        else:
            state = '<span class="tag open">待填写</span>'
            action = f'<button class="btn primary" onclick="requestEdit(\'{m.meeting_id}\',this)">发送给主持人修改</button>'
        rows.append(f"""
          <tr>
            <td>{(m.start_time or '')[:16]}</td>
            <td>{_html_escape(display_title(m))}</td>
            <td>{_html_escape(m.owner or '')}</td>
            <td>{m.source.value}</td>
            <td>{state}</td>
            <td>{action}</td>
          </tr>
        """)
    selected_label = "、".join("腾讯会议" if s == "tencent" else "飞书妙记" for s in picked)
    empty = '<tr><td colspan="6" class="empty">该范围内没有会议</td></tr>'
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>范围邮件督办</title>
<style>
  :root{{--bg:#f6f8fb;--card:#fff;--text:#172033;--muted:#64748b;--primary:#2563eb;--border:#dbe3ef;--ok:#059669;--warn:#d97706}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI","Microsoft YaHei",Arial,sans-serif}}
  .top{{background:#0f172a;color:#fff;padding:13px 22px;display:flex;justify-content:space-between;align-items:center;gap:12px}}
  .top h1{{font-size:16px;margin:0}} .top a{{color:#e2e8f0;text-decoration:none;font-size:13px}}
  .wrap{{max-width:1120px;margin:22px auto;padding:0 16px 48px}}
  .panel{{background:#fff;border:1px solid var(--border);border-radius:12px;padding:18px;box-shadow:0 1px 3px rgba(15,23,42,.06)}}
  .meta{{color:var(--muted);font-size:13px;margin:4px 0 14px;line-height:1.7}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid var(--border);vertical-align:middle}}
  th{{color:var(--muted);font-weight:700;font-size:12px;background:#f8fafc}}
  .btn{{font:inherit;padding:7px 12px;border-radius:8px;border:1px solid var(--border);background:#fff;cursor:pointer;font-weight:650;text-decoration:none;color:var(--text);display:inline-block}}
  .btn.primary{{background:var(--primary);border-color:var(--primary);color:#fff}}
  .tag{{display:inline-block;padding:3px 9px;border-radius:999px;font-size:11px;font-weight:700;color:#fff}}
  .tag.done{{background:var(--ok)}} .tag.open{{background:var(--primary)}} .tag.wait{{background:var(--warn)}}
  .muted{{color:var(--muted);font-size:12px}} .empty{{text-align:center;color:var(--muted);padding:42px}}
  @media(max-width:760px){{.wrap{{padding:0 10px}} table{{font-size:12px}} th,td{{padding:8px 6px}}}}
</style>
</head>
<body>
<div class="top"><h1>范围邮件督办</h1><a href="/">返回会议管理</a></div>
<div class="wrap">
  <div class="panel">
    <div class="meta">
      来源：{selected_label}；日期范围：{_html_escape(date_from or "不限")} ~ {_html_escape(date_to or "不限")}；共 {len(meetings)} 场会议。<br>
      这里按范围列出会议；点击发送后，系统会把会议详情和修改链接发给会议主持人。主持人在网页填写完成后，才会写入飞书督办表。
    </div>
    <table>
      <thead><tr><th>时间</th><th>会议名称</th><th>召集人</th><th>来源</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>{''.join(rows) if rows else empty}</tbody>
    </table>
  </div>
</div>
<script>
async function requestEdit(id,btn){{
  if(!confirm('确定将该会议详情发送给主持人修改吗？'))return;
  const old=btn.textContent;
  btn.disabled=true;btn.textContent='发送中...';
  try{{
    const d=await (await fetch('/api/meetings/'+id+'/request-supervision-edit',{{method:'POST'}})).json();
    if(d.ok){{btn.textContent='已发送';btn.classList.remove('primary');alert('已发送给：'+(d.receiver||d.email_to||''));}}
    else {{alert('发送失败：'+(d.error||''));btn.disabled=false;btn.textContent=old;}}
  }}catch(e){{alert('发送失败');btn.disabled=false;btn.textContent=old;}}
}}
</script>
</body>
</html>"""


def _supervision_html(meeting) -> str:
    payload = meeting.to_dict()
    safe_payload = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    # 人员名单仅取姓名，服务端内联进页面（公开页不调用 /api/staff，避免暴露通讯录/邮箱）
    try:
        staff_names = sorted({item["name"] for item in list_staff_options() if item.get("name")})
    except Exception:  # noqa: BLE001
        staff_names = []
    safe_staff = json.dumps([{"name": n} for n in staff_names], ensure_ascii=False)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>发起邮件督办</title>
<style>
  :root{{--bg:#f5f7fb;--card:#fff;--text:#172033;--muted:#64748b;--primary:#2563eb;--border:#dbe3ef;--danger:#dc2626;--ok:#059669;--soft:#f8fafc;--line:#edf2f7}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI","Microsoft YaHei",Arial,sans-serif}}
  .top{{background:#111827;color:#fff;padding:13px 22px;display:flex;justify-content:space-between;align-items:center;gap:12px;box-shadow:0 1px 0 rgba(255,255,255,.06) inset}}
  .top h1{{font-size:16px;margin:0;letter-spacing:0}} .top a{{color:#e2e8f0;text-decoration:none;font-size:13px}}
  .wrap{{max-width:1180px;margin:22px auto;padding:0 18px 92px}}
  .panel{{background:#fff;border:1px solid var(--border);border-radius:8px;padding:18px;margin-bottom:14px;box-shadow:0 2px 10px rgba(15,23,42,.04)}}
  .hero{{display:grid;grid-template-columns:1fr auto;gap:14px;align-items:center;border-left:4px solid var(--primary)}}
  .eyebrow{{font-size:12px;color:var(--primary);font-weight:800;margin-bottom:5px}}
  .hero-title{{font-size:22px;font-weight:800;line-height:1.3;margin:0 0 8px;letter-spacing:0}}
  .status-pill{{font-size:12px;font-weight:800;color:#047857;background:#ecfdf5;border:1px solid #bbf7d0;border-radius:999px;padding:5px 10px;white-space:nowrap}}
  .section-head{{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--line)}}
  .section-head h2{{margin:0;font-size:15px}}
  label{{display:block;font-size:12px;color:var(--muted);font-weight:700;margin-bottom:6px}}
  input,select,textarea{{width:100%;font:inherit;border:1px solid var(--border);border-radius:8px;padding:9px 10px;background:#fff}}
  input:focus,select:focus,textarea:focus{{outline:2px solid rgba(37,99,235,.18);border-color:var(--primary)}}
  input[readonly]{{background:#f1f5f9;color:#475569;cursor:not-allowed}}
  input[list]{{background:#fff url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%2364748b' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='11' cy='11' r='8'/%3E%3Cpath d='m21 21-4.3-4.3'/%3E%3C/svg%3E") no-repeat right 10px center;padding-right:34px}}
  textarea{{min-height:96px;resize:vertical;line-height:1.55}}
  .grid{{display:grid;grid-template-columns:1.4fr .8fr .8fr;gap:12px}}
  .hint{{font-size:12px;color:var(--muted);line-height:1.7;margin:8px 0 0}}
  .info-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}}
  .info-item{{background:var(--soft);border:1px solid var(--line);border-radius:8px;padding:12px;min-width:0}}
  .info-label{{font-size:12px;color:var(--muted);font-weight:800;margin-bottom:5px}}
  .info-value{{font-size:13px;line-height:1.55;word-break:break-all}}
  .info-value a{{color:var(--primary);font-weight:700;text-decoration:none}}
  .media-box{{margin-top:12px;border:1px solid var(--border);border-radius:8px;overflow:hidden;background:#111827}}
  .media-box video,.media-box audio{{display:block;width:100%;max-height:300px;background:#0f172a}}
  .media-empty{{color:var(--muted);font-size:13px;padding:12px;border:1px dashed var(--border);border-radius:8px;background:var(--soft)}}
  .person-field{{position:relative}}
  .person-field input{{background:#fff;padding-right:34px}}
  .person-field:after{{content:"";position:absolute;right:12px;top:33px;width:14px;height:14px;background:no-repeat center/14px 14px url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%2364748b' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");pointer-events:none}}
  .person-menu{{display:none;position:absolute;left:0;right:0;top:calc(100% + 4px);z-index:20;max-height:220px;overflow:auto;background:#fff;border:1px solid var(--border);border-radius:8px;box-shadow:0 12px 26px rgba(15,23,42,.14);padding:4px}}
  .person-option{{width:100%;border:0;background:#fff;text-align:left;padding:8px 10px;border-radius:6px;cursor:pointer;font:inherit;font-size:13px;color:var(--text)}}
  .person-option:hover{{background:#eff6ff;color:#1d4ed8}}
  .person-empty{{padding:9px 10px;color:var(--muted);font-size:12px}}
  .task-list{{display:flex;flex-direction:column;gap:10px}}
  .row{{display:grid;grid-template-columns:minmax(380px,2fr) minmax(240px,1fr) 180px 42px;gap:10px;align-items:end;background:#fbfdff;border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:0}}
  .row:hover{{border-color:#bfdbfe;box-shadow:0 3px 12px rgba(37,99,235,.06)}}
  .row .wide{{min-width:0}}
  .hidden{{display:none!important}}
  .btn{{font:inherit;padding:8px 13px;border-radius:8px;border:1px solid var(--border);background:#fff;cursor:pointer;font-weight:650}}
  .btn.primary{{background:var(--primary);border-color:var(--primary);color:#fff}}
  .btn.danger{{color:var(--danger);border-color:#fecaca}}
  .btn.ok{{background:var(--ok);border-color:var(--ok);color:#fff}}
  .muted{{color:var(--muted);font-size:12px;line-height:1.6}}
  .empty-state{{padding:18px;border:1px dashed var(--border);border-radius:8px;background:#fbfdff;color:var(--muted);font-size:13px;line-height:1.7}}
  .actions{{position:fixed;left:0;right:0;bottom:0;background:rgba(255,255,255,.94);border-top:1px solid var(--border);padding:12px 18px;display:flex;gap:8px;justify-content:center;flex-wrap:wrap;backdrop-filter:blur(8px)}}
  .toast{{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:#0f172a;color:#fff;padding:9px 18px;border-radius:9px;font-size:13px;opacity:0;transition:.2s;pointer-events:none}}
  .toast.show{{opacity:1}}
  @media(max-width:980px){{.hero,.grid,.row,.info-grid{{grid-template-columns:1fr}}.status-pill{{justify-self:start}}}}
  .summary-card{{background:#f8fafc;border:1px solid var(--border);border-radius:9px}}
  .summary-card .sum-bar{{display:flex;justify-content:space-between;align-items:center;padding:7px 11px;border-bottom:1px solid var(--border)}}
  .summary-card .sum-bar .hint{{font-size:11px;color:var(--muted)}}
  .summary-card .summary{{border:0;border-radius:0;max-height:260px;overflow:auto;white-space:normal;padding:11px}}
  .summary-md h1{{font-size:17px;margin:10px 0 6px}} .summary-md h2{{font-size:15px;margin:12px 0 5px;border-left:3px solid var(--primary);padding-left:8px}} .summary-md h3{{font-size:14px;margin:10px 0 4px;color:#334155}}
  .summary-md p{{margin:5px 0}} .summary-md ul,.summary-md ol{{margin:5px 0;padding-left:20px}} .summary-md li{{margin:3px 0}}
  .summary-md strong{{color:#0f172a}} .summary-md code{{background:#e2e8f0;padding:1px 5px;border-radius:4px;font-size:12px}}
  .summary-md table{{margin:6px 0;border-collapse:collapse}} .summary-md th,.summary-md td{{font-size:12px;border:1px solid var(--border);padding:4px 7px}}
  .raw{{white-space:pre-wrap;font-size:12px;line-height:1.6;color:#334155;background:var(--soft);border:1px solid var(--line);border-radius:8px;padding:10px;max-height:320px;overflow:auto}}
  .modal{{position:fixed;inset:0;background:rgba(15,23,42,.55);display:none;z-index:100;align-items:center;justify-content:center;padding:24px}}
  .modal.show{{display:flex}}
  .modal-box{{background:#fff;border-radius:14px;max-width:880px;width:100%;max-height:88vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.3)}}
  .modal-head{{display:flex;justify-content:space-between;align-items:center;padding:13px 18px;border-bottom:1px solid var(--border)}}
  .modal-head .t{{font-weight:700;font-size:15px}}
  .modal-close{{background:#f1f5f9;border:1px solid var(--border);border-radius:8px;width:30px;height:30px;cursor:pointer;font-size:15px;color:#475569}}
  .modal-close:hover{{background:#e2e8f0}}
  .modal-body{{padding:18px 22px;overflow:auto;line-height:1.75;font-size:14px}}
</style>
</head>
<body>
<div class="top"><h1>会议督办填写</h1><a href="/">返回会议管理</a></div>
<datalist id="staffList"></datalist>
<div class="wrap">
  <div class="panel hero">
    <div>
      <div class="eyebrow">主持人填写页</div>
      <div class="hero-title">请确认会议名称、召集人、主持人和待办督办项</div>
      <div class="muted">原会议名称：{_html_escape(meeting.title)}</div>
    </div>
    <div class="status-pill">提交后写入督办表</div>
  </div>

  <div class="panel">
    <div id="filledNotice" class="muted" style="display:none;margin-bottom:12px;color:#047857;font-weight:700"></div>
    <div class="section-head"><h2>会议信息</h2></div>
    <div class="grid">
      <div><label>会议主题（必填）</label><input id="title"></div>
      <div class="person-field"><label>会议召集人（必填）</label><input id="convener" placeholder="搜索并选择会议召集人" autocomplete="off" onfocus="showStaffMenu(this)" oninput="showStaffMenu(this)"><div class="person-menu"></div></div>
      <div class="person-field"><label>会议主持人（必填）</label><input id="responsible" placeholder="搜索并选择会议主持人" autocomplete="off" onfocus="showStaffMenu(this)" oninput="showStaffMenu(this)"><div class="person-menu"></div></div>
    </div>
    <p class="hint">会议主题、会议召集人和会议主持人必填；会议召集人和会议主持人需从飞书人员名单中搜索选择。</p>
  </div>

  <div class="panel">
    <div class="section-head"><h2>会议资料</h2></div>
    <div class="info-grid">
      <div class="info-item">
        <div class="info-label">会议时间</div>
        <div class="info-value" id="meetingTime"></div>
      </div>
      <div class="info-item">
        <div class="info-label">纪要链接</div>
        <div class="info-value" id="contentLink"></div>
      </div>
      <div class="info-item">
        <div class="info-label">视频或录音链接</div>
        <div class="info-value" id="mediaLink"></div>
      </div>
    </div>
    <div id="mediaPreview"></div>
  </div>

  <div class="panel">
    <div class="section-head"><h2>AI 智能纪要</h2></div>
    <div id="aiSummary"></div>
    <details id="rawWrap" style="margin-top:12px;display:none">
      <summary style="cursor:pointer;color:var(--muted);font-size:13px">查看纪要原文</summary>
      <div class="raw" id="rawPreview" style="margin-top:8px"></div>
    </details>
  </div>

  <div class="panel">
    <div class="section-head">
      <h2>待办督办项</h2>
      <button class="btn" onclick="addItem()">+ 新增待办</button>
    </div>
    <div id="items" class="task-list"></div>
    <p class="hint">待办项整体可以为空；如果保留或新增待办行，则内容不可为空。责任人只能从飞书人员名单选择；提交后事项自动视为已确定。</p>
  </div>

  <div class="actions">
    <button class="btn ok" id="sendBtn" onclick="sendNow()">提交并写入督办表</button>
    <button class="btn" onclick="location.reload()">重置</button>
  </div>
</div>
<div class="toast" id="toast"></div>
<div class="modal" id="sumModal" onclick="if(event.target===this)closeZoomSummary()">
  <div class="modal-box">
    <div class="modal-head"><span class="t">📋 AI 智能会议纪要</span><button class="modal-close" onclick="closeZoomSummary()">✕</button></div>
    <div class="modal-body summary-md" id="sumModalBody"></div>
  </div>
</div>
<script>
const meeting={safe_payload};
const originalTitle=meeting.title||'';
const lastPayload=meeting.supervision_payload||{{}};
const isFilled=!!(meeting.supervised&&Object.keys(lastPayload).length);
let editLocked=isFilled;
let STAFF={safe_staff};
document.getElementById('title').placeholder='原会议名称：'+originalTitle;
document.getElementById('title').value=isFilled?(lastPayload.title||''):'';
document.getElementById('convener').value=isFilled?(lastPayload.convener||meeting.owner||''):(meeting.owner||'');
document.getElementById('responsible').value=isFilled?(lastPayload.responsible||meeting.owner||''):(meeting.owner||'');
if(isFilled){{
  document.getElementById('filledNotice').style.display='block';
  document.getElementById('filledNotice').textContent='该会议已填写并写入过督办表，每个会议只能填写一次。';
  document.getElementById('sendBtn').style.display='none';
}}
function esc(s){{return String(s==null?'':s).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));}}
function toast(m){{const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1800);}}
function linkOrEmpty(url,text){{
  if(!url)return '<span style="color:var(--muted)">暂无</span>';
  return `<a href="${{esc(url)}}" target="_blank" rel="noopener">${{esc(text||url)}}</a>`;
}}
function renderMeetingAssets(){{
  const media=meeting.media_url||'';
  document.getElementById('meetingTime').textContent=meeting.start_time||'暂无';
  document.getElementById('contentLink').innerHTML=linkOrEmpty(meeting.content_url,'打开纪要');
  document.getElementById('mediaLink').innerHTML=linkOrEmpty(media,'打开视频或录音');
  const box=document.getElementById('mediaPreview');
  if(!media){{
    box.innerHTML='<div class="media-empty" style="margin-top:12px">暂无视频或录音文件。</div>';
    return;
  }}
  const lower=media.toLowerCase().split('?')[0];
  const isAudio=/\\.(mp3|m4a|wav|aac|ogg)$/.test(lower);
  box.innerHTML=`<div class="media-box">${{isAudio
    ? `<audio controls preload="metadata" src="${{esc(media)}}"></audio>`
    : `<video controls preload="metadata" src="${{esc(media)}}"></video>`}}</div>`;
}}
function staffNames(){{
  return [...new Set(STAFF.map(x=>String(x.name||'').trim()).filter(Boolean))];
}}
function renderStaffList(){{
  document.getElementById('staffList').innerHTML=staffNames().map(name=>`<option value="${{esc(name)}}"></option>`).join('');
}}
function showStaffMenu(input){{
  const field=input.closest('.person-field');
  const menu=field.querySelector('.person-menu');
  const q=input.value.trim().toLowerCase();
  const matches=staffNames().filter(name=>!q||name.toLowerCase().includes(q)).slice(0,24);
  menu.innerHTML=matches.length
    ? matches.map(name=>`<button type="button" class="person-option" data-name="${{esc(name)}}" onclick="chooseStaff(this)">${{esc(name)}}</button>`).join('')
    : '<div class="person-empty">没有匹配人员</div>';
  menu.style.display='block';
}}
function chooseStaff(btn){{
  const name=btn.dataset.name||'';
  const field=btn.closest('.person-field');
  const input=field.querySelector('input');
  input.value=name;
  field.querySelector('.person-menu').style.display='none';
  refreshDetail();
}}
document.addEventListener('click',e=>{{
  if(!e.target.closest('.person-field'))document.querySelectorAll('.person-menu').forEach(menu=>menu.style.display='none');
}});
function rowHtml(item={{}}){{
  const owner=String(item.owner||'').trim();
  return `<div class="row">
    <div class="wide"><label>待办内容</label><textarea class="it-content" placeholder="填写事项内容" oninput="refreshDetail()">${{esc(item.content||'')}}</textarea></div>
    <div class="person-field"><label>责任人</label><input class="it-owner" placeholder="搜索并选择责任人" value="${{esc(owner)}}" autocomplete="off" onfocus="showStaffMenu(this)" oninput="showStaffMenu(this);refreshDetail()"><div class="person-menu"></div></div>
    <div><label>截止日期</label><input class="it-due" type="text" placeholder="如：8月10日 / 本周内" value="${{esc(item.due||'')}}" oninput="refreshDetail()"></div>
    <button class="btn danger" title="删除该待办" onclick="this.closest('.row').remove();updateItemEmpty();refreshDetail()">删</button>
  </div>`;
}}
function updateItemEmpty(){{
  const box=document.getElementById('items');
  if(!box.querySelector('.row'))box.innerHTML='<div class="empty-state" id="emptyItems">暂无待办项。可直接提交会议信息，或点击“新增待办”补充需要督办的事项。</div>';
}}
function addItem(item){{
  const box=document.getElementById('items');
  const empty=document.getElementById('emptyItems');
  if(empty)empty.remove();
  box.insertAdjacentHTML('beforeend',rowHtml(item));
}}
function lockForm(locked){{
  editLocked=locked;
  document.querySelectorAll('input,select,textarea,#items button').forEach(el=>{{el.disabled=locked;}});
  document.getElementById('sendBtn').style.display=locked?'none':'inline-block';
}}
function readItems(){{
  return [...document.querySelectorAll('#items .row')].map(r=>({{
    content:r.querySelector('.it-content').value.trim(),
    owner:r.querySelector('.it-owner').value.trim(),
    due:r.querySelector('.it-due').value.trim(),
    confirmation:'已确定'
  }}));
}}
function refreshDetail(){{}}
function mdToHtml(md){{
  if(!md) return '';
  const esc2=s=>String(s).replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));
  const inline=s=>esc2(s).replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/`([^`]+)`/g,'<code>$1</code>');
  const lines=String(md).split(String.fromCharCode(10));
  const out=[]; let i=0;
  const stripMark=s=>s.replace(/^\s*([-*]\s+|\d+\.\s+)/,'');
  while(i<lines.length){{
    const line=lines[i];
    if(!line.trim()){{i++;continue;}}
    const h=line.match(/^(#{{1,4}})\s+(.*)$/);
    if(h){{const lv=Math.min(h[1].length,4);out.push('<h'+lv+'>'+inline(h[2])+'</h'+lv+'>');i++;continue;}}
    if(/^\s*\|.*\|\s*$/.test(line)){{
      const rows=[];while(i<lines.length&&/^\s*\|.*\|\s*$/.test(lines[i])){{rows.push(lines[i]);i++;}}
      let cells=rows.map(r=>r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>c.trim()));
      cells=cells.filter(r=>!(r.length&&r.every(c=>/^:?-+:?$/.test(c))));
      if(cells.length){{const th=cells[0],tb=cells.slice(1);
        out.push('<table><thead><tr>'+th.map(c=>'<th>'+inline(c)+'</th>').join('')+'</tr></thead><tbody>'+tb.map(r=>'<tr>'+r.map(c=>'<td>'+inline(c)+'</td>').join('')+'</tr>').join('')+'</tbody></table>');}}
      continue;
    }}
    if(/^\s*[-*]\s+/.test(line)){{const it=[];while(i<lines.length&&/^\s*[-*]\s+/.test(lines[i])){{it.push(lines[i]);i++;}}out.push('<ul>'+it.map(x=>'<li>'+inline(stripMark(x))+'</li>').join('')+'</ul>');continue;}}
    if(/^\s*\d+\.\s+/.test(line)){{const it=[];while(i<lines.length&&/^\s*\d+\.\s+/.test(lines[i])){{it.push(lines[i]);i++;}}out.push('<ol>'+it.map(x=>'<li>'+inline(stripMark(x))+'</li>').join('')+'</ol>');continue;}}
    const para=[];while(i<lines.length&&lines[i].trim()&&!/^(#{{1,4}}\s|\s*[-*]\s+|\s*\d+\.\s+|\s*\|)/.test(lines[i])){{para.push(lines[i]);i++;}}
    if(para.length)out.push('<p>'+inline(para.join(' '))+'</p>');
  }}
  return out.join(String.fromCharCode(10));
}}
function zoomSummary(){{
  const box=document.getElementById('summaryBox'); if(!box)return;
  document.getElementById('sumModalBody').innerHTML=box.innerHTML;
  document.getElementById('sumModal').classList.add('show');
  document.body.style.overflow='hidden';
}}
function closeZoomSummary(){{
  document.getElementById('sumModal').classList.remove('show');
  document.body.style.overflow='';
}}
function renderAISummary(){{
  const a=(meeting.analysis||{{}});
  const sum=a.summary||'';
  const box=document.getElementById('aiSummary');
  if(sum){{
    box.innerHTML=`<div class="summary-card"><div class="sum-bar"><span class="hint">默认预览（可上下滚动）· 点击右侧放大查看完整纪要</span><button class="btn" style="padding:4px 10px;font-size:12px" onclick="zoomSummary()">🔍 放大</button></div><div class="summary summary-md" id="summaryBox">${{mdToHtml(sum)}}</div></div>`;
  }}else{{
    box.innerHTML='<div class="empty-state">暂无 AI 摘要。可前往会议管理平台生成后再发起督办。</div>';
  }}
  const raw=meeting.raw_content||'';
  const wrap=document.getElementById('rawWrap');
  if(raw){{
    wrap.style.display='block';
    document.getElementById('rawPreview').textContent=raw.slice(0,4000)+(raw.length>4000?'\\n…(截断)':'');
  }}
}}
async function sendNow(){{
  if(editLocked){{toast('该会议已填写过，不能再次提交');return;}}
  const title=document.getElementById('title').value.trim();
  if(!title){{toast('会议主题必填');return;}}
  const convener=document.getElementById('convener').value.trim();
  if(!convener){{toast('会议召集人必填');return;}}
  if(title===originalTitle&&!confirm('会议名称与原名称没有变化，确定继续提交吗？')){{return;}}
  const items=readItems();
  for(let i=0;i<items.length;i++){{if(!items[i].content){{toast('第 '+(i+1)+' 条待办内容不可为空');return;}}}}
  const names=staffNames();
  if(!names.includes(convener)){{toast('会议召集人请从人员名单中选择');return;}}
  const responsible=document.getElementById('responsible').value.trim();
  if(!responsible){{toast('会议主持人必填');return;}}
  if(!names.includes(responsible)){{toast('会议主持人请从人员名单中选择');return;}}
  for(let i=0;i<items.length;i++){{
    if(!items[i].owner){{toast('第 '+(i+1)+' 条责任人必选');return;}}
    if(items[i].owner&&!names.includes(items[i].owner)){{toast('第 '+(i+1)+' 条责任人请从人员名单中选择');return;}}
  }}
  const btn=document.getElementById('sendBtn');btn.disabled=true;btn.textContent='提交中...';
  try{{
    const d=await (await fetch(`/api/meetings/${{meeting.meeting_id}}/email-supervision`,{{
      method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{
        title,
        convener,
        responsible,
        items
      }})
    }})).json();
    if(d.ok){{toast('已写入督办表');setTimeout(()=>location.href='/',900);}}
    else {{toast('提交失败：'+(d.error||''));btn.disabled=false;btn.textContent='提交并写入督办表';}}
  }}catch(e){{toast('请求失败');btn.disabled=false;btn.textContent='提交并写入督办表';}}
}}
async function init(){{
  renderMeetingAssets();
  renderAISummary();
  renderStaffList();
  (isFilled?(lastPayload.items||[]):((meeting.analysis&&meeting.analysis.action_items)||[])).forEach(addItem);
  updateItemEmpty();
  if(isFilled)lockForm(true);
  document.getElementById('title').addEventListener('input',refreshDetail);
  document.getElementById('convener').addEventListener('input',refreshDetail);
  document.getElementById('responsible').addEventListener('input',refreshDetail);
  refreshDetail();
}}
init();
</script>
</body>
</html>"""

def _feishu_page_html() -> str:
    auth_url = get_feishu_oauth_service().build_auth_url()
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>飞书授权管理 · 会议平台</title>
<style>
  :root{--bg:#f6f8fb;--card:#fff;--text:#1f2937;--muted:#6b7280;--primary:#2563eb;--border:#dbe3ef;--danger:#dc2626}
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,"Segoe UI","Microsoft YaHei",Arial;background:var(--bg);color:var(--text)}
  .top{background:#0f172a;color:#fff;padding:12px 22px;display:flex;align-items:center;justify-content:space-between}
  .top a{color:#f1f5f9;text-decoration:none;font-size:14px;font-weight:600}
  .top a:hover{color:#fff;text-decoration:underline}
  .top h1{margin:0;font-size:16px}
  .wrap{max-width:1000px;margin:24px auto;padding:0 16px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:18px 20px;margin-bottom:16px;box-shadow:0 6px 20px rgba(31,41,55,.05)}
  h2{margin:0 0 6px;font-size:17px} p.muted{color:var(--muted);margin:4px 0 12px;font-size:13px}
  .row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
  textarea{width:100%;min-height:54px;font:inherit;padding:8px;border:1px solid var(--border);border-radius:8px;background:#f8fafc}
  .btn{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--border);background:#fff;cursor:pointer;font-weight:600;text-decoration:none;color:var(--text)}
  .btn:hover{background:#f8fafc} .btn.primary{background:var(--primary);color:#fff;border-color:var(--primary)}
  .btn.danger{color:var(--danger);border-color:#fecaca}
  input[type=text]{font:inherit;padding:8px 10px;border:1px solid var(--border);border-radius:8px;min-width:90px}
  table{width:100%;border-collapse:collapse;margin-top:8px} th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--border);vertical-align:middle;font-size:13px}
  th{color:var(--muted);font-size:11px;text-transform:uppercase}
  .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px} .ok{background:#ecfdf5;color:#047857} .bad{background:#fef2f2;color:#b91c1c} .warn{background:#fef3c7;color:#b45309}
  .toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:#0f172a;color:#fff;padding:9px 18px;border-radius:9px;font-size:13px;opacity:0;transition:.2s;pointer-events:none;z-index:50}
  .toast.show{opacity:1}
</style>
</head>
<body>
<div class="top"><h1>飞书授权管理</h1>
  <div><a href="/">← 回仪表盘（采集/分析请到主页面）</a></div></div>

<div class="wrap">
  <div class="card">
    <h2>① 授权链接</h2>
    <p class="muted">把下面链接发给同事，对方授权后会自动出现在下方列表。<b>授权谁就抓谁的妙记</b>。采集与分析请在主仪表盘操作。</p>
    <textarea id="authUrl" readonly>__AUTHURL__</textarea>
    <div class="row" style="margin-top:8px">
      <button class="btn" onclick="copyTxt('authUrl',this)">复制授权链接</button>
    </div>
    <p class="muted" style="margin-top:10px">提示：登录自己的飞书账号，把上面授权链接复制进浏览器打开即可完成授权。</p>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between"><h2 style="margin:0">② 授权用户</h2>
      <div class="row"><button class="btn" onclick="loadUsers()">刷新</button>
      <button class="btn" onclick="refreshTokens()">刷新全部 token</button></div></div>
    <p class="muted">改名只影响归档文件名里的「姓名」。token 后台每小时自动刷新；access_token 过期会自动续。</p>
    <table><thead><tr><th>姓名</th><th>open_id</th><th>access_token</th><th>妙记权限</th><th>操作</th></tr></thead>
    <tbody id="utbody"><tr><td colspan="5" style="color:var(--muted)">加载中…</td></tr></tbody></table>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const $=id=>document.getElementById(id);
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function toast(m){const t=$('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1500);}
function copyTxt(id,btn){const t=$(id);t.select();document.execCommand('copy');btn.textContent='已复制';setTimeout(()=>btn.textContent='复制授权链接',1200);}
async function loadUsers(){
  try{
    const d=await (await fetch('/api/feishu/users')).json();
    const users=d.users||[];
    const tb=$('utbody');
    if(!users.length){tb.innerHTML='<tr><td colspan="5" style="color:var(--muted)">还没有授权用户，把上面的授权链接发出去吧。</td></tr>';return;}
    tb.innerHTML=users.map(u=>{
      const ex=u.access_token_expires_at;
      const tok=ex?('<span class="pill '+(new Date(ex)<=new Date()?'bad':'ok')+'">'+ex.replace('T',' ').slice(0,16)+'</span>'):'<span class="pill warn">未知</span>';
      const sc=u.has_minutes_scope?'<span class="pill ok">齐全</span>':'<span class="pill bad">缺失</span>';
      return '<tr>'+
        '<td><input type="text" value="'+esc(u.name||'')+'" id="nm_'+u.user_key+'"></td>'+
        '<td style="font-size:11px;color:var(--muted)">'+esc((u.open_id||u.user_key||'').slice(0,20))+'…</td>'+
        '<td>'+tok+'</td><td>'+sc+'</td>'+
        '<td><button class="btn" onclick="rename(\\''+u.user_key+'\\')">保存</button> <button class="btn danger" onclick="del(\\''+u.user_key+'\\',\\''+esc(u.name||'')+'\\')">删除</button></td>'+
      '</tr>';
    }).join('');
  }catch(e){$('utbody').innerHTML='<tr><td colspan="5" style="color:var(--muted)">加载失败</td></tr>';}
}
async function rename(k){const nm=$('nm_'+k).value;
  try{const d=await (await fetch('/api/feishu/users/'+encodeURIComponent(k),{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:nm})})).json();
    if(d.ok){toast('已保存');loadUsers();}else toast('失败：'+(d.error||''));}catch(e){toast('请求失败');}}
async function del(k,label){if(!confirm('删除授权用户「'+label+'」？删除后不再抓取（已上传文件不受影响）。'))return;
  try{const d=await (await fetch('/api/feishu/users/'+encodeURIComponent(k),{method:'DELETE'})).json();
    if(d.ok){toast('已删除');loadUsers();}else toast('失败：'+(d.error||''));}catch(e){toast('请求失败');}}
async function refreshTokens(){toast('正在刷新全部 token…');
  try{const d=await (await fetch('/api/feishu/users/refresh-tokens',{method:'POST'})).json();toast('刷新完成：成功 '+(d.refreshed||0)+'，失败 '+(d.failed||0));await loadUsers();}catch(e){toast('刷新失败');}}
loadUsers();
</script>
</body>
</html>""".replace("__AUTHURL__", auth_url)


def _configure_logging() -> None:
    """统一日志：INFO 级，含时间/级别/logger名/消息。

    - 让 meeting_scheduler / notion_sync / feishu_ingest 等后台线程的进度可见；
    - analyze 500 / Notion 同步失败 等会进同一日志；
    - werkzeug 请求日志继续走 root，不丢。
    幂等：重复调用不会叠加 handler。
    """
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, force=True)
    # werkzeug 的请求日志保持 INFO（默认就是），避免被刷太多时改成 WARNING：
    if os.getenv("APP_DEBUG", "false").lower() != "true":
        logging.getLogger("werkzeug").setLevel(logging.INFO)


if __name__ == "__main__":
    _configure_logging()
    log.info("启动会议管理平台：PORT=%s", os.getenv("PORT", "5050"))
    start_background_services()  # 启动每日定时采集 + 05:00 邮件督办
    # use_reloader=False：保证单进程，后台采集线程与定时调度不被重载杀掉
    create_app().run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5050")),
        debug=os.getenv("APP_DEBUG", "false").lower() == "true",
        use_reloader=False,
    )
