"""notion_service.py 依赖的通用工具：配置、异常、带退避的重试。

仅为打通 notion_service.py 的 import 而提供的最小实现。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass


class SyncError(Exception):
    """同步流程中需要中断的上层错误。"""


@dataclass
class Config:
    retry_max_attempts: int = 6
    retry_base_delay_sec: float = 3.0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def load_config() -> Config:
    return Config(
        retry_max_attempts=max(1, _env_int("NOTION_RETRY_MAX_ATTEMPTS", 6)),
        retry_base_delay_sec=max(0.1, _env_float("NOTION_RETRY_BASE_DELAY_SEC", 3.0)),
    )


def call_with_retry(action: str, max_attempts: int, base_delay_sec: float, func):
    """指数退避重试。Notion API 限流(429)/网络抖动时自动重试。"""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= max_attempts:
                raise
            delay = base_delay_sec * (2 ** (attempt - 1))
            time.sleep(delay)
    raise RuntimeError(f"{action} failed: {last_exc}")
