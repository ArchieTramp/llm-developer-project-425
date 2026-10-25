import email
import imaplib
import json
import os
import smtplib
from email.header import decode_header
from email.mime.text import MIMEText

import requests

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

METADATA_TOKEN_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
)
AI_STUDIO_URL = "https://llm.api.cloud.yandex.net/v1/responses"


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
    body = msg.get_body(preferencelist=("plain",))
    if body is not None:
        return body.get_content()
    body = msg.get_body(preferencelist=("html",))
    if body is not None:
        return body.get_content()
    return ""


def _call_agent(text: str, iam_token: str) -> str:
    payload = {
        "model": f"gpt://{YC_FOLDER_ID}/{AGENT_ID}",
        "input": text,
    }
    r = requests.post(
        AI_STUDIO_URL,
        headers={
            "Authorization": f"Bearer {iam_token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    # Responses API: текст ответа в output[].content[].text
    try:
        return data["output"][0]["content"][0]["text"]
    except (KeyError, IndexError):
        return json.dumps(data, ensure_ascii=False)


def _send_reply(to_addr: str, subject: str, body: str) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = HELPDESK_MAILBOX
    msg["To"] = to_addr
    msg["Subject"] = f"Re: {subject}"
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)


def handle(event, context):
    processed = 0
    iam_token = _get_iam_token()

    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(IMAP_USER, IMAP_PASSWORD)
    imap.select("INBOX")

    typ, data = imap.search(None, "UNSEEN")
    if typ != "OK":
        imap.logout()
        return {"processed": 0}

    nums = data[0].split()
    print(f"GOT_UNSEEN={len(nums)}")

    for num in nums:
        try:
            typ, msg_data = imap.fetch(num, "(RFC822)")
            if typ != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            from_addr = email.utils.parseaddr(msg.get("From", ""))[1]
            subject = _decode_header(msg.get("Subject", ""))
            text = _extract_text(msg)
            print(f"MSG num={num!r} from={from_addr} subject={subject!r}")

            answer = _call_agent(text, iam_token)
            print(f"AGENT_OK len={len(answer)}")

            _send_reply(from_addr, subject, answer)
            print(f"SEND_OK to={from_addr}")
            processed += 1
        except Exception as e:
            print(f"ERR num={num!r}: {e}")
        finally:
            # всегда помечаем \Seen, чтобы не зациклиться на проблемном письме
            try:
                imap.store(num, "+FLAGS", "\\Seen")
            except Exception as e:
                print(f"MARK_SEEN_ERR num={num!r}: {e}")

    imap.logout()
    return {"processed": processed}