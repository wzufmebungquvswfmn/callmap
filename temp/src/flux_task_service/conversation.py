"""Retrieve conversation text through the sensitive OpenAPI content endpoints."""

import json

from flux_task_service.client import OpenAPIClient
from flux_task_service.errors import ServiceError


def projection_hides_content(meta: dict) -> bool:
    if not isinstance(meta, dict):
        return True
    projection = meta.get("projection") or {}
    projection = projection if isinstance(projection, dict) else {}
    redaction = meta.get("redaction") or {}
    redaction = redaction if isinstance(redaction, dict) else {}
    return (
        projection.get("mode") == "metadata_only"
        or redaction.get("level") == "metadata_only"
        or "content" in (projection.get("omitted") or [])
    )


def has_content(data) -> bool:
    stack = [data]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"content", "text", "message", "body", "input", "output"}:
                    if isinstance(child, str) and child.strip():
                        return True
                stack.append(child)
        elif isinstance(value, list):
            stack.extend(value)
    return False


def error_info(exc: ServiceError) -> dict:
    if exc.upstream_code == 2002:
        return {
            "code": "CONVERSATION_FORBIDDEN",
            "message": "完整会话正文读取被拒绝，需要核查 IAM access_sensitive 权限",
            "upstream_code": 2002,
        }
    result = {"code": exc.code, "message": exc.message}
    if exc.upstream_code is not None:
        result["upstream_code"] = exc.upstream_code
    return result


def ticket_list(inventory: dict) -> list[tuple[str, str]]:
    tickets = []
    stack = [(inventory, "chunk")]
    while stack:
        value, action = stack.pop()
        if isinstance(value, dict):
            for key, mode in (
                ("ticket", action),
                ("batch_ticket", "chunk"),
                ("session_ticket", "messages"),
            ):
                ticket = value.get(key)
                if isinstance(ticket, str) and ticket:
                    tickets.append((ticket, mode))
            for key, child in value.items():
                stack.append(
                    (
                        child,
                        "messages"
                        if key in {"sessions", "session", "messages"}
                        else action,
                    )
                )
        elif isinstance(value, list):
            stack.extend((child, action) for child in value)
    return list(dict.fromkeys(tickets))


def append_document(result: dict, name: str, data: dict, byte_limit: int) -> None:
    size = len(json.dumps(data, ensure_ascii=False).encode())
    if result["bytes_read"] + size > byte_limit:
        raise ServiceError(
            "DOWNLOAD_LIMIT_EXCEEDED", "Conversation exceeds byte limit", 422
        )
    result["documents"].append({"name": name, "data": data})
    result["bytes_read"] += size


async def fetch_conversation(
    api: OpenAPIClient, task_id: str, mode: str = "full"
) -> dict:
    result = {
        "source": "flux_insight_openapi",
        "mode": mode,
        "status": "unavailable",
        "complete": False,
        "content_available": False,
        "documents": [],
        "bytes_read": 0,
        "errors": [],
    }
    try:
        flag = "include_trace" if mode == "full" else "messages_only"
        data = await api.query_tasks(
            {"task_id_list": [task_id], "limit": 50, flag: True}
        )
        append_document(
            result,
            "full-trace" if mode == "full" else "messages-only",
            data,
            api.settings.max_download_bytes,
        )
        detail = data.get("trace_detail")
        status = data.get("trace_detail_status")
        if status == "not_found":
            result["status"] = "not_found"
            result["errors"].append(
                {"code": "CONVERSATION_NOT_FOUND", "message": "OpenAPI 未找到会话正文"}
            )
            return result
        if isinstance(detail, dict):
            meta = detail.get("meta") or {}
            if isinstance(meta, dict):
                result["upstream_meta"] = meta
                result["completeness_scope"] = meta.get("completenessScope")
                result["scope"] = meta.get("scope")
                result["coverage"] = meta.get("coverage")
                result["omitted"] = meta.get("omitted", [])
                result["resources_not_embedded"] = meta.get("resourcesNotEmbedded", [])
            available = has_content(
                detail.get("data", detail)
            ) and not projection_hides_content(meta)
            if not isinstance(meta, dict):
                meta = {}
            complete = (
                meta.get("complete") if isinstance(meta.get("complete"), bool) else None
            )
            result.update(
                content_available=available, complete=complete if available else False
            )
            if available:
                result["status"] = "available" if complete is True else "partial"
                if complete is not True:
                    for entry in result.get("coverage") or []:
                        if (
                            isinstance(entry, dict)
                            and entry.get("required") is True
                            and entry.get("status") in {"partial", "error"}
                        ):
                            result["errors"].append(
                                {
                                    "code": "CONVERSATION_SOURCE_INCOMPLETE",
                                    "source": entry.get("source"),
                                    "message": entry.get("reason")
                                    or "Required conversation source is incomplete",
                                }
                            )
                return result
            if projection_hides_content(meta):
                result["errors"].append(
                    {
                        "code": "CONVERSATION_CONTENT_HIDDEN",
                        "message": "上游 metadata-only 投影隐藏了会话正文",
                    }
                )
                return result
    except ServiceError as exc:
        result["errors"].append(error_info(exc))
        if exc.upstream_code == 2002 or exc.code == "AUTH_NOT_CONFIGURED":
            return result
        # Large/full responses can be read with the same identity in batches.
        if (
            exc.upstream_code not in {1001, 1002, 1005}
            and exc.code != "DOWNLOAD_LIMIT_EXCEEDED"
        ):
            return result
    try:
        inventory = await api.request_json(
            "/task/trace", {"task_id": task_id, "action": "inventory"}
        )
        append_document(result, "inventory", inventory, api.settings.max_download_bytes)
    except ServiceError as exc:
        result["errors"].append(error_info(exc))
        return result
    tickets = ticket_list(inventory)
    if not tickets:
        result["errors"].append(
            {
                "code": "CONVERSATION_TICKETS_UNAVAILABLE",
                "message": "Inventory 未返回可识别票据，不能确认完整会话",
            }
        )
        return result
    complete_flags = []
    failed = False
    for index, (ticket, action) in enumerate(tickets):
        cursor = None
        seen = set()
        for page in range(api.settings.max_pages):
            body = {"task_id": task_id, "action": action, "ticket": ticket}
            if cursor:
                body["cursor"] = cursor
            try:
                data = await api.request_json("/task/trace", body)
                append_document(
                    result,
                    f"{index}-{action}-{page}",
                    data,
                    api.settings.max_download_bytes,
                )
            except ServiceError as exc:
                failed = True
                result["errors"].append(error_info(exc))
                break
            payload = data.get("data", data)
            meta = (
                data.get("meta")
                or (payload.get("meta") if isinstance(payload, dict) else {})
                or {}
            )
            if not isinstance(meta, dict):
                failed = True
                result["errors"].append(
                    {
                        "code": "CONVERSATION_PROTOCOL_ERROR",
                        "message": "Invalid conversation metadata",
                    }
                )
                break
            complete_flags.append(meta.get("complete"))
            if not projection_hides_content(meta) and has_content(payload):
                result["content_available"] = True
            if projection_hides_content(meta):
                failed = True
                result["errors"].append(
                    {
                        "code": "CONVERSATION_CONTENT_HIDDEN",
                        "message": "读取批次的会话正文被隐藏",
                    }
                )
            next_cursor = meta.get("next_cursor") or meta.get("cursor")
            if isinstance(payload, dict):
                next_cursor = (
                    next_cursor or payload.get("next_cursor") or payload.get("cursor")
                )
            more = meta.get(
                "has_more",
                payload.get("has_more") if isinstance(payload, dict) else None,
            )
            if not next_cursor and more is not True:
                break
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen
            ):
                failed = True
                result["errors"].append(
                    {
                        "code": "CONVERSATION_PAGINATION_INCOMPLETE",
                        "message": "会话分页游标缺失或重复",
                    }
                )
                break
            cursor = next_cursor
            seen.add(cursor)
        else:
            failed = True
            result["errors"].append(
                {
                    "code": "CONVERSATION_PAGINATION_INCOMPLETE",
                    "message": "会话读取达到页数上限",
                }
            )
    if result["content_available"]:
        result["complete"] = (
            False
            if failed or False in complete_flags
            else True
            if complete_flags and all(flag is True for flag in complete_flags)
            else None
        )
        result["status"] = "available" if result["complete"] is True else "partial"
    else:
        result["errors"].append(
            {
                "code": "CONVERSATION_CONTENT_UNAVAILABLE",
                "message": "批次未包含可识别的会话正文，不能视为完整会话",
            }
        )
    return result
