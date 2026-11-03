"""会议纪要文本提取（TXT/DOCX/PDF → 纯文本，支持 GBK 编码与 pdfplumber 兜底）。"""

from __future__ import annotations

import io
import re
import zipfile


def extract_text(content: bytes, file_type: str = "") -> str:
    """根据文件类型提取纯文本；识别失败返回空串。"""
    file_type = (file_type or "").lower().strip(".")
    head = content[:5]

    if file_type == "pdf" or head.startswith(b"%PDF"):
        return _extract_pdf(content)
    if file_type in ("docx", "doc") or content[:2] == b"PK":
        return _extract_docx(content)
    return _extract_txt(content)


def _extract_txt(content: bytes) -> str:
    """纯文本：依次尝试 UTF-8 / GB18030 / GBK，避免中文乱码。"""
    for encoding in ("utf-8", "gb18030", "gbk"):
        try:
            return content.decode(encoding).strip()
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", errors="ignore").strip()


def _extract_docx(content: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return _normalize(xml)


def _extract_pdf(content: bytes) -> str:
    """pypdf 优先，效果差再用 pdfplumber 兜底。"""
    text = _pdf_with(content, "pypdf")
    if _looks_good(text):
        return _normalize(text)
    text2 = _pdf_with(content, "pdfplumber")
    if _looks_good(text2):
        return _normalize(text2)
    return _normalize(text or text2)


def _pdf_with(content: bytes, lib: str) -> str:
    try:
        if lib == "pypdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        if lib == "pdfplumber":
            import pdfplumber
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _looks_good(text: str) -> bool:
    return bool(text and len(re.findall(r"[一-鿿A-Za-z0-9]", text)) >= 10)


def _normalize(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return re.sub(r"[ \t]+\n", "\n", text).strip()
