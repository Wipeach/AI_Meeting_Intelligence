"""登录凭证源 + 校验。

凭证来自飞书多维表（与督办表同一个 base），字段 ``name`` + ``password``。
复用 ``FeishuBitableClient``（tenant_access_token，国内直连 open.feishu.cn）。

策略：
- 每次登录都实时拉飞书表，账密改动即时生效（无需等缓存过期）。
- 内存缓存仅在飞书不可达时作兜底，避免飞书抖动锁死所有人。
- ``hmac.compare_digest`` 常量时间比较（防计时侧信道）。
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
from typing import Dict, Optional

from meeting_platform.ingest.feishu_bitable import FeishuBitableClient, _field_text

log = logging.getLogger("auth")

DEFAULT_LOGIN_TABLE = "tblxZmqsJkFDHuWo"


class UserStore:
    """从飞书多维表读 {name: password}；每次校验实时拉表，缓存仅兜底。"""

    def __init__(self):
        self._client = None  # 惰性创建：首次校验时才读 env，避免 import 早于 .env 加载
        self._table = os.getenv("FEISHU_BITABLE_LOGIN_TABLE", DEFAULT_LOGIN_TABLE)
        self._users: Dict[str, str] = {}
        self._lock = threading.Lock()

    def _client_lazy(self) -> FeishuBitableClient:
        if self._client is None:
            self._client = FeishuBitableClient()
            self._table = os.getenv("FEISHU_BITABLE_LOGIN_TABLE", DEFAULT_LOGIN_TABLE)
        return self._client

    def _fetch(self) -> Dict[str, str]:
        """实时从飞书表读取 {name: password}。"""
        users: Dict[str, str] = {}
        for r in self._client_lazy().list_records(self._table):
            f = r.get("fields") or {}
            name = _field_text(f.get("name")).strip()
            pwd = _field_text(f.get("password"))
            if name:
                users[name] = pwd
        return users

    def verify(self, name: Optional[str], password: Optional[str]) -> bool:
        """校验用户名/密码：每次实时拉飞书表；飞书不可达才用上次缓存兜底。"""
        name = (name or "").strip()
        if not name or password is None:
            return False
        with self._lock:
            try:
                self._users = self._fetch()  # 实时拉，改动即时生效
            except Exception:  # noqa: BLE001
                log.warning("拉取登录用户表失败，使用上次缓存兜底")
            users = self._users
        expect = users.get(name)
        if expect is None:
            return False
        # 编码成 bytes 比较：compare_digest 对非 ASCII 字符串会抛 TypeError
        return hmac.compare_digest(expect.encode("utf-8"), password.encode("utf-8"))


# 进程级单例
_user_store = UserStore()
