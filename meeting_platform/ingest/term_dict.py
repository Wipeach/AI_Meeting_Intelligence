"""专有名词纠错词表：从飞书多维表拉取，供 AI 分析前归一化转写文本。

背景：ASR 转写对地名/人名/品牌等专有名词经常写错（如 泗泾→四金、马陆→马路），
错误会一路污染 AI 纪要与督办待办。词表维护在飞书多维表里（业务同事可直接编辑），
本模块负责：拉取 → 分类 → 预处理 → 提供两类消费接口。

规则分类：
- 确定类：备注不含标记词 → 分析前直接字符串替换（最长匹配优先）
- 语境类：备注含「可能/优先/语境/判断」，或在代码内置风险词列表中
  （错误写法本身是常用词，盲目替换会误伤，如「四金」=四险一金）
  → 不替换，注入 analyzer 的 SYSTEM_PROMPT 交给 LLM 结合上下文判断

环境变量（复用督办表同一个 base，缺省即可用）：
    FEISHU_BITABLE_APP_TOKEN   词表所在 base（默认与督办表相同）
    TERM_DICT_TABLE_ID         词表 table_id，默认 tbldJftor4Dr1ain
字段约定：错误写法1 / 标准写法 / 类别 / 备注

缓存：内存 TTL（默认 6 小时）+ 文件兜底 data/term_dict_cache.json——
飞书不可达时用上次成功拉取的副本，保证分析链路不因词表故障中断。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from meeting_platform.ingest.feishu_bitable import DEFAULT_APP_TOKEN, FeishuBitableClient, _field_text

log = logging.getLogger("term_dict")

DEFAULT_TERM_TABLE = "tbldJftor4Dr1ain"
CACHE_FILE = Path("data/term_dict_cache.json")
CACHE_TTL = int(os.getenv("TERM_DICT_TTL", str(6 * 3600)))  # 内存缓存有效期（秒）

# 备注里出现任一关键词 → 语境类
CONTEXT_MARKERS = ("可能", "优先", "语境", "判断")

# 内置风险词：错误写法本身是常用词，盲目替换易误伤，强制走语境类
RISK_WORDS = {"四金"}


@dataclass
class TermRule:
    """一条纠错规则。"""

    wrong: str      # 错误写法
    standard: str   # 标准写法
    category: str = ""
    note: str = ""


class TermDict:
    """加载后的词表：确定类替换 + 语境类 prompt 生成。"""

    def __init__(self, rules: List[TermRule]):
        # 同一错误写法可能跨类别重复（如 虹金耀），去重保留第一条
        seen = set()
        unique: List[TermRule] = []
        for r in rules:
            if r.wrong and r.standard and r.wrong != r.standard and r.wrong not in seen:
                seen.add(r.wrong)
                unique.append(r)

        self._contextual = [r for r in unique if self._is_contextual(r)]
        self._deterministic = [r for r in unique if r not in self._contextual]
        # 最长匹配优先：先替换长词（新新马路），避免短词（马路）把长词拆坏
        self._deterministic.sort(key=lambda r: len(r.wrong), reverse=True)
        self._all = unique  # 全量规则（含语境类），供输出残留清除用

    @staticmethod
    def _is_contextual(rule: TermRule) -> bool:
        if rule.wrong in RISK_WORDS:
            return True
        return any(marker in rule.note for marker in CONTEXT_MARKERS)

    # ----- 消费接口 -----

    def correct(self, text: str) -> Tuple[str, int]:
        """确定类替换：返回 (修正后文本, 替换处数)。"""
        if not text:
            return text, 0
        total = 0
        for rule in self._deterministic:
            count = text.count(rule.wrong)
            if count:
                text = text.replace(rule.wrong, rule.standard)
                total += count
        return text, total

    def correct_title(self, title: str) -> Tuple[str, int]:
        """标题修正：与正文同一套规则（标题常带项目名）。"""
        return self.correct(title)

    def prompt_section(self) -> str:
        """语境类规则 → 追加到 analyzer SYSTEM_PROMPT 末尾的文本段。空规则返回空串。"""
        if not self._contextual:
            return ""
        lines = [
            "",
            "术语纠错规则（以下是语音转写中的错误写法/称呼别名，请结合上下文判断真实指代）：",
            "输出时【直接使用标准写法】把错误写法完全替换掉——禁止保留原词，",
            "禁止用括号标注原词（应写「泗泾」，不要写「泗泾(四金)」）；",
            "摘要、议题、决议、待办及其责任人里出现的所有人名/地名/项目名同理：",
        ]
        for r in self._contextual:
            note = f"（{r.note}）" if r.note else ""
            lines.append(f"- 「{r.wrong}」→ 直接写「{r.standard}」{note}")
        return "\n".join(lines)

    def strip_annotations(self, text: str) -> str:
        """清除 LLM 输出中「标准词(错误词)」式残留标注（全/半角括号），只保留标准词。

        prompt 已要求模型直接替换不保留原词，此为代码级兜底——模型偶尔不听话时
        保证最终落库内容干净。
        """
        if not text:
            return text
        for r in self._all:
            for fmt in (f"{r.standard}({r.wrong})", f"{r.standard}（{r.wrong}）"):
                if fmt in text:
                    text = text.replace(fmt, r.standard)
        return text

    def stats(self) -> dict:
        return {"deterministic": len(self._deterministic), "contextual": len(self._contextual)}


# ----- 拉取与缓存（进程级单例） -----

_lock = threading.Lock()
_cached: Optional[TermDict] = None
_cached_at = 0.0


def _fetch_rules() -> List[TermRule]:
    client = FeishuBitableClient()
    table_id = os.getenv("TERM_DICT_TABLE_ID", DEFAULT_TERM_TABLE)
    if not client.app_token and not os.getenv("FEISHU_BITABLE_APP_TOKEN"):
        client.app_token = DEFAULT_APP_TOKEN
    records = client.list_records(table_id)
    rules = []
    for rec in records:
        fields = rec.get("fields", {}) or {}
        wrong = _field_text(fields.get("错误写法1")).strip()
        standard = _field_text(fields.get("标准写法")).strip()
        if not wrong or not standard:
            continue
        rules.append(
            TermRule(
                wrong=wrong,
                standard=standard,
                category=_field_text(fields.get("类别")).strip(),
                note=_field_text(fields.get("备注")).strip(),
            )
        )
    return rules


def _save_cache(rules: List[TermRule]) -> None:
    """成功拉取后落盘一份兜底副本（data/ 已在 .gitignore）。"""
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = [{"wrong": r.wrong, "standard": r.standard, "category": r.category, "note": r.note} for r in rules]
        CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        log.warning("词表兜底缓存写盘失败（不影响本次使用）", exc_info=True)


def _load_cache() -> Optional[TermDict]:
    try:
        if not CACHE_FILE.exists():
            return None
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return TermDict([TermRule(**item) for item in payload])
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def refresh_term_dict() -> TermDict:
    """立即拉取最新词表（成功会刷新内存与文件缓存）。失败时回落到旧缓存。"""
    global _cached, _cached_at
    rules = _fetch_rules()
    term_dict = TermDict(rules)
    with _lock:
        _cached = term_dict
        _cached_at = time.time()
    _save_cache(rules)
    log.info("纠错词表已加载：%s", term_dict.stats())
    return term_dict


def get_term_dict() -> Optional[TermDict]:
    """获取词表（TTL 内用内存缓存，过期重拉；飞书不可达回落文件缓存；全失败返回 None）。"""
    global _cached, _cached_at
    with _lock:
        if _cached and time.time() - _cached_at < CACHE_TTL:
            return _cached
    try:
        return refresh_term_dict()
    except Exception:  # noqa: BLE001
        log.warning("拉取纠错词表失败，尝试兜底缓存", exc_info=True)
        fallback = _load_cache()
        if fallback:
            with _lock:
                _cached, _cached_at = fallback, time.time()  # 避免每次调用都重试拉取
            log.info("使用词表兜底缓存：%s", fallback.stats())
            return fallback
        return None
