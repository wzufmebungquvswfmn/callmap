"""Build scoped filter options from task data, with a bounded TTL cache."""

import asyncio
import json
import time
from collections import OrderedDict

from flux_task_service.config import Settings
from flux_task_service.errors import ServiceError
from flux_task_service.models import (
    INDEXED_FIELDS,
    FilterOption,
    Filters,
    OptionsQuery,
    OptionsResult,
)
from flux_task_service.service import TaskService


class OptionsService:
    def __init__(self, tasks: TaskService, settings: Settings) -> None:
        self.tasks = tasks
        self.settings = settings
        self._cache: OrderedDict[str, tuple[float, OptionsResult]] = OrderedDict()
        # Coalesce simultaneous cache misses without background tasks outliving
        # the HTTP query deadline. Waiting callers recheck cache after the lock.
        self._lock = asyncio.Lock()

    async def get_options(self, query: OptionsQuery) -> OptionsResult:
        key = json.dumps(query.model_dump(), sort_keys=True, ensure_ascii=False)
        async with self._lock:
            entry = self._cache.get(key)
            if entry is not None and entry[0] > time.monotonic():
                self._cache.move_to_end(key)
                result = entry[1].model_copy(deep=True)
                result.cached = True
                return result
            self._cache.pop(key, None)
            result = await self._scan(query)
            ttl = self.settings.options_cache_ttl_seconds
            if ttl > 0:
                self._cache[key] = (
                    time.monotonic() + ttl,
                    result.model_copy(deep=True),
                )
                while len(self._cache) > self.settings.options_cache_size:
                    self._cache.popitem(last=False)
            return result

    async def _scan(self, query: OptionsQuery) -> OptionsResult:
        level = len(query.category_path) + 1 if query.field == "category" else None
        target = f"level_{level}_category" if level else query.field
        filters = query.filters
        if level:
            values = filters.model_dump()
            # Replace ancestor constraints with the exact requested path, without
            # leaving duplicate predicates for a category level.
            values["filter_fields"] = [
                field.model_dump()
                for field in filters.filter_fields
                if not field.field_name.startswith("level_")
            ]
            values["category_path"] = query.category_path
            values["query_and_or"] = "and"
            if query.category_path and not any(
                field["field_name"] in INDEXED_FIELDS
                for field in values["filter_fields"]
            ):
                values["filter_fields"].append(
                    {
                        "field_name": "task_created_at",
                        "query_type": "range",
                        "values": ["", ""],
                    }
                )
            filters = Filters.model_validate(values)
        candidate_ids: set[str] = set()
        excluded_ids: set[str] = set()
        missing_ids: set[str] = set()
        redacted_ids: set[str] = set()
        unavailable_ids: set[str] = set()
        matches: dict[str, set[str]] = {}
        cursor = query.cursor
        seen_cursors: set[str] = {cursor} if cursor else set()
        scanned = 0
        page_count = 0
        exhausted = False
        stop_reason = None
        next_cursor = None
        limit = self.tasks.page_limit(filters, query.limit)
        batch_pages = min(
            self.settings.max_pages, self.settings.options_pages_per_batch
        )
        for _ in range(batch_pages):
            page_count += 1
            page = await self.tasks.raw_page(filters, limit, cursor)
            scanned += page.scanned_count
            ids = list(dict.fromkeys(item["task_id"] for item in page.tasks))
            candidate_ids.update(ids)
            if len(candidate_ids) > self.settings.max_task_ids:
                raise ServiceError(
                    "QUERY_LIMIT_EXCEEDED",
                    "Too many tasks; narrow the options scope",
                    422,
                )
            excluded_ids.update(await self.tasks.excluded_ids(filters, ids))
            for item in page.tasks:
                task_id = item["task_id"]
                if task_id in excluded_ids:
                    continue
                if target == "task_id":
                    value = task_id
                else:
                    task = item.get("task")
                    if not isinstance(task, dict):
                        raise ServiceError(
                            "UPSTREAM_PROTOCOL_ERROR", "Task row lacks field projection"
                        )
                    projection = task.get("privacy_projection")
                    if (
                        target not in task
                        and isinstance(projection, str)
                        and projection.startswith("task-metadata-only.")
                    ):
                        unavailable_ids.add(task_id)
                        continue
                    value = task.get(target)
                if value is None or value == "":
                    missing_ids.add(task_id)
                    continue
                if not isinstance(value, str):
                    raise ServiceError(
                        "UPSTREAM_PROTOCOL_ERROR", "Option value must be a string"
                    )
                if not value.strip():
                    missing_ids.add(task_id)
                    continue
                if target == "created_by" and (
                    "*" in value
                    or value.lower() in {"[redacted]", "<redacted>", "redacted"}
                ):
                    redacted_ids.add(task_id)
                    continue
                matches.setdefault(value, set()).add(task_id)
            if not page.has_more:
                exhausted = True
                break
            cursor = self.tasks.advance_cursor(page.next_cursor, seen_cursors)
            next_cursor = cursor
            if (
                target != "task_id"
                and page.tasks
                and all(
                    isinstance(item.get("task"), dict)
                    and target not in item["task"]
                    and isinstance(item["task"].get("privacy_projection"), str)
                    and item["task"]["privacy_projection"].startswith(
                        "task-metadata-only."
                    )
                    for item in page.tasks
                )
            ):
                stop_reason = "field_unavailable"
                break
        else:
            stop_reason = "batch_limit"
        if exhausted:
            next_cursor = None
        options = []
        for value, ids in sorted(matches.items()):
            count = len(ids - excluded_ids)
            if count:
                options.append(
                    FilterOption(
                        value=value,
                        label=value,
                        task_count=count,
                        category_path=[*query.category_path, value] if level else None,
                    )
                )
        missing = len(missing_ids - excluded_ids)
        redacted = len(redacted_ids - excluded_ids)
        unavailable = len(unavailable_ids - excluded_ids)
        warnings = []
        if missing:
            warnings.append("部分任务未返回该字段或字段为空，选项仅包含可见的非空值")
        if redacted:
            warnings.append("部分人员字段已脱敏，无法用作选项；需要上游敏感字段权限")
        if unavailable:
            warnings.append(
                "上游 task-metadata-only 投影未返回该字段；"
                "需核查上游字段投影或接入独立选项数据源，不能仅据此判断缺少权限"
            )
        availability = "available"
        if not options and (unavailable or redacted):
            availability = "unavailable"
        elif missing or redacted or unavailable:
            availability = "partial"
        elif not exhausted:
            availability = "partial"
        if not exhausted and stop_reason == "batch_limit":
            warnings.append("选项来自本批任务，使用 next_cursor 可以继续加载")
        return OptionsResult(
            field=query.field,
            options=options,
            category_path=query.category_path,
            category_level=level,
            matched_task_count=len(candidate_ids - excluded_ids),
            scanned_count=scanned,
            page_count=page_count,
            missing_value_count=missing,
            redacted_value_count=redacted,
            unavailable_value_count=unavailable,
            availability=availability,
            exhausted=exhausted,
            next_cursor=next_cursor,
            stop_reason=stop_reason,
            warnings=warnings,
        )
