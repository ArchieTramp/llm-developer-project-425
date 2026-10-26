import json
import os
import uuid
from datetime import datetime, timezone

import ydb
import ydb.iam

driver = ydb.Driver(
    endpoint=os.environ["YDB_ENDPOINT"],
    database=os.environ["YDB_DATABASE"],
    credentials=ydb.iam.MetadataUrlCredentials(),
)
driver.wait(fail_fast=True, timeout=5)
pool = ydb.QuerySessionPool(driver)


def _create_ticket(event):
    ticket_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    query = """
    DECLARE $id AS Utf8;
    DECLARE $user_id AS Utf8;
    DECLARE $category AS Utf8;
    DECLARE $status AS Utf8;
    DECLARE $text AS Utf8;
    DECLARE $created_at AS Timestamp;
    DECLARE $updated_at AS Timestamp;
    UPSERT INTO tickets (id, user_id, category, status, text, created_at, updated_at)
    VALUES ($id, $user_id, $category, $status, $text, $created_at, $updated_at);
    """

    parameters = {
        "$id": (ticket_id, ydb.PrimitiveType.Utf8),
        "$user_id": (event.get("user_id", ""), ydb.PrimitiveType.Utf8),
        "$category": (event.get("category", "other"), ydb.PrimitiveType.Utf8),
        "$status": ("open", ydb.PrimitiveType.Utf8),
        "$text": (event.get("text", ""), ydb.PrimitiveType.Utf8),
        "$created_at": (now, ydb.PrimitiveType.Timestamp),
        "$updated_at": (now, ydb.PrimitiveType.Timestamp),
    }

    pool.execute_with_retries(query, parameters)
    return {"ticket_id": ticket_id, "created_at": now.isoformat()}


def _list_my_tickets(event):
    user_id = event.get("user_id", "")

    query = """
    DECLARE $user_id AS Utf8;
    SELECT id, status, category, text, created_at
    FROM tickets
    WHERE user_id = $user_id
    ORDER BY created_at DESC
    LIMIT 50;
    """

    parameters = {"$user_id": user_id}

    result_sets = pool.execute_with_retries(query, parameters)
    rows = []
    for row in result_sets[0].rows:
        rows.append({
            "id": row.id,
            "status": row.status,
            "category": row.category,
            "text": row.text,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })
    return rows


def handle(event, context):
    # MCP Hub передаёт аргументы напрямую как event
    if "action" in event:
        action = event["action"]
        payload = event
    elif "body" in event:
        # HTTP через API Gateway
        payload = json.loads(event["body"]) if isinstance(event["body"], str) else event["body"]
        action = payload.get("action")
    else:
        # MCP-вызов: аргументы без обёртки
        payload = event
        if "user_id" in event and "text" in event and "category" in event:
            action = "create-ticket"
        elif "user_id" in event and "text" not in event:
            action = "list-my-tickets"
        else:
            action = None

    if action == "create-ticket":
        return _create_ticket(payload)
    if action == "list-my-tickets":
        return _list_my_tickets(payload)
    return {"error": f"unknown action: {action}"}