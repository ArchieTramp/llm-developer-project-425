"""email-poller: pull-режим Help Desk-агента (финальная версия)."""
import email
import imaplib
import json
import logging
import os
import smtplib
import time
import uuid
import datetime as dt
from email.header import decode_header
from email.mime.text import MIMEText
from email.utils import parseaddr

import requests
import ydb
import ydb.iam

from pii import mask_pii

log = logging.getLogger()
log.setLevel(logging.INFO)

# ---------- конфигурация ----------
YC_FOLDER_ID     = os.environ["YC_FOLDER_ID"]
IMAP_HOST        = os.environ.get("IMAP_HOST", "imap.yandex.ru")
IMAP_USER        = os.environ["IMAP_USER"]
IMAP_PASSWORD    = os.environ["IMAP_PASSWORD"]
SMTP_HOST        = os.environ.get("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT        = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER        = os.environ["SMTP_USER"]
SMTP_PASSWORD    = os.environ["SMTP_PASSWORD"]
HELPDESK_MAILBOX = os.environ["HELPDESK_MAILBOX"]

YDB_ENDPOINT     = os.environ["YDB_ENDPOINT"]
YDB_DATABASE     = os.environ["YDB_DATABASE"]

SEARCH_INDEX_ID  = os.environ["SEARCH_INDEX_ID"]
CF_TICKETS_ID    = os.environ.get("CF_TICKETS_ID")

AGENT_MODEL      = os.environ.get("AGENT_MODEL", "yandexgpt")
HISTORY_LIMIT    = int(os.environ.get("HISTORY_LIMIT", "10"))

RESPONSES_URL = "https://rest-assistant.api.cloud.yandex.net/v1/responses"
METADATA_TOKEN_URL = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"

# ---------- глобальный драйвер и пул YDB ----------
_ydb_init_error = None
try:
    _ydb_driver = ydb.Driver(
        endpoint=YDB_ENDPOINT,
        database=YDB_DATABASE,
        credentials=ydb.iam.MetadataUrlCredentials(),
    )
    _ydb_driver.wait(timeout=20, fail_fast=True)
    _ydb_pool = ydb.QuerySessionPool(_ydb_driver)
except Exception as e:
    _ydb_init_error = f"{type(e).__name__}: {e}"
    log.error("YDB_DRIVER_INIT_FAIL %s", _ydb_init_error)
    _ydb_pool = None

SYSTEM_PROMPT = """Ты — Help Desk-агент службы поддержки сотрудников компании. Отвечай кратко, по делу, вежливо.

Перед ответом на любой вопрос ВСЕГДА обращайся к корпоративной базе знаний через file_search.
Если нашёл релевантный документ — дай краткое резюме (не более 3 предложений) и ссылку на документ.
Если в базе знаний нет ответа — честно скажи «Я не знаю ответа на этот вопрос» и предложи создать тикет, написав команду /ticket.
"""

def get_iam_token() -> str:
    api_key = os.environ.get("YC_API_KEY")
    if api_key:
        return api_key
    r = requests.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=5)
    r.raise_for_status()
    return r.json()["access_token"]

# ============================================================
# YDB helpers
# ============================================================
def read_history(pool, user_id: str, limit: int) -> list[dict]:
    query = """
        DECLARE $uid AS Utf8;
        DECLARE $lim AS Uint64;
        SELECT id, role, text, created_at
        FROM messages
        WHERE user_id = $uid
        ORDER BY created_at DESC
        LIMIT $lim;
    """
    result_sets = pool.execute_with_retries(query, {
        "$uid": ydb.TypedValue(user_id, ydb.PrimitiveType.Utf8),
        "$lim": ydb.TypedValue(limit, ydb.PrimitiveType.Uint64),
    })
    rows = list(result_sets[0].rows) if result_sets else []
    return [{"id": r.id, "role": r.role, "text": r.text, "created_at": r.created_at} for r in reversed(rows)]

def insert_message(pool, user_id: str, ticket_id: str | None, role: str, text: str,
                   model: str | None = None, tokens_in: int = 0, tokens_out: int = 0, latency_ms: int = 0) -> str:
    msg_id = str(uuid.uuid4())
    now = dt.datetime.now(dt.timezone.utc)
    query = """
        DECLARE $id AS Utf8; DECLARE $uid AS Utf8; DECLARE $ticket_id AS Utf8?;
        DECLARE $role AS Utf8; DECLARE $text AS Utf8; DECLARE $model AS Utf8?;
        DECLARE $tokens_in AS Uint64; DECLARE $tokens_out AS Uint64;
        DECLARE $latency_ms AS Uint32; DECLARE $created_at AS Timestamp;
        UPSERT INTO messages (id, user_id, ticket_id, role, text, model, tokens_in, tokens_out, latency_ms, created_at)
        VALUES ($id, $uid, $ticket_id, $role, $text, $model, $tokens_in, $tokens_out, $latency_ms, $created_at);
    """
    pool.execute_with_retries(query, {
        "$id": ydb.TypedValue(msg_id, ydb.PrimitiveType.Utf8),
        "$uid": ydb.TypedValue(user_id, ydb.PrimitiveType.Utf8),
        "$ticket_id": ydb.TypedValue(ticket_id, ydb.OptionalType(ydb.PrimitiveType.Utf8)),
        "$role": ydb.TypedValue(role, ydb.PrimitiveType.Utf8),
        "$text": ydb.TypedValue(text, ydb.PrimitiveType.Utf8),
        "$model": ydb.TypedValue(model, ydb.OptionalType(ydb.PrimitiveType.Utf8)),
        "$tokens_in": ydb.TypedValue(int(tokens_in), ydb.PrimitiveType.Uint64),
        "$tokens_out": ydb.TypedValue(int(tokens_out), ydb.PrimitiveType.Uint64),
        "$latency_ms": ydb.TypedValue(int(latency_ms), ydb.PrimitiveType.Uint32),
        "$created_at": ydb.TypedValue(now, ydb.PrimitiveType.Timestamp),
    })
    return msg_id

def update_ticket_id(pool, user_id: str, msg_ids: list[str], ticket_id: str) -> None:
    query = """
        DECLARE $uid AS Utf8; DECLARE $tid AS Utf8; DECLARE $id1 AS Utf8; DECLARE $id2 AS Utf8;
        UPDATE messages SET ticket_id = $tid WHERE user_id = $uid AND id IN ($id1, $id2);
    """
    pool.execute_with_retries(query, {
        "$uid": ydb.TypedValue(user_id, ydb.PrimitiveType.Utf8),
        "$tid": ydb.TypedValue(ticket_id, ydb.PrimitiveType.Utf8),
        "$id1": ydb.TypedValue(msg_ids[0], ydb.PrimitiveType.Utf8),
        "$id2": ydb.TypedValue(msg_ids[1], ydb.PrimitiveType.Utf8),
    })

# ============================================================
# Cloud Function invocation (симуляция MCP)
# ============================================================
def invoke_create_ticket(user_email: str, text: str) -> str | None:
    if not CF_TICKETS_ID:
        return None
    iam_token = get_iam_token()
    url = f"https://functions.yandexcloud.net/{CF_TICKETS_ID}"
    payload = {"action": "create-ticket", "user_id": user_email, "category": "bug", "text": text}
    try:
        r = requests.post(url, json=payload, headers={"Authorization": f"Bearer {iam_token}"}, timeout=10)
        r.raise_for_status()
        return r.json().get("ticket_id")
    except Exception as e:
        log.error("INVOKE_TICKET_FAIL err=%s", e)
        return None

# ============================================================
# Responses API
# ============================================================
def call_responses_api(iam_token: str, history: list[dict], current_text: str) -> dict:
    input_msgs = [{"role": "assistant" if h["role"] == "agent" else "user", "content": h["text"]} for h in history]
    input_msgs.append({"role": "user", "content": current_text})
    payload = {
        "model": f"gpt://{YC_FOLDER_ID}/{AGENT_MODEL}/latest",
        "instructions": SYSTEM_PROMPT,
        "input": input_msgs,
        "tools": [{"type": "file_search", "vector_store_ids": [SEARCH_INDEX_ID]}],
    }
    r = requests.post(RESPONSES_URL, headers={"Authorization": f"Bearer {iam_token}", "Content-Type": "application/json"}, json=payload, timeout=120)
    if r.status_code >= 400:
        log.error("RESPONSES_ERR status=%s body=%s", r.status_code, r.text[:500])
        r.raise_for_status()
    return r.json()

def extract_output_text(resp: dict) -> str:
    parts = [c.get("text", "") for item in resp.get("output", []) if item.get("type") == "message" for c in item.get("content", []) if c.get("type") == "output_text"]
    return "\n".join(p for p in parts if p).strip()

# ============================================================
# IMAP / SMTP
# ============================================================
def _decode_subject(raw: str | None) -> str:
    if not raw: return ""
    parts = [chunk.decode(enc or "utf-8", errors="replace") if isinstance(chunk, bytes) else chunk for chunk, enc in decode_header(raw)]
    return "".join(parts)

def get_plain_body(msg) -> str:
    """Ручной парсинг тела письма."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""

def send_reply(to_addr: str, subject: str, body: str) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = HELPDESK_MAILBOX
    msg["To"] = to_addr
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(msg)

# ============================================================
# Главный обработчик
# ============================================================
def handle(event, context):
    log.info("HANDLE_STARTED")
    if _ydb_pool is None:
        return {"error": "YDB_UNAVAILABLE", "detail": _ydb_init_error}

    iam_token = get_iam_token()
    pool = _ydb_pool
    processed, errors = 0, 0

    imap = imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=60)
    try:
        imap.login(IMAP_USER, IMAP_PASSWORD)
        imap.select("INBOX")
        typ, data = imap.search(None, "UNSEEN")
        if typ != "OK": return {"processed": 0, "errors": 1}

        nums = data[0].split() if data and data[0] else []
        for num in nums:
            try:
                typ, msg_data = imap.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                from_email = parseaddr(msg.get("From", ""))[1].strip().lower()
                subject = _decode_subject(msg.get("Subject"))
                raw_text = get_plain_body(msg).strip()

                if not from_email or not raw_text:
                    imap.store(num, "+FLAGS", "\\Seen")
                    continue

                history = read_history(pool, from_email, HISTORY_LIMIT)
                user_msg_id = insert_message(pool, from_email, None, "user", mask_pii(raw_text))

                ticket_id = None
                if "/ticket" in raw_text.lower():
                    ticket_id = invoke_create_ticket(from_email, raw_text)
                    answer = f"Тикет успешно создан. Номер: {ticket_id}" if ticket_id else "Не удалось создать тикет."
                else:
                    try:
                        resp = call_responses_api(iam_token, history, raw_text)
                        answer = extract_output_text(resp) or "Не удалось сформировать ответ."
                    except Exception as e:
                        log.error("AGENT_FAIL %s", e)
                        answer = "Произошла ошибка при обращении к ИИ."

                agent_msg_id = insert_message(pool, from_email, ticket_id, "agent", mask_pii(answer), model=AGENT_MODEL)
                if ticket_id:
                    update_ticket_id(pool, from_email, [user_msg_id, agent_msg_id], ticket_id)

                reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
                send_reply(from_email, reply_subject, answer)

                # Безопасная пометка \Seen
                try:
                    imap.store(num, "+FLAGS", "\\Seen")
                except Exception as e:
                    log.warning("MARK_SEEN_FAIL %s", e)

                processed += 1
            except Exception as e:
                log.exception("MSG_FAIL %s", e)
                try: imap.store(num, "+FLAGS", "\\Seen")
                except: pass
                errors += 1
    finally:
        try: imap.close()
        except: pass
        try: imap.logout()
        except: pass

    return {"processed": processed, "errors": errors}