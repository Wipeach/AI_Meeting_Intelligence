"""LLM 客户端抽象与实现。

``OpenAICompatibleClient`` 适配任何兼容 OpenAI Chat Completions 的服务
（GLM / Deepseek / 通义 / Moonshot / OpenAI），通过环境变量配置：

    LLM_API_KEY / LLM_BASE_URL / LLM_MODEL

``MockLLMClient`` 返回固定结构化结果，用于无 Key 时的本地演示与测试。
"""

from __future__ import annotations

import json
import os
import random
import time
from typing import Any, Dict

import requests


class LLMClient:
    """LLM 客户端抽象：输入 system/user 提示，返回解析后的 JSON dict。"""

    def chat_json(self, system: str, user: str) -> Dict[str, Any]:
        raise NotImplementedError


class OpenAICompatibleClient(LLMClient):
    """兼容 OpenAI Chat Completions 的通用客户端。"""

    def __init__(self, *, api_key: str = "", base_url: str = "", model: str = "", timeout: int = 180, retries: int = 5):
        self.api_key = api_key or os.getenv("LLM_API_KEY", "")
        self.base_url = (base_url or os.getenv("LLM_BASE_URL", "")).rstrip("/")
        self.model = model or os.getenv("LLM_MODEL", "")
        self.timeout = timeout
        self.retries = retries  # 429/5xx 指数退避重试次数（并发下防限流）

    def chat_json(self, system: str, user: str) -> Dict[str, Any]:
        if not (self.api_key and self.base_url and self.model):
            raise RuntimeError("LLM 配置不完整，请检查 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": int(os.getenv("LLM_MAX_TOKENS", "8192")),
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_error = ""
        for attempt in range(self.retries):
            try:
                resp = requests.post(f"{self.base_url}/chat/completions", headers=headers, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:  # 网络/连接错误：可重试
                last_error = str(exc)
                self._backoff(attempt)
                continue
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    # 200 但结构异常（空 choices / 字段缺失）：不重试，直接报错，附带原文便于排查
                    raise RuntimeError(f"LLM 返回结构异常：{resp.text[:300]}") from exc
                try:
                    return json.loads(content)
                except json.JSONDecodeError as exc:  # 模型输出非 JSON：不重试，直接报错
                    raise RuntimeError(f"LLM 返回非合法 JSON：{content[:300]}") from exc
            last_error = f"HTTP={resp.status_code}, body={resp.text[:200]}"
            if resp.status_code not in (429, 500, 502, 503, 504):  # 非临时错误：立即抛出
                raise RuntimeError(f"LLM 请求失败：{last_error}")
            self._backoff(attempt)  # 429/5xx：退避后重试
        raise RuntimeError(f"LLM 请求失败（重试 {self.retries} 次仍失败）：{last_error}")

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(2 ** attempt + random.uniform(0, 1), 20))


class MockLLMClient(LLMClient):
    """返回固定结果的 Mock，用于无 Key 演示与测试。"""

    def chat_json(self, system: str, user: str) -> Dict[str, Any]:
        return {
            "meeting_topic": "演示会议议题",
            "summary": "（演示用）本次会议讨论了核心议题并形成决议，明确了几项待办及责任人。",
            "topics": ["议题一：项目进度", "议题二：需求评审", "议题三：分工安排"],
            "decisions": ["决议一：按既定节奏推进", "决议二：需求本周内定稿"],
            "action_items": [
                {"content": "完成需求文档定稿", "owner": "王五", "due": "2026-08-08"},
                {"content": "拉齐前端排期", "owner": "赵六", "due": "2026-08-10"},
            ],
        }
