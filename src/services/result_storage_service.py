"""
结果数据的 SQLite 读写服务。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.storage_names import build_result_filename
from src.services.price_history_service import parse_price_value
from src.services.result_blacklist_service import (
    match_blacklist_keywords,
    normalize_blacklist_keywords,
)


SORT_COLUMN_MAP = {
    "crawl_time": "crawl_time",
    "publish_time": "COALESCE(publish_time, '')",
    "price": "COALESCE(price, 0)",
    "keyword_hit_count": "keyword_hit_count",
    "replication_score": "replication_score",
}


def _get_link_unique_key(link: str) -> str:
    return link.split("&", 1)[0]


def _fallback_unique_key(record: dict, item: dict) -> str:
    item_id = str(item.get("商品ID") or "").strip()
    if item_id:
        return f"item:{item_id}"
    digest = hashlib.sha1(
        json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"hash:{digest}"


def _parse_raw_record(raw_json: str, *, status: str | None = None) -> dict:
    record = json.loads(raw_json)
    if status is not None:
        record["_status"] = status
    return record


def _build_query_conditions(
    *,
    filename: str,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    publish_within_days: int | None = None,
) -> tuple[str, list]:
    conditions = ["result_filename = ?"]
    params: list = [filename]
    if ai_recommended_only:
        conditions.append("is_recommended = 1")
        conditions.append("analysis_source = ?")
        params.append("ai")
    if keyword_recommended_only:
        conditions.append("is_recommended = 1")
        conditions.append("analysis_source = ?")
        params.append("keyword")
    if publish_within_days is not None and publish_within_days > 0:
        conditions.append("publish_time IS NOT NULL AND publish_time != ''")
        conditions.append("publish_time >= ?")
        params.append(_days_ago_iso(publish_within_days))
    return " AND ".join(conditions), params


def _days_ago_iso(days: int) -> str:
    """Return an ISO date string for *days* ago (YYYY-MM-DD)."""
    from datetime import timedelta

    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _sort_expression(sort_by: str, sort_order: str) -> str:
    column = SORT_COLUMN_MAP.get(sort_by, SORT_COLUMN_MAP["crawl_time"])
    direction = "ASC" if sort_order == "asc" else "DESC"
    return f"(CASE WHEN status = 'active' THEN 0 ELSE 1 END), {column} {direction}, id {direction}"


def _load_blacklist_keywords_from_conn(conn, filename: str) -> list[str]:
    row = conn.execute(
        """
        SELECT blacklist_keywords_json
        FROM result_blacklist_rules
        WHERE result_filename = ?
        """,
        (filename,),
    ).fetchone()
    if row is None:
        return []
    try:
        payload = json.loads(row["blacklist_keywords_json"] or "[]")
    except json.JSONDecodeError:
        return []
    return normalize_blacklist_keywords(payload)


def _decorate_record_visibility(record: dict, status: str | None, blacklist_keywords: list[str]) -> dict:
    matched_keywords = match_blacklist_keywords(record, blacklist_keywords)
    hidden_reason = None
    if status == "expired":
        hidden_reason = "expired"
    elif status and status != "active":
        hidden_reason = "manual"
    elif matched_keywords:
        hidden_reason = "rule"

    record["_status"] = status or "active"
    record["_matched_blacklist_keywords"] = matched_keywords
    record["_hidden_reason"] = hidden_reason
    record["_effective_hidden"] = hidden_reason is not None
    return record


def _is_record_visible(record: dict) -> bool:
    return record.get("_effective_hidden") is not True


def _parse_int_field(value: str | int | None) -> int | None:
    """Try to parse a numeric field that may be a string with Chinese suffixes."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    # Handle "万" suffix: e.g. "1.2万" → 12000
    if "万" in s:
        try:
            return int(float(s.replace("万", "")) * 10000)
        except (ValueError, TypeError):
            return None
    # Strip non-digit characters
    cleaned = "".join(ch for ch in s if ch.isdigit())
    if not cleaned:
        return None
    return int(cleaned)


def _passes_count_filters(
    product: dict,
    min_view_count: int | None,
    min_want_count: int | None,
) -> bool:
    """Check if a product passes min view/want count filters."""
    if min_view_count is not None:
        views = _parse_int_field(product.get("浏览量"))
        if views is None or views < min_view_count:
            return False
    if min_want_count is not None:
        wants = _parse_int_field(product.get("“想要”人数"))
        if wants is None or wants < min_want_count:
            return False
    return True


def _passes_replication_filters(
    ai_analysis: dict | None,
    is_replicable_only: bool,
    min_replication_score: int | None,
) -> bool:
    """Check if a record passes replication assessment filters."""
    if not is_replicable_only and min_replication_score is None:
        return True
    analysis = ai_analysis or {}
    replication = analysis.get("replication_assessment") or {}
    if is_replicable_only and not replication.get("is_replicable"):
        return False
    if min_replication_score is not None:
        score = replication.get("replication_score")
        if score is None or int(score) < min_replication_score:
            return False
    return True


def _load_filtered_records_from_conn(
    conn,
    *,
    filename: str,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    include_hidden: bool,
    publish_within_days: int | None = None,
    min_view_count: int | None = None,
    min_want_count: int | None = None,
    is_replicable_only: bool = False,
    min_replication_score: int | None = None,
) -> list[dict]:
    where_clause, params = _build_query_conditions(
        filename=filename,
        ai_recommended_only=ai_recommended_only,
        keyword_recommended_only=keyword_recommended_only,
        publish_within_days=publish_within_days,
    )
    order_clause = _sort_expression(sort_by, sort_order)
    # replication_score is inside raw_json — must sort in Python
    use_python_sort = sort_by == "replication_score"
    if use_python_sort:
        order_clause = _sort_expression("crawl_time", sort_order)
    rows = conn.execute(
        f"""
        SELECT raw_json, status
        FROM result_items
        WHERE {where_clause}
        ORDER BY {order_clause}
        """,
        tuple(params),
    ).fetchall()
    blacklist_keywords = _load_blacklist_keywords_from_conn(conn, filename)

    records: list[dict] = []
    for row in rows:
        record = _parse_raw_record(str(row["raw_json"]), status=row["status"])
        decorated = _decorate_record_visibility(record, row["status"], blacklist_keywords)
        if include_hidden or _is_record_visible(decorated):
            # Post-processing filters on raw_json fields
            product = decorated.get("商品信息", {}) or {}
            if not _passes_count_filters(product, min_view_count, min_want_count):
                continue
            # Post-processing filters on replication assessment
            if not _passes_replication_filters(
                decorated.get("ai_analysis"), is_replicable_only, min_replication_score
            ):
                continue
            records.append(decorated)
    if use_python_sort:
        reverse = sort_order == "desc"
        records.sort(
            key=lambda r: (
                r.get("_effective_hidden", False),
                -(
                    int((r.get("ai_analysis") or {}).get("replication_assessment", {}).get("replication_score", 0))
                ),
            ),
            reverse=reverse,
        )
    return records


async def save_result_record(record: dict, keyword: str) -> bool:
    return await asyncio.to_thread(_save_result_record_sync, record, keyword)


def _save_result_record_sync(record: dict, keyword: str) -> bool:
    bootstrap_sqlite_storage()
    item = record.get("商品信息", {}) or {}
    analysis = record.get("ai_analysis", {}) or {}
    link = str(item.get("商品链接") or "")
    link_unique_key = _get_link_unique_key(link) if link else _fallback_unique_key(record, item)
    keyword_hit_count = analysis.get("keyword_hit_count", 0)
    try:
        keyword_hit_count = int(keyword_hit_count)
    except (TypeError, ValueError):
        keyword_hit_count = 0

    with sqlite_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO result_items (
                result_filename, keyword, task_name, crawl_time, publish_time, price,
                price_display, item_id, title, link, link_unique_key, seller_nickname,
                is_recommended, analysis_source, keyword_hit_count, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                build_result_filename(keyword),
                record.get("搜索关键字", keyword),
                record.get("任务名称", ""),
                record.get("爬取时间", ""),
                item.get("发布时间"),
                parse_price_value(item.get("当前售价")),
                item.get("当前售价"),
                item.get("商品ID"),
                item.get("商品标题"),
                link,
                link_unique_key,
                (record.get("卖家信息", {}) or {}).get("卖家昵称") or item.get("卖家昵称"),
                1 if analysis.get("is_recommended") else 0,
                analysis.get("analysis_source"),
                keyword_hit_count,
                json.dumps(record, ensure_ascii=False),
            ),
        )
        conn.commit()
    return True


def load_processed_link_keys(keyword: str) -> set[str]:
    bootstrap_sqlite_storage()
    filename = build_result_filename(keyword)
    with sqlite_connection() as conn:
        rows = conn.execute(
            "SELECT link_unique_key FROM result_items WHERE result_filename = ?",
            (filename,),
        ).fetchall()
    return {str(row["link_unique_key"]) for row in rows if row["link_unique_key"]}


async def list_result_filenames() -> list[str]:
    return await asyncio.to_thread(_list_result_filenames_sync)


def _list_result_filenames_sync() -> list[str]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        rows = conn.execute(
            """
            SELECT result_filename, MAX(crawl_time) AS latest_crawl_time
            FROM result_items
            GROUP BY result_filename
            ORDER BY latest_crawl_time DESC, result_filename DESC
            """
        ).fetchall()
    return [str(row["result_filename"]) for row in rows]


async def result_file_exists(filename: str) -> bool:
    return await asyncio.to_thread(_result_file_exists_sync, filename)


def _result_file_exists_sync(filename: str) -> bool:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM result_items WHERE result_filename = ? LIMIT 1",
            (filename,),
        ).fetchone()
    return row is not None


async def delete_result_file_records(filename: str) -> int:
    return await asyncio.to_thread(_delete_result_file_records_sync, filename)


def _delete_result_file_records_sync(filename: str) -> int:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM result_items WHERE result_filename = ?",
            (filename,),
        )
        conn.commit()
    return int(cursor.rowcount or 0)


async def query_result_records(
    filename: str,
    *,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    page: int,
    limit: int,
    include_hidden: bool = False,
    publish_within_days: int | None = None,
    min_view_count: int | None = None,
    min_want_count: int | None = None,
    is_replicable_only: bool = False,
    min_replication_score: int | None = None,
) -> tuple[int, list[dict]]:
    return await asyncio.to_thread(
        _query_result_records_sync,
        filename,
        ai_recommended_only,
        keyword_recommended_only,
        sort_by,
        sort_order,
        page,
        limit,
        include_hidden,
        publish_within_days,
        min_view_count,
        min_want_count,
        is_replicable_only,
        min_replication_score,
    )


def _query_result_records_sync(
    filename: str,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    page: int,
    limit: int,
    include_hidden: bool,
    publish_within_days: int | None = None,
    min_view_count: int | None = None,
    min_want_count: int | None = None,
    is_replicable_only: bool = False,
    min_replication_score: int | None = None,
) -> tuple[int, list[dict]]:
    bootstrap_sqlite_storage()
    offset = max(page - 1, 0) * limit
    with sqlite_connection() as conn:
        records = _load_filtered_records_from_conn(
            conn,
            filename=filename,
            ai_recommended_only=ai_recommended_only,
            keyword_recommended_only=keyword_recommended_only,
            sort_by=sort_by,
            sort_order=sort_order,
            include_hidden=include_hidden,
            publish_within_days=publish_within_days,
            min_view_count=min_view_count,
            min_want_count=min_want_count,
            is_replicable_only=is_replicable_only,
            min_replication_score=min_replication_score,
        )
    total = len(records)
    return total, records[offset: offset + limit]


async def load_all_result_records(
    filename: str,
    *,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    include_hidden: bool = False,
    publish_within_days: int | None = None,
    min_view_count: int | None = None,
    min_want_count: int | None = None,
    is_replicable_only: bool = False,
    min_replication_score: int | None = None,
) -> list[dict]:
    return await asyncio.to_thread(
        _load_all_result_records_sync,
        filename,
        ai_recommended_only,
        keyword_recommended_only,
        sort_by,
        sort_order,
        include_hidden,
        publish_within_days,
        min_view_count,
        min_want_count,
        is_replicable_only,
        min_replication_score,
    )


def _load_all_result_records_sync(
    filename: str,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    include_hidden: bool,
    publish_within_days: int | None = None,
    min_view_count: int | None = None,
    min_want_count: int | None = None,
    is_replicable_only: bool = False,
    min_replication_score: int | None = None,
) -> list[dict]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        return _load_filtered_records_from_conn(
            conn,
            filename=filename,
            ai_recommended_only=ai_recommended_only,
            keyword_recommended_only=keyword_recommended_only,
            sort_by=sort_by,
            sort_order=sort_order,
            include_hidden=include_hidden,
            publish_within_days=publish_within_days,
            min_view_count=min_view_count,
            min_want_count=min_want_count,
            is_replicable_only=is_replicable_only,
            min_replication_score=min_replication_score,
        )


async def build_result_ndjson(filename: str) -> str:
    return await asyncio.to_thread(_build_result_ndjson_sync, filename)


def _build_result_ndjson_sync(filename: str) -> str:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        rows = conn.execute(
            "SELECT raw_json FROM result_items WHERE result_filename = ? ORDER BY id ASC",
            (filename,),
        ).fetchall()
    return "\n".join(str(row["raw_json"]) for row in rows)


async def load_result_summary(filename: str) -> dict | None:
    return await asyncio.to_thread(_load_result_summary_sync, filename)


def _load_result_summary_sync(filename: str) -> dict | None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        visible_records = _load_filtered_records_from_conn(
            conn,
            filename=filename,
            ai_recommended_only=False,
            keyword_recommended_only=False,
            sort_by="crawl_time",
            sort_order="desc",
            include_hidden=False,
        )
    if not visible_records:
        return None

    recommended_records = [
        record
        for record in visible_records
        if (record.get("ai_analysis", {}) or {}).get("is_recommended") is True
    ]
    ai_recommended_items = 0
    keyword_recommended_items = 0
    for record in recommended_records:
        source = (record.get("ai_analysis", {}) or {}).get("analysis_source")
        if source == "ai":
            ai_recommended_items += 1
        elif source == "keyword":
            keyword_recommended_items += 1

    return {
        "total_items": len(visible_records),
        "recommended_items": len(recommended_records),
        "ai_recommended_items": ai_recommended_items,
        "keyword_recommended_items": keyword_recommended_items,
        "latest_crawl_time": visible_records[0].get("爬取时间"),
        "latest_record": visible_records[0],
        "latest_recommendation": recommended_records[0] if recommended_records else None,
    }


async def update_item_status(filename: str, item_id: str, status: str) -> bool:
    valid = {"active", "hidden", "expired"}
    if status not in valid:
        raise ValueError(f"status must be one of {valid}")
    return await asyncio.to_thread(_update_item_status_sync, filename, item_id, status)


def _update_item_status_sync(filename: str, item_id: str, status: str) -> bool:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        cursor = conn.execute(
            "UPDATE result_items SET status = ? WHERE result_filename = ? AND item_id = ?",
            (status, filename, item_id),
        )
        conn.commit()
        return cursor.rowcount > 0


async def load_result_blacklist_keywords(filename: str) -> list[str]:
    return await asyncio.to_thread(_load_result_blacklist_keywords_sync, filename)


def _load_result_blacklist_keywords_sync(filename: str) -> list[str]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        return _load_blacklist_keywords_from_conn(conn, filename)


async def save_result_blacklist_keywords(filename: str, keywords: list[str]) -> list[str]:
    return await asyncio.to_thread(_save_result_blacklist_keywords_sync, filename, keywords)


def _save_result_blacklist_keywords_sync(filename: str, keywords: list[str]) -> list[str]:
    bootstrap_sqlite_storage()
    normalized_keywords = normalize_blacklist_keywords(keywords)
    now = datetime.now().isoformat()
    with sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO result_blacklist_rules (
                result_filename, blacklist_keywords_json, updated_at
            ) VALUES (?, ?, ?)
            ON CONFLICT(result_filename) DO UPDATE SET
                blacklist_keywords_json = excluded.blacklist_keywords_json,
                updated_at = excluded.updated_at
            """,
            (filename, json.dumps(normalized_keywords, ensure_ascii=False), now),
        )
        conn.commit()
    return normalized_keywords


def load_visible_result_item_ids(filename: str) -> set[str]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        visible_records = _load_filtered_records_from_conn(
            conn,
            filename=filename,
            ai_recommended_only=False,
            keyword_recommended_only=False,
            sort_by="crawl_time",
            sort_order="desc",
            include_hidden=False,
        )
    item_ids: set[str] = set()
    for record in visible_records:
        product = record.get("商品信息", {}) or {}
        item_id = str(product.get("商品ID") or "").strip()
        if item_id:
            item_ids.add(item_id)
    return item_ids
