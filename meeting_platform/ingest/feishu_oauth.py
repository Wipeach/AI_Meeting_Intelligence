"""飞书 OAuth 授权 + 用户 token 存储（移植自 feishu_oauth_wh，补齐并发安全）。

负责：
- 构造授权链接、用 code 换 token、拉用户信息、持久化
- access_token 过期自动 refresh；refresh_token 过期提示重新授权
- 后台定时刷新所有授权用户 token（守护线程）

环境变量：
    FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_REDIRECT_URI
    FEISHU_TOKEN_STORE_PATH（默认 data/feishu_tokens.json）
    FEISHU_TOKEN_REFRESH_SECONDS（默认 3600）
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests

# 申请的 scope：基础 + 妙记四件套（搜索/元信息/文字记录/录音）
REQUESTED_SCOPES = [
    "auth:user.id:read",
    "offline_access",
    "contact:user.base:readonly",
    "minutes:minutes.search:read",
    "minutes:minutes.basic:read",
    "minutes:minutes.transcript:export",
    "minutes:minutes.media:export",
]
REQUIRED_MINUTES_SCOPES = [
    "minutes:minutes.search:read",
    "minutes:minutes.basic:read",
    "minutes:minutes.transcript:export",
    "minutes:minutes.media:export",
]

AUTH_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"


# ============================ 时间小工具 ============================

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_at(seconds: Optional[int]) -> Optional[str]:
    if seconds is None:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=int(seconds))).isoformat()


def _is_expired(iso_str: Optional[str], *, skew_seconds: int = 120) -> bool:
    if not iso_str:
        return True
    try:
        expires_at = datetime.fromisoformat(iso_str)
    except ValueError:
        return True
    return datetime.now(timezone.utc) >= (expires_at - timedelta(seconds=skew_seconds))


# ============================ 配置 ============================

@dataclass
class FeishuConfig:
    app_id: str = ""
    app_secret: str = ""
    redirect_uri: str = ""

    def __post_init__(self):
        self.app_id = self.app_id or os.getenv("FEISHU_APP_ID", "")
        self.app_secret = self.app_secret or os.getenv("FEISHU_APP_SECRET", "")
        self.redirect_uri = self.redirect_uri or os.getenv(
            "FEISHU_REDIRECT_URI", "http://localhost:5050/callback"
        )

    def validate(self) -> None:
        missing = [n for n, v in {
            "FEISHU_APP_ID": self.app_id,
            "FEISHU_APP_SECRET": self.app_secret,
            "FEISHU_REDIRECT_URI": self.redirect_uri,
        }.items() if not v]
        if missing:
            raise RuntimeError(f"飞书配置缺失：{', '.join(missing)}")

    @property
    def scope_string(self) -> str:
        return " ".join(REQUESTED_SCOPES)


# ============================ token 存储（原子写 + 类级锁）============================

class FeishuTokenStore:
    """JSON 存储：已授权用户及其 token。结构 {latest_user_key, users:{key:{user_info,tokens}}}。"""

    _write_lock = threading.Lock()  # 类级锁：所有实例共享，串行化写

    def __init__(self, file_path: str = ""):
        self.file_path = Path(file_path or os.getenv("FEISHU_TOKEN_STORE_PATH", "data/feishu_tokens.json"))
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            self._write_raw({"latest_user_key": None, "users": {}})

    def _read(self) -> Dict[str, Any]:
        if not self.file_path.exists():
            return {"latest_user_key": None, "users": {}}
        try:
            with self.file_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, ValueError):
            return {"latest_user_key": None, "users": {}}
        data.setdefault("users", {})
        return data

    def _write_raw(self, data: Dict[str, Any]) -> None:
        tmp = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.file_path)

    def upsert_user(self, user_key: str, *, user_info: Dict[str, Any], tokens: Dict[str, Any]) -> None:
        with self._write_lock:
            data = self._read()
            data.setdefault("users", {})[user_key] = {"user_info": user_info, "tokens": tokens}
            data["latest_user_key"] = user_key
            self._write_raw(data)

    def update_tokens(self, user_key: str, tokens: Dict[str, Any]) -> None:
        with self._write_lock:
            data = self._read()
            users = data.setdefault("users", {})
            if user_key not in users:
                raise KeyError(f"本地未找到飞书用户 {user_key}")
            users[user_key]["tokens"] = tokens
            data["latest_user_key"] = user_key
            self._write_raw(data)

    def update_user_info(self, user_key: str, *, user_info: Dict[str, Any]) -> None:
        with self._write_lock:
            data = self._read()
            users = data.setdefault("users", {})
            if user_key not in users:
                raise KeyError(f"本地未找到飞书用户 {user_key}")
            users[user_key]["user_info"] = {**(users[user_key].get("user_info") or {}), **user_info}
            self._write_raw(data)

    def get_user(self, user_key: str) -> Optional[Dict[str, Any]]:
        return self._read().get("users", {}).get(user_key)

    def list_users(self) -> Dict[str, Any]:
        return self._read().get("users", {})

    def delete_user(self, user_key: str) -> bool:
        with self._write_lock:
            data = self._read()
            users = data.get("users", {})
            existed = user_key in users
            users.pop(user_key, None)
            if data.get("latest_user_key") == user_key:
                data["latest_user_key"] = next(iter(users), None)
            self._write_raw(data)
            return existed


# ============================ OAuth 服务 ============================

class FeishuOAuthService:
    """飞书 OAuth：授权链接、换 token、刷新、用户信息、定时刷新。"""

    def __init__(self, config: Optional[FeishuConfig] = None, store: Optional[FeishuTokenStore] = None):
        self.config = config or FeishuConfig()
        self.store = store or FeishuTokenStore()
        self._refresh_all_lock = threading.Lock()
        self._scheduler_started = False

    # ----- 授权链接 -----

    def build_auth_url(self) -> str:
        params = {
            "client_id": self.config.app_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": self.config.scope_string,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    # ----- 换 token / 刷新 -----

    def exchange_code(self, code: str) -> Dict[str, Any]:
        body = {
            "grant_type": "authorization_code",
            "client_id": self.config.app_id,
            "client_secret": self.config.app_secret,
            "code": code,
            "redirect_uri": self.config.redirect_uri,
        }
        return self._normalize_token(self._post_json(TOKEN_URL, body))

    def refresh_user_token(self, refresh_token: str) -> Dict[str, Any]:
        body = {
            "grant_type": "refresh_token",
            "client_id": self.config.app_id,
            "client_secret": self.config.app_secret,
            "refresh_token": refresh_token,
        }
        return self._normalize_token(self._post_json(TOKEN_URL, body))

    def get_user_info(self, access_token: str) -> Dict[str, Any]:
        resp = requests.get(
            USER_INFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=30
        )
        try:
            result = resp.json()
        except ValueError:
            raise RuntimeError(f"获取飞书用户信息失败：返回非 JSON，HTTP={resp.status_code}")
        if resp.status_code != 200 or result.get("code") != 0:
            raise RuntimeError(f"获取飞书用户信息失败：HTTP={resp.status_code}, result={result}")
        return result.get("data", {})

    def persist_user_session(self, token_payload: Dict[str, Any], user_info: Dict[str, Any]) -> str:
        user_key = self._get_user_key(user_info)
        record_tokens = {**token_payload, "saved_at": _iso_now()}
        self.store.upsert_user(user_key, user_info=user_info, tokens=record_tokens)
        return user_key

    # ----- 取可用 access_token（过期自动刷新）-----

    def ensure_valid_access_token(self, user_key: str) -> Dict[str, Any]:
        """返回 {user_key, user_info, tokens}；access_token 过期则刷新并回写。"""
        record = self.store.get_user(user_key)
        if not record:
            raise RuntimeError(f"本地未找到飞书用户 {user_key}")
        tokens = record.get("tokens") or {}
        if not _is_expired(tokens.get("access_token_expires_at")):
            return {"user_key": user_key, "user_info": record.get("user_info") or {}, "tokens": tokens}
        return self.refresh_access_token(user_key)

    def refresh_access_token(self, user_key: str) -> Dict[str, Any]:
        record = self.store.get_user(user_key)
        if not record:
            raise RuntimeError(f"本地未找到飞书用户 {user_key}")
        tokens = record.get("tokens") or {}
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("本地没有 refresh_token，请重新授权")
        if _is_expired(tokens.get("refresh_token_expires_at"), skew_seconds=300):
            raise RuntimeError("refresh_token 已过期，请重新授权")

        refreshed = self.refresh_user_token(refresh_token)
        # 飞书刷新可能不回部分字段，用旧值兜底
        refreshed["scope"] = refreshed.get("scope") or tokens.get("scope")
        refreshed["refresh_token"] = refreshed.get("refresh_token") or refresh_token
        refreshed["refresh_token_expires_at"] = (
            refreshed.get("refresh_token_expires_at") or tokens.get("refresh_token_expires_at")
        )
        refreshed["refresh_token_expires_in"] = (
            refreshed.get("refresh_token_expires_in") or tokens.get("refresh_token_expires_in")
        )
        refreshed["saved_at"] = _iso_now()
        self.store.update_tokens(user_key, refreshed)
        return {"user_key": user_key, "user_info": record.get("user_info") or {}, "tokens": refreshed}

    def refresh_all_access_tokens(self) -> Dict[str, Any]:
        """刷新所有本地用户 token，返回非敏感摘要。"""
        if not self._refresh_all_lock.acquire(blocking=False):
            return {"refreshed": 0, "failed": 0, "message": "token refresh already running"}
        try:
            users = self.store.list_users()
            refreshed, failed, results = 0, 0, []
            for user_key, record in users.items():
                name = (record.get("user_info") or {}).get("name")
                try:
                    sess = self.refresh_access_token(user_key)
                    refreshed += 1
                    results.append({"user_key": user_key, "name": name, "ok": True,
                                    "access_token_expires_at": sess["tokens"].get("access_token_expires_at")})
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    results.append({"user_key": user_key, "name": name, "ok": False, "error": str(exc)})
            return {"refreshed": refreshed, "failed": failed, "results": results}
        finally:
            self._refresh_all_lock.release()

    # ----- 定时刷新调度 -----

    def start_token_refresh_scheduler(self, *, interval: Optional[int] = None) -> None:
        if self._scheduler_started:
            return
        self._scheduler_started = True
        seconds = int(interval or os.getenv("FEISHU_TOKEN_REFRESH_SECONDS", "3600"))
        seconds = max(seconds, 60)

        def _loop():
            while True:
                threading.Event().wait(seconds)
                try:
                    self.refresh_all_access_tokens()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=_loop, name="feishu-token-refresh", daemon=True).start()

    # ----- 内部 -----

    def _post_json(self, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
        resp = requests.post(url, headers={"Content-Type": "application/json; charset=utf-8"},
                             json=body, timeout=30)
        try:
            result = resp.json()
        except ValueError:
            raise RuntimeError(f"飞书请求失败：返回非 JSON，HTTP={resp.status_code}, body={resp.text[:200]}")
        if resp.status_code != 200 or result.get("code") != 0:
            raise RuntimeError(f"飞书请求失败：HTTP={resp.status_code}, result={result}")
        return result

    @staticmethod
    def _normalize_token(result: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "access_token": result.get("access_token"),
            "expires_in": result.get("expires_in"),
            "access_token_expires_at": _expires_at(result.get("expires_in")),
            "refresh_token": result.get("refresh_token"),
            "refresh_token_expires_in": result.get("refresh_token_expires_in"),
            "refresh_token_expires_at": _expires_at(result.get("refresh_token_expires_in")),
            "token_type": result.get("token_type"),
            "scope": result.get("scope"),
        }

    @staticmethod
    def _get_user_key(user_info: Dict[str, Any]) -> str:
        for key in ("open_id", "user_id", "union_id", "sub"):
            value = user_info.get(key)
            if value:
                return str(value)
        raise RuntimeError(f"无法从飞书用户信息识别唯一标识: {user_info}")


# ============================ 进程级单例 ============================

_singleton: Optional[FeishuOAuthService] = None
_singleton_lock = threading.Lock()


def get_feishu_oauth_service() -> FeishuOAuthService:
    """全局共享的 OAuth 服务单例（scheduler 与 web 共用，避免起多个刷新线程）。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = FeishuOAuthService()
    return _singleton
