"""Task projection, cursor traversal and server-evaluated exclusions."""

import re
from dataclasses import dataclass

from flux_task_service.client import OpenAPIClient
from flux_task_service.config import Settings
from flux_task_service.errors import ServiceError
from flux_task_service.models import (
    INDEXED_FIELDS,
    CollectQuery,
    CollectResult,
    FilterField,
    Filters,
    PageQuery,
    PageResult,
)


@dataclass
class TaskPage:
    tasks: list[dict]
    scanned_count: int
    skipped_session_count: int
    has_more: bool
    next_cursor: str | None


class TaskService:
    def __init__(self, client: OpenAPIClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    async def raw_page(
        self, filters: Filters, limit: int, cursor: str | None = None
    ) -> TaskPage:
        body = {**filters.upstream_body(), "limit": limit}
        if cursor:
            body["cursor"] = cursor
        data = await self.client.query_tasks(body)
        items, info = data.get("items"), data.get("page_info")
        if not isinstance(items, list) or not isinstance(info, dict):
            raise ServiceError(
                "UPSTREAM_PROTOCOL_ERROR", "Task response lacks items or page_info"
            )
        if len(items) > limit or not isinstance(info.get("has_more"), bool):
            raise ServiceError(
                "UPSTREAM_PROTOCOL_ERROR", "Invalid task page size or has_more"
            )
        has_more = info["has_more"]
        next_cursor = info.get("next_cursor")
        if has_more and (
            not items
            or not isinstance(next_cursor, str)
            or not next_cursor
            or len(next_cursor) > 8192
            or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", next_cursor)
            or next_cursor == cursor
        ):
            raise ServiceError(
                "UPSTREAM_PAGINATION_ERROR", "Missing, invalid or repeated next cursor"
            )
        tasks = []
        skipped = 0
        mode_filters = [
            field for field in filters.filter_fields if field.field_name == "work_mode"
        ]
        for item in items:
            if not isinstance(item, dict) or item.get("id_kind") not in {
                "task",
                "session",
            }:
                raise ServiceError(
                    "UPSTREAM_PROTOCOL_ERROR", "Task row lacks a valid id_kind"
                )
            if item["id_kind"] == "session":
                skipped += 1
                continue
            task_id = item.get("task_id")
            if (
                not isinstance(task_id, str)
                or not re.fullmatch(r"[0-9]{1,64}", task_id)
                or not task_id.strip("0")
            ):
                raise ServiceError(
                    "UPSTREAM_PROTOCOL_ERROR", "Task row has an invalid task_id"
                )
            if filters.task_ids and task_id not in filters.task_ids:
                raise ServiceError(
                    "UPSTREAM_FILTER_MISMATCH", "Upstream returned an unrequested task"
                )
            if mode_filters:
                task = item.get("task")
                if not isinstance(task, dict) or "work_mode" not in task:
                    raise ServiceError(
                        "FILTER_FIELD_UNAVAILABLE",
                        "Task response lacks work_mode required for filtering",
                    )
                if any(task["work_mode"] not in field.values for field in mode_filters):
                    continue
            tasks.append(item)
        return TaskPage(
            tasks=tasks,
            scanned_count=len(items),
            skipped_session_count=skipped,
            has_more=has_more,
            next_cursor=next_cursor if has_more else None,
        )

    async def excluded_ids(self, filters: Filters, task_ids: list[str]) -> set[str]:
        """Query exclusion matches by ID, without reading redacted owner fields."""
        excluded = set(task_ids) & set(filters.exclude_task_ids)
        for group in filters.exclusion_groups():
            remaining = [task_id for task_id in task_ids if task_id not in excluded]
            if not remaining:
                break
            predicates = list(group)
            if not any(field.field_name in INDEXED_FIELDS for field in predicates):
                index = next(
                    (
                        field
                        for field in filters.include_fields()
                        if field.field_name in INDEXED_FIELDS
                    ),
                    None,
                )
                predicates.append(
                    index
                    or FilterField(
                        field_name="task_created_at",
                        query_type="range",
                        values=["", ""],
                    )
                )
            scope = Filters(filter_fields=predicates, task_ids=remaining)
            cursor = None
            seen_cursors: set[str] = set()
            for _ in range(self.settings.max_pages):
                page = await self.raw_page(
                    scope, 50 if len(remaining) == 1 else 200, cursor
                )
                excluded.update(item["task_id"] for item in page.tasks)
                if not page.has_more:
                    break
                cursor = self.advance_cursor(page.next_cursor, seen_cursors)
            else:
                raise ServiceError(
                    "QUERY_LIMIT_EXCEEDED", "Exclusion query exceeded page limit", 422
                )
        return excluded

    @staticmethod
    def page_limit(filters: Filters, requested: int) -> int:
        return min(requested, 50) if len(set(filters.task_ids)) == 1 else requested

    async def query_page(self, query: PageQuery) -> PageResult:
        page = await self.raw_page(
            query.filters, self.page_limit(query.filters, query.limit), query.cursor
        )
        ids = list(dict.fromkeys(item["task_id"] for item in page.tasks))
        excluded = await self.excluded_ids(query.filters, ids)
        kept = [task_id for task_id in ids if task_id not in excluded]
        return PageResult(
            task_ids=kept,
            count=len(kept),
            scanned_count=page.scanned_count,
            skipped_session_count=page.skipped_session_count,
            has_more=page.has_more,
            next_cursor=page.next_cursor,
            excluded_count=len(excluded),
        )

    async def collect(self, query: CollectQuery) -> CollectResult:
        candidates: dict[str, None] = {}
        excluded: set[str] = set()
        cursor = None
        seen_cursors: set[str] = set()
        scanned = skipped = 0
        limit = self.page_limit(query.filters, query.limit)
        for page_count in range(1, self.settings.max_pages + 1):
            page = await self.raw_page(query.filters, limit, cursor)
            ids = list(dict.fromkeys(item["task_id"] for item in page.tasks))
            candidates.update(dict.fromkeys(ids))
            scanned += page.scanned_count
            skipped += page.skipped_session_count
            if len(candidates) > self.settings.max_task_ids:
                raise ServiceError(
                    "QUERY_LIMIT_EXCEEDED", "Too many task IDs; narrow the filters", 422
                )
            excluded.update(await self.excluded_ids(query.filters, ids))
            if not page.has_more:
                kept = [task_id for task_id in candidates if task_id not in excluded]
                return CollectResult(
                    task_ids=kept,
                    count=len(kept),
                    scanned_count=scanned,
                    skipped_session_count=skipped,
                    page_count=page_count,
                    excluded_count=len(excluded),
                )
            cursor = self.advance_cursor(page.next_cursor, seen_cursors)
        raise ServiceError(
            "QUERY_LIMIT_EXCEEDED", "Too many pages; narrow the filters", 422
        )

    @staticmethod
    def advance_cursor(cursor: str | None, seen: set[str]) -> str:
        if cursor is None or cursor in seen:
            raise ServiceError("UPSTREAM_PAGINATION_ERROR", "Cursor cycle detected")
        seen.add(cursor)
        return cursor
