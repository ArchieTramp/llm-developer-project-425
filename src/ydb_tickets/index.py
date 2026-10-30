"""CF ydb-tickets: MCP-инструменты Help Desk-агента.

Два action'а:
  create-ticket   — создать тикет в tickets
  list-my-tickets — вернуть тикеты пользователя по user_id

Три источника события:
  1. прямой invoke:  {"action": "create-ticket", "user_id": ..., ...}
  2. HTTP через API Gateway:  {"httpMethod": "POST", "body": "<JSON-string>"}
  3. MCP Hub: аргументы инструмента приходят прямо как event (без обёртки),
     диспетчеризация по набору ключей.

PII маскируется перед INSERT/UPDATE. Перед create-ticket — guardrail
от prompt injection (regex-предфильтр, без LLM — быстро и без сети).

Драйвер YDB и QuerySessionPool создаются ОДИН РАЗ при импорте модуля.
Cloud Functions переиспользует контейнер между вызовами (warm start),
поэтому при повторных вызовах подключение к YDB уже установлено.
"""
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import ydb
import ydb.iam

from pii import mask_pii

log = logging.getLogger()
log.setLevel(logging.INFO)

YDB_ENDPOINT = os.environ["YDB_ENDPOINT"]
YDB_DATABASE = os.environ["YDB_DATABASE"]

VALID_CATEGORIES = {"bug", "docs", "feature", "access"}

# ---------- глобальный драйвер и пул (создаются при импорте) ----------
_init_error = None
try:
    _driver = ydb.Driver(
        endpoint=YDB_ENDPOINT,
        database=YDB_DATABASE,
        credentials=ydb.iam.MetadataUrlCredentials(),
    )
    _driver.wait(timeout=20, fail_fast=True)
    _pool = ydb.QuerySessionPool(_driver)
except Exception as e:  # noqa: BLE001
    _init_error = f"{type(e).__name__}: {e}"
    log.error("YDB_DRIVER_INIT_FAIL %s", _init_error)
    _driver = None
    _pool = None


# ---------- guardrail: regex-предфильтр injection ----------
INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
    re.compile(r"проигнорируй\s+(все\s+)?(предыдущие\s+)?инструкции", re.I),
    re.compile(r"забудь\s+(все\s+)?(предыдущие\s+)?инструкции", re.I),
    re.compile(r"\bDROP\s+TABLE\b", re.I),
    re.compile(r"\bDELETE\s+FROM\b", re.I),
    re.compile(r"удали\s+все\s+(тикет|заявк|обращени)", re.I),
    re.compile(r"system\s*prompt", re.I),
    re.compile(r"you\s+are\s+now\s+", re.I),
]


def is_injection(text: str) -> bool:
    if not text:
        return False
    for pat in INJECTION_PATTERNS:
        if pat.search(text):
            return True
    return False


# ---------- YDB operations ----------
def create_ticket(pool, user_id: str, category: str, text: str) -> dict:
    ticket_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    masked_text = mask_pii(text)

    query = """
        DECLARE $id AS Utf8;
        DECLARE $uid AS Utf8;
        DECLARE $cat AS Utf8;
        DECLARE $status AS Utf8;
        DECLARE $text AS Utf8;
        DECLARE $created_at AS Timestamp;
        DECLARE $updated_at AS Timestamp;

        INSERT INTO tickets
            (id, user_id, category, status, text, created_at, updated_at)
        VALUES
            ($id, $uid, $cat, $status, $text, $created_at, $updated_at);
    """
    pool.execute_with_retries(
        query,
        {
            "$id": ydb.TypedValue(ticket_id, ydb.PrimitiveType.Utf8),
            "$uid": ydb.TypedValue(user_id, ydb.PrimitiveType.Utf8),
            "$cat": ydb.TypedValue(category, ydb.PrimitiveType.Utf8),
            "$status": ydb.TypedValue("open", ydb.PrimitiveType.Utf8),
            "$text": ydb.TypedValue(masked_text, ydb.PrimitiveType.Utf8),
            "$created_at": ydb.TypedValue(now, ydb.PrimitiveType.Timestamp),
            "$updated_at": ydb.TypedValue(now, ydb.PrimitiveType.Timestamp),
        },
    )
    return {"ticket_id": ticket_id, "created_at": now.isoformat()}


def list_my_tickets(pool, user_id: str) -> list[dict]:
    query = """
        DECLARE $uid AS Utf8;
        SELECT id, status, category, text, created_at
        FROM tickets
        WHERE user_id = $uid
        ORDER BY created_at DESC
        LIMIT 50;
    """
    result_sets = pool.execute_with_retries(
        query,
        {"$uid": ydb.TypedValue(user_id, ydb.PrimitiveType.Utf8)},
    )
    rows = list(result_sets[0].rows) if result_sets else []

    return [
        {
            "id": r.id,
            "status": r.status,
            "category": r.category,
            "text": r.text,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ---------- диспетчеризация ----------
def _normalize_event(event):
    if not isinstance(event, dict):
        return {}
    if "httpMethod" in event or ("body" in event and "requestContext" in event):
        body = event.get("body") or "{}"
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                body = {}
        return body if isinstance(body, dict) else {}
    return event


def _resolve_action(ev: dict) -> str | None:
    action = ev.get("action")
    if action:
        return action
    if "text" in ev and "category" in ev and "user_id" in ev:
        return "create-ticket"
    if "user_id" in ev and set(ev.keys()) <= {"user_id"}:
        return "list-my-tickets"
    return None


def handle(event, context):
    ev = _normalize_event(event)
    action = _resolve_action(ev)

    log.info("REQUEST action=%s keys=%s", action, sorted(ev.keys()))

    if action not in {"create-ticket", "list-my-tickets"}:
        return {"error": f"unknown action: {action}"}

    user_id = (ev.get("user_id") or "").strip().lower()
    if not user_id:
        return {"error": "user_id is required"}

    if _pool is None:
        return {"error": "ydb_unavailable", "detail": _init_error}

    if action == "create-ticket":
        category = (ev.get("category") or "").strip().lower()
        text = (ev.get("text") or "").strip()

        if category not in VALID_CATEGORIES:
            return {"error": f"invalid category: {category!r}"}
        if not text:
            return {"error": "text is required"}

        if is_injection(text):
            log.warning(
                "ALERT_INJECTION_BLOCKED user_id=%s category=%s text_len=%d",
                user_id, category, len(text),
            )
            return {"error": "injection_blocked"}

        result = create_ticket(_pool, user_id, category, text)
        log.info("TICKET_CREATED id=%s user_id=%s", result["ticket_id"], user_id)
        return result

    items = list_my_tickets(_pool, user_id)
    log.info("TICKETS_LISTED user_id=%s count=%d", user_id, len(items))
    return items