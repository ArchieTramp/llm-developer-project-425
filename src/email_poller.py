import email
import email.utils
import imaplib
import json
import os
import smtplib
import time
import uuid
from datetime import datetime, timezone
from email.header import decode_header
from email.mime.text import MIMEText

import requests
import ydb
import ydb.iam

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.yandex.ru")
IMAP_USER = os.environ["IMAP_USER"]
IMAP_PASSWORD = os.environ["IMAP_PASSWORD"]
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
HELPDESK_MAILBOX = os.environ.get("HELPDESK_MAILBOX", IMAP_USER)
AGENT_ID = os.environ["AGENT_ID"]
YC_FOLDER_ID = os.environ.get("YC_FOLDER_ID", "")
MCP_SERVER_URL = os.environ["MCP_SERVER_URL"]
MODEL_NAME = os.environ.get("MODEL_NAME", "yandexgpt")
AI_STUDIO_API_KEY = os.environ["AI_STUDIO_API_KEY"]

METADATA_TOKEN_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
)
AI_STUDIO_URL = "https://ai.api.cloud.yandex.net/v1/responses"

# YDB
_driver = ydb.Driver(
    endpoint=os.environ["YDB_ENDPOINT"],
    database=os.environ["YDB_DATABASE"],
    credentials=ydb.iam.MetadataUrlCredentials(),
)
_driver.wait(fail_fast=True, timeout=5)
_pool = ydb.QuerySessionPool(_driver)


def _get_iam_token() -> str:
    r = requests.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=5)
    r.raise_for_status()
    return r.json()["access_token"]


def _decode_header(value: str) -> str:
    parts = decode_header(value or "")
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _extract_text(msg: email.message.Message) -> str:
    # Ищем text/plain
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Если text/plain нет — ищем text/html
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/html" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""

def _mask_pii(text: str) -> str:
    # Простое PII-маскирование: email и телефоны
    import re
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", text)
    text = re.sub(r"\+?\d[\d\s\-()]{8,}\d", "<phone>", text)
    return text


def _load_history(user_id: str, limit: int = 10):
    query = """
    DECLARE $user_id AS Utf8;
    DECLARE $limit AS Uint64;
    SELECT id, role, text, created_at
    FROM messages
    WHERE user_id = $user_id
    ORDER BY created_at DESC
    LIMIT $limit;
    """
    params = {
        "$user_id": user_id,
        "$limit": (limit, ydb.PrimitiveType.Uint64),  # явно указываем Uint64
    }
    result_sets = _pool.execute_with_retries(query, params)
    rows = list(result_sets[0].rows)
    rows.reverse()  # от старых к новым
    return rows


def _save_message(user_id, role, text, ticket_id=None, model=None,
                  tokens_in=0, tokens_out=0, latency_ms=0):
    msg_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    query = """
    DECLARE $id AS Utf8;
    DECLARE $user_id AS Utf8;
    DECLARE $ticket_id AS Utf8;
    DECLARE $model AS Utf8;
    DECLARE $role AS Utf8;
    DECLARE $text AS Utf8;
    DECLARE $tokens_in AS Uint64;
    DECLARE $tokens_out AS Uint64;
    DECLARE $latency_ms AS Uint32;
    DECLARE $created_at AS Timestamp;
    UPSERT INTO messages (id, user_id, ticket_id, role, text, model,
                          tokens_in, tokens_out, latency_ms, created_at)
    VALUES ($id, $user_id, $ticket_id, $role, $text, $model,
            $tokens_in, $tokens_out, $latency_ms, $created_at);
    """
    params = {
        "$id": (msg_id, ydb.PrimitiveType.Utf8),
        "$user_id": (user_id, ydb.PrimitiveType.Utf8),
        "$ticket_id": (ticket_id if ticket_id is not None else "", ydb.PrimitiveType.Utf8),
        "$role": (role, ydb.PrimitiveType.Utf8),
        "$text": (text, ydb.PrimitiveType.Utf8),
        "$model": (model if model is not None else "", ydb.PrimitiveType.Utf8),
        "$tokens_in": (tokens_in, ydb.PrimitiveType.Uint64),
        "$tokens_out": (tokens_out, ydb.PrimitiveType.Uint64),
        "$latency_ms": (latency_ms, ydb.PrimitiveType.Uint32),
        "$created_at": (now, ydb.PrimitiveType.Timestamp),
    }
    _pool.execute_with_retries(query, params)
    return msg_id


def _update_ticket_id(user_id, msg_ids, ticket_id):
    if not msg_ids:
        return
    query = """
    DECLARE $user_id AS Utf8;
    DECLARE $id AS Utf8;
    DECLARE $ticket_id AS Utf8;
    UPDATE messages SET ticket_id = $ticket_id
    WHERE user_id = $user_id AND id = $id;
    """
    for mid in msg_ids:
        _pool.execute_with_retries(query, {
            "$user_id": user_id, "$id": mid, "$ticket_id": ticket_id,
        })


def _call_agent(history, current_text, iam_token):
    # input: история (agent -> assistant, user -> user) + текущее письмо
    input_messages = []
    for row in history:
        role = "assistant" if row.role == "agent" else "user"
        input_messages.append({"role": role, "content": row.text})
    input_messages.append({"role": "user", "content": current_text})

    payload = {
        "model": f"gpt://{YC_FOLDER_ID}/{AGENT_ID}",
        "input": input_messages,
        "tools": [{
            "type": "mcp",
            "server_label": "ydb-tickets",
            "server_url": MCP_SERVER_URL,
            "require_approval": "never",
        }],
    }
    t0 = time.monotonic()
    r = requests.post(
        AI_STUDIO_URL,
        headers={
            "Authorization": f"Api-Key {AI_STUDIO_API_KEY}",  # <-- заменили Bearer на Api-Key
            "Content-Type": "application/json",
            "x-folder-id": YC_FOLDER_ID,
        },
        json=payload,
        timeout=60,
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    if not r.ok:
        raise RuntimeError(f"AI Studio error {r.status_code}: {r.text[:500]}")
    r.raise_for_status()
    data = r.json()

    answer = ""
    ticket_id = None
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    answer += c.get("text", "")
        elif item.get("type") == "mcp_call" and item.get("name") == "create-ticket":
            try:
                out = item.get("output")
                if isinstance(out, str):
                    out = json.loads(out)
                ticket_id = out.get("ticket_id")
            except Exception:
                pass

    usage = data.get("usage", {})
    return {
        "answer": answer,
        "ticket_id": ticket_id,
        "tokens_in": usage.get("input_tokens", 0),
        "tokens_out": usage.get("output_tokens", 0),
        "latency_ms": latency_ms,
        "model": MODEL_NAME,
    }


def _send_reply(to_addr, subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = HELPDESK_MAILBOX
    msg["To"] = to_addr
    msg["Subject"] = f"Re: {subject}"
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)


def handle(event, context):
    processed = 0
    errors = []

    iam_token = _get_iam_token()

    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(IMAP_USER, IMAP_PASSWORD)
    imap.select("INBOX")

    typ, data = imap.search(None, "UNSEEN")
    if typ != "OK":
        imap.logout()
        return {"processed": 0, "errors": ["imap.search failed"]}

    nums = data[0].split()
    print(f"GOT_UNSEEN={len(nums)}")

    for num in nums:
        try:
            typ, msg_data = imap.fetch(num, "(RFC822)")
            if typ != "OK":
                errors.append(f"fetch failed num={num!r}")
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            from_addr = email.utils.parseaddr(msg.get("From", ""))[1]
            subject = _decode_header(msg.get("Subject", ""))
            text = _extract_text(msg)
            text_masked = _mask_pii(text)
            print(f"MSG num={num!r} from={from_addr} subject={subject!r}")

            # 1) история
            try:
                history = _load_history(from_addr)
            except Exception as e:
                errors.append(f"load_history: {e}")
                history = []

            # 2) входящее сообщение
            try:
                user_msg_id = _save_message(from_addr, "user", text_masked)
            except Exception as e:
                errors.append(f"save_user_message: {e}")
                user_msg_id = None

            # 3) вызов агента
            try:
                result = _call_agent(history, text_masked, iam_token)
                print(f"AGENT_OK len={len(result['answer'])}")
            except Exception as e:
                errors.append(f"call_agent: {e}")
                result = None

            if result is None:
                # агент не ответил — письмо всё равно пометим \Seen в finally
                continue

            # 4) ответ агента
            try:
                agent_msg_id = _save_message(
                    from_addr, "agent", result["answer"],
                    model=result["model"],
                    tokens_in=result["tokens_in"],
                    tokens_out=result["tokens_out"],
                    latency_ms=result["latency_ms"],
                )
            except Exception as e:
                errors.append(f"save_agent_message: {e}")
                agent_msg_id = None

            # 5) привязка ticket_id
            if result["ticket_id"] and user_msg_id and agent_msg_id:
                try:
                    _update_ticket_id(from_addr, [user_msg_id, agent_msg_id], result["ticket_id"])
                    print(f"TICKET_LINKED id={result['ticket_id']}")
                except Exception as e:
                    errors.append(f"update_ticket_id: {e}")

            # 6) отправка ответа
            try:
                _send_reply(from_addr, subject, result["answer"])
                print(f"SEND_OK to={from_addr}")
                processed += 1
            except Exception as e:
                errors.append(f"send_reply: {e}")

        except Exception as e:
            print(f"ERR num={num!r}: {e}")
            errors.append(f"outer: {e}")
        finally:
            try:
                imap.store(num, "+FLAGS", "\\Seen")
            except Exception as e:
                print(f"MARK_SEEN_ERR num={num!r}: {e}")
                errors.append(f"mark_seen: {e}")

    imap.logout()
    return {"processed": processed, "errors": errors}