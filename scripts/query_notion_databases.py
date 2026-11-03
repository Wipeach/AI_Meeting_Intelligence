from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from notion_client import Client


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RETRY_MAX_ATTEMPTS = 6
RETRY_BASE_DELAY_SEC = 3.0
PAGE_DELAY_SEC = 0.4
@dataclass
class DatabaseInfo:
    id: str
    data_source_id: str
    title: str


def main() -> None:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    notion_token = args.token or os.getenv("NOTION_TOKEN", "").strip()
    if not notion_token:
        raise ValueError("缺少 Notion token，请在 .env 写入 NOTION_TOKEN，或通过 --token 传入。")

    page_reference = args.notion or input("请输入 Notion 页面链接或页面 ID: ").strip()
    page_id = resolve_notion_id(page_reference)
    if not page_id:
        raise ValueError("Notion 页面链接不能为空。")

    notion = build_notion_client(notion_token)
    try:
        databases = find_databases_under_page(notion, page_id)
        rows = []
        for database in databases:
            for page in query_database_pages(notion, database):
                rows.append(
                    {
                        "database_id": database.id,
                        "database_title": database.title,
                        "page_id": page["id"],
                        "title": get_page_title(page),
                        "summary": get_page_property_text(page, "Summary"),
                        "content": get_page_plain_text(notion, page["id"]),
                    }
                )
    finally:
        notion.close()

    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"输出文件: {output_path}")
        return

    print(json.dumps(rows, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="查询 Notion 页面下所有数据库的 Summary 和正文内容。")
    parser.add_argument("--notion", help="Notion 页面链接或页面 ID。")
    parser.add_argument("--token", help="Notion integration token；默认读取 .env 的 NOTION_TOKEN。")
    parser.add_argument("--output", help="可选：输出 JSON 文件路径。不传时直接打印 List<map> JSON。")
    return parser.parse_args()


def build_notion_client(token: str) -> Client:
    proxy_url = os.getenv("PROXY_URL", "").strip()
    if proxy_url:
        return Client(auth=token, client=httpx.Client(proxy=proxy_url, timeout=180))
    return Client(auth=token)


def resolve_notion_id(reference: str) -> str:
    raw = (reference or "").strip()
    if not raw:
        return ""
    uuid_candidates = re.findall(
        r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?"
        r"[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}",
        raw,
    )
    if uuid_candidates:
        return uuid_candidates[-1].replace("-", "")
    compact_candidates = re.findall(r"[0-9a-fA-F]{32}", raw)
    if compact_candidates:
        return compact_candidates[-1]
    return raw


def find_databases_under_page(notion: Client, page_id: str) -> list[DatabaseInfo]:
    databases: list[DatabaseInfo] = []
    visited_blocks: set[str] = set()
    walk_blocks_for_databases(notion, page_id, databases, visited_blocks)
    return databases


def walk_blocks_for_databases(
    notion: Client,
    block_id: str,
    databases: list[DatabaseInfo],
    visited_blocks: set[str],
) -> None:
    if block_id in visited_blocks:
        return
    visited_blocks.add(block_id)

    for block in list_block_children(notion, block_id):
        block_type = block.get("type")
        if block_type in {"child_database", "database"}:
            database_id = block["id"]
            databases.append(
                DatabaseInfo(
                    id=database_id,
                    data_source_id=resolve_database_data_source_id(notion, database_id),
                    title=get_block_plain_text(block),
                )
            )
            continue

        if block_type == "child_page":
            walk_blocks_for_databases(notion, block["id"], databases, visited_blocks)
            continue

        if block.get("has_children"):
            walk_blocks_for_databases(notion, block["id"], databases, visited_blocks)


def list_block_children(notion: Client, block_id: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    cursor = None
    while True:
        response = notion_call_with_retry(
            "list block children",
            lambda: notion.blocks.children.list(block_id=block_id, start_cursor=cursor, page_size=100),
        )
        results.extend(response.get("results", []))
        if not response.get("has_more"):
            return results
        cursor = response.get("next_cursor")
        time.sleep(PAGE_DELAY_SEC)


def resolve_database_data_source_id(notion: Client, database_id: str) -> str:
    try:
        database = notion_call_with_retry(
            "retrieve database",
            lambda: notion.databases.retrieve(database_id=database_id),
        )
    except Exception:
        return database_id

    data_sources = database.get("data_sources")
    if isinstance(data_sources, list) and data_sources:
        first_data_source = data_sources[0]
        if isinstance(first_data_source, dict) and first_data_source.get("id"):
            return str(first_data_source["id"])
    return database_id


def query_database_pages(notion: Client, database: DatabaseInfo) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    cursor = None
    while True:
        if hasattr(notion, "data_sources"):
            response = notion_call_with_retry(
                "query data source",
                lambda: notion.data_sources.query(
                    data_source_id=database.data_source_id,
                    start_cursor=cursor,
                    page_size=100,
                ),
            )
        else:
            response = notion_call_with_retry(
                "query database",
                lambda: notion.databases.query(
                    database_id=database.id,
                    start_cursor=cursor,
                    page_size=100,
                ),
            )
        results.extend(response.get("results", []))
        if not response.get("has_more"):
            return results
        cursor = response.get("next_cursor")
        time.sleep(PAGE_DELAY_SEC)


def get_page_plain_text(notion: Client, page_id: str) -> str:
    lines: list[str] = []
    collect_block_text(notion, page_id, lines, visited=set())
    return "\n".join(line for line in lines if line.strip())


def collect_block_text(notion: Client, block_id: str, lines: list[str], visited: set[str]) -> None:
    if block_id in visited:
        return
    visited.add(block_id)

    for block in list_block_children(notion, block_id):
        text = get_block_plain_text(block)
        if text:
            lines.append(text)
        if block.get("has_children"):
            collect_block_text(notion, block["id"], lines, visited)


def get_page_title(page: dict[str, Any]) -> str:
    properties = page.get("properties") or {}
    for property_value in properties.values():
        if isinstance(property_value, dict) and property_value.get("type") == "title":
            return rich_text_plain(property_value.get("title") or [])
    return ""


def get_page_property_text(page: dict[str, Any], property_name: str) -> str:
    property_value = (page.get("properties") or {}).get(property_name)
    if not isinstance(property_value, dict):
        return ""

    property_type = property_value.get("type")
    if property_type == "rich_text":
        return rich_text_plain(property_value.get("rich_text") or [])
    if property_type == "title":
        return rich_text_plain(property_value.get("title") or [])
    if property_type == "select":
        selected = property_value.get("select") or {}
        return selected.get("name", "")
    if property_type == "multi_select":
        return ", ".join(item.get("name", "") for item in property_value.get("multi_select") or [])
    if property_type == "number":
        value = property_value.get("number")
        return "" if value is None else str(value)
    if property_type == "checkbox":
        return str(property_value.get("checkbox", ""))
    if property_type in {"url", "email", "phone_number"}:
        return property_value.get(property_type) or ""
    if property_type == "formula":
        return formula_plain(property_value.get("formula") or {})
    return ""


def formula_plain(formula: dict[str, Any]) -> str:
    formula_type = formula.get("type")
    value = formula.get(formula_type)
    return "" if value is None else str(value)


def get_block_plain_text(block: dict[str, Any]) -> str:
    block_type = block.get("type")
    if not block_type:
        return ""
    data = block.get(block_type) or {}
    if block_type == "child_database":
        return data.get("title", "")
    if block_type == "child_page":
        return data.get("title", "")
    rich_text = data.get("rich_text")
    if isinstance(rich_text, list):
        return rich_text_plain(rich_text)
    caption = data.get("caption")
    if isinstance(caption, list):
        return rich_text_plain(caption)
    return ""


def rich_text_plain(items: list[dict[str, Any]]) -> str:
    return "".join(item.get("plain_text", "") for item in items if isinstance(item, dict)).strip()


def notion_call_with_retry(action: str, func):
    last_exc: Exception | None = None
    for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
        try:
            return func()
        except Exception as exc:
            last_exc = exc
            if attempt >= RETRY_MAX_ATTEMPTS:
                raise
            delay = RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1))
            print(f"{action} 失败，{delay:.1f} 秒后重试：{exc}")
            time.sleep(delay)
    raise RuntimeError(f"{action} failed: {last_exc}")


if __name__ == "__main__":
    main()
