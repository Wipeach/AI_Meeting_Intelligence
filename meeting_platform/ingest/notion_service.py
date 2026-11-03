from __future__ import annotations

"""
Notion 服务层。

集中管理所有 Notion 相关能力：
- 创建子页面
- 追加文本 block
- 上传本地文件
- 把上传后的文件作为 file/pdf/image 等 block 插入页面
"""

import logging
import math
import mimetypes
import os
import re
from io import BytesIO
from pathlib import Path
from typing import Any

from notion_client import Client

from meeting_platform.core.common import Config, SyncError, call_with_retry


NOTION_SINGLE_PART_LIMIT = 20 * 1024 * 1024
NOTION_MULTI_PART_SIZE = 10 * 1024 * 1024


# ========== 文件类型 ==========


def guess_file_info(local_path: Path) -> dict[str, str]:
    """根据本地文件路径推断 Notion 上传所需的文件信息。"""
    filename = local_path.name
    suffix = local_path.suffix.lower()
    content_type = content_type_for_suffix(suffix, filename)

    if suffix == ".pdf":
        block_type = "pdf"
    elif suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}:
        block_type = "image"
    elif suffix in {".mp4", ".mov", ".webm", ".avi", ".mkv"}:
        block_type = "video"
    elif suffix in {".mp3", ".wav", ".m4a", ".flac"}:
        block_type = "audio"
    else:
        block_type = "file"

    return {
        "filename": filename,
        "suffix": suffix,
        "content_type": content_type,
        "block_type": block_type,
    }


def content_type_for_suffix(suffix: str, filename: str) -> str:
    """根据后缀名推断 Content-Type。"""
    content_type_map = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".bmp": "image/bmp",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".csv": "text/csv",
        ".json": "application/json",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".zip": "application/zip",
        ".rar": "application/vnd.rar",
        ".7z": "application/x-7z-compressed",
        ".tar": "application/x-tar",
        ".gz": "application/gzip",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
        ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".flac": "audio/x-flac",
    }
    content_type = content_type_map.get(suffix)
    if content_type:
        return content_type
    guessed_type, _ = mimetypes.guess_type(filename)
    return guessed_type or "application/octet-stream"


# ========== Block 结构 ==========


def text_rich(content: str) -> list[dict[str, Any]]:
    """构造 Notion rich_text 文本结构。"""
    return [{"type": "text", "text": {"content": content}}]


def paragraph_block(content: str) -> dict[str, Any]:
    """构造段落 block。"""
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": text_rich(content)},
    }


def file_upload_block(file_upload_id: str, file_info: dict[str, str]) -> dict[str, Any]:
    """构造引用 Notion File Upload 的文件类 block。"""
    block_type = file_info["block_type"]
    return {
        "object": "block",
        "type": block_type,
        block_type: {
            "type": "file_upload",
            "file_upload": {"id": file_upload_id},
        },
    }


def split_text_content(content: str, chunk_size: int = 1800) -> list[str]:
    """把长文本拆成多个 Notion 可接受的段落片段。"""
    text = content.strip()
    if not text:
        return []

    chunks: list[str] = []
    for paragraph in text.splitlines():
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        while len(paragraph) > chunk_size:
            chunks.append(paragraph[:chunk_size])
            paragraph = paragraph[chunk_size:]
        chunks.append(paragraph)
    return chunks


# ========== 页面 ==========


def create_notion_child_page(
    notion: Client,
    parent_page_id: str,
    title: str,
    config: Config,
) -> dict[str, Any]:
    """在指定 Notion 父页面下创建子页面。"""
    page_title = title.strip() or "Feishu sync"
    if len(page_title) > 200:
        page_title = page_title[:200].rstrip()

    logging.info("Creating Notion child page under %s", parent_page_id)
    return call_with_retry(
        "Create Notion child page",
        config.retry_max_attempts,
        config.retry_base_delay_sec,
        lambda: notion.pages.create(
            parent={"type": "page_id", "page_id": parent_page_id},
            properties={"title": {"title": text_rich(page_title)}},
        ),
    )


# ========== 文件上传 ==========


def upload_local_file_to_notion(
    notion: Client,
    local_path: Path,
    config: Config,
) -> dict[str, Any]:
    """按文件大小选择单片或分片模式上传到 Notion。"""
    file_size = local_path.stat().st_size
    if file_size <= NOTION_SINGLE_PART_LIMIT:
        return upload_single_part_file(notion, local_path, config)
    return upload_multi_part_file(notion, local_path, config)


def upload_single_part_file(
    notion: Client,
    local_path: Path,
    config: Config,
) -> dict[str, Any]:
    """上传 20MB 以内的单片文件。"""
    file_info = guess_file_info(local_path)
    logging.info("Uploading local file to Notion in single-part mode")
    created = call_with_retry(
        "Create Notion file upload",
        config.retry_max_attempts,
        config.retry_base_delay_sec,
        lambda: notion.file_uploads.create(
            mode="single_part",
            filename=file_info["filename"],
            content_type=file_info["content_type"],
        ),
    )

    with local_path.open("rb") as file_obj:
        def send_single_part() -> dict[str, Any]:
            file_obj.seek(0)
            return notion.file_uploads.send(
                created["id"],
                file=(file_info["filename"], file_obj, file_info["content_type"]),
            )

        uploaded = call_with_retry(
            "Send Notion file upload",
            config.retry_max_attempts,
            config.retry_base_delay_sec,
            send_single_part,
        )
    return ensure_notion_file_uploaded(uploaded)


def upload_multi_part_file(
    notion: Client,
    local_path: Path,
    config: Config,
) -> dict[str, Any]:
    """上传超过 20MB 的分片文件。"""
    file_info = guess_file_info(local_path)
    file_size = local_path.stat().st_size
    number_of_parts = math.ceil(file_size / NOTION_MULTI_PART_SIZE)
    logging.info(
        "Uploading local file to Notion in multi-part mode with %s parts",
        number_of_parts,
    )
    created = call_with_retry(
        "Create Notion multipart upload",
        config.retry_max_attempts,
        config.retry_base_delay_sec,
        lambda: notion.file_uploads.create(
            mode="multi_part",
            filename=file_info["filename"],
            content_type=file_info["content_type"],
            number_of_parts=number_of_parts,
        ),
    )

    with local_path.open("rb") as file_obj:
        part_number = 1
        while True:
            chunk = file_obj.read(NOTION_MULTI_PART_SIZE)
            if not chunk:
                break
            send_multi_part_chunk(
                notion,
                created["id"],
                file_info,
                chunk,
                part_number,
                config,
            )
            part_number += 1

    call_with_retry(
        "Complete Notion multipart upload",
        config.retry_max_attempts,
        config.retry_base_delay_sec,
        lambda: notion.file_uploads.complete(created["id"]),
    )
    retrieved = call_with_retry(
        "Retrieve Notion file upload",
        config.retry_max_attempts,
        config.retry_base_delay_sec,
        lambda: notion.file_uploads.retrieve(created["id"]),
    )
    return ensure_notion_file_uploaded(retrieved)


def send_multi_part_chunk(
    notion: Client,
    upload_id: str,
    file_info: dict[str, str],
    chunk: bytes,
    part_number: int,
    config: Config,
) -> None:
    """发送 Notion 分片上传中的单个分片。"""
    call_with_retry(
        f"Send Notion multipart upload part {part_number}",
        config.retry_max_attempts,
        config.retry_base_delay_sec,
        lambda: notion.file_uploads.send(
            upload_id,
            file=(
                file_info["filename"],
                BytesIO(chunk),
                file_info["content_type"],
            ),
            part_number=part_number,
        ),
    )


def ensure_notion_file_uploaded(file_upload: dict[str, Any]) -> dict[str, Any]:
    """确认 Notion 文件上传状态已经完成。"""
    status = file_upload.get("status")
    if status != "uploaded":
        raise SyncError(f"Notion file upload did not finish successfully: status={status}")
    return file_upload


# ========== 追加到页面 ==========


def append_text_blocks_to_page(
    notion: Client,
    page_id: str,
    text_content: str,
    config: Config,
) -> list[dict[str, Any]]:
    """把文本内容追加到指定 Notion 页面。"""
    chunks = split_text_content(text_content)
    if not chunks:
        return []

    results: list[dict[str, Any]] = []
    blocks = [paragraph_block(chunk) for chunk in chunks]
    for start in range(0, len(blocks), 100):
        batch = blocks[start:start + 100]
        logging.info("Appending %s text block(s) to page %s", len(batch), page_id)
        result = call_with_retry(
            "Append Notion text blocks",
            config.retry_max_attempts,
            config.retry_base_delay_sec,
            lambda batch=batch: notion.blocks.children.append(
                block_id=page_id,
                children=batch,
            ),
        )
        results.append(result)
    return results


def append_notion_uploaded_file_block(
    notion: Client,
    page_id: str,
    file_upload_id: str,
    local_path: Path,
    config: Config,
) -> dict[str, Any]:
    """把已上传到 Notion 的文件追加为页面正文 block。"""
    file_info = guess_file_info(local_path)
    block = file_upload_block(file_upload_id, file_info)
    logging.info("Appending Notion-hosted %s block to page %s", file_info["block_type"], page_id)
    return call_with_retry(
        "Append Notion file upload block",
        config.retry_max_attempts,
        config.retry_base_delay_sec,
        lambda: notion.blocks.children.append(block_id=page_id, children=[block]),
    )


# ========== Markdown → Notion 结构化 block ==========
# 把 AI 智能纪要(markdown)转成 Notion 的标题/列表/引用/段落 block，渲染美观。

RICH_TEXT_LIMIT = 2000  # Notion 单个 rich_text text.content 上限 2000
_INLINE_RE = re.compile(r"(\*\*[^*]+?\*\*|`[^`]+?`)")


def _split_content(content: str) -> list[str]:
    if not content:
        return [""]
    return [content[i:i + RICH_TEXT_LIMIT] for i in range(0, len(content), RICH_TEXT_LIMIT)]


def _inline_rich_text(text: str) -> list[dict[str, Any]]:
    """解析行内 **加粗** 与 `代码`，生成 Notion rich_text 数组（每段自动按 2000 字切分）。"""
    segs: list[tuple[str, bool, bool]] = []  # (content, bold, code)
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            segs.append((text[pos:m.start()], False, False))
        tok = m.group(0)
        if tok.startswith("**"):
            segs.append((tok[2:-2], True, False))
        else:
            segs.append((tok[1:-1], False, True))
        pos = m.end()
    if pos < len(text):
        segs.append((text[pos:], False, False))
    if not segs:
        segs = [("", False, False)]
    out: list[dict[str, Any]] = []
    for content, bold, code in segs:
        for chunk in _split_content(content):
            item: dict[str, Any] = {"type": "text", "text": {"content": chunk}}
            if bold or code:
                item["annotations"] = {"bold": bold, "code": code}
            out.append(item)
    return out


def heading_block(content: str, level: int) -> dict[str, Any]:
    t = {1: "heading_1", 2: "heading_2", 3: "heading_3"}.get(min(max(level, 1), 3), "heading_3")
    return {"object": "block", "type": t, t: {"rich_text": _inline_rich_text(content)}}


def bulleted_block(content: str) -> dict[str, Any]:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _inline_rich_text(content)}}


def numbered_block(content: str) -> dict[str, Any]:
    return {"object": "block", "type": "numbered_list_item",
            "numbered_list_item": {"rich_text": _inline_rich_text(content)}}


def quote_block(content: str) -> dict[str, Any]:
    return {"object": "block", "type": "quote", "quote": {"rich_text": _inline_rich_text(content)}}


def divider_block() -> dict[str, Any]:
    return {"object": "block", "type": "divider", "divider": {}}


def paragraph_rich_block(content: str) -> dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _inline_rich_text(content)}}


def markdown_to_blocks(md: str) -> list[dict[str, Any]]:
    """把 markdown 文本转成 Notion block 列表（标题/列表/引用/分隔/段落；表格行降级为段落）。"""
    if not md or not md.strip():
        return []
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        hm = re.match(r"^(#{1,6})\s+(.*)$", line)
        if hm:
            blocks.append(heading_block(hm.group(2), len(hm.group(1))))
            continue
        if line in ("---", "***", "___"):
            blocks.append(divider_block())
            continue
        if line.startswith("> "):
            blocks.append(quote_block(line[2:].strip()))
            continue
        if re.match(r"^[-*+]\s+", line):
            blocks.append(bulleted_block(re.sub(r"^[-*+]\s+", "", line)))
            continue
        nm = re.match(r"^\d+\.\s+(.*)$", line)
        if nm:
            blocks.append(numbered_block(nm.group(1)))
            continue
        blocks.append(paragraph_rich_block(line))
    return blocks


def append_markdown_blocks_to_page(
    notion: Client,
    page_id: str,
    md: str,
    config: Config,
) -> list[dict[str, Any]]:
    """把 markdown 追加为结构化 block（每批最多 100 个，Notion API 上限）。"""
    blocks = markdown_to_blocks(md)
    if not blocks:
        return []
    results: list[dict[str, Any]] = []
    for start in range(0, len(blocks), 100):
        batch = blocks[start:start + 100]
        logging.info("Appending %d markdown block(s) to page %s", len(batch), page_id)
        result = call_with_retry(
            "Append markdown blocks",
            config.retry_max_attempts,
            config.retry_base_delay_sec,
            lambda batch=batch: notion.blocks.children.append(block_id=page_id, children=batch),
        )
        results.append(result)
    return results


# ========== 客户端构建 ==========

def build_notion_client() -> Client:
    """读取 NOTION_TOKEN；若配置了 PROXY_URL（国内访问 Notion 必需）则走代理。"""
    token = os.getenv("NOTION_TOKEN", "").strip()
    if not token:
        raise SyncError("缺少 NOTION_TOKEN")
    proxy = os.getenv("PROXY_URL", "").strip()
    if proxy:
        try:
            import httpx
            return Client(auth=token, client=httpx.Client(proxy=proxy, timeout=180))
        except ImportError:
            logging.warning("httpx 未安装，忽略 PROXY_URL=%s", proxy)
    return Client(auth=token)
