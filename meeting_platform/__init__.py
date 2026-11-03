"""会议管理平台核心包。

目录结构（按职责分层）：
    core/      统一数据模型 + 存储
    ingest/    采集 + 适配器 + OSS + 文本提取
    analysis/  LLM + AI 分析（长文本 map-reduce）
    ops/       定时调度 + 一键运行入口
    web/       仪表盘
    demo/      演示数据 + 示例

导入本包时自动加载项目根目录的 .env（若 python-dotenv 可用）。
"""

import os
from pathlib import Path


def _load_env_file() -> None:
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    _load_env_file()

# 再导出常用名，方便 `from meeting_platform import Meeting, MeetingStore`
from meeting_platform.core.models import (
    ActionItem,
    ActionItemStatus,
    AnalysisResult,
    Meeting,
    MeetingSource,
    MeetingStatus,
)
from meeting_platform.core.store import MeetingStore

__all__ = [
    "ActionItem",
    "ActionItemStatus",
    "AnalysisResult",
    "Meeting",
    "MeetingSource",
    "MeetingStatus",
    "MeetingStore",
]
