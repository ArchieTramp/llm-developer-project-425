import os
import json
import smtplib
from email.mime.text import MIMEText


def handle(event, context):
    # Поддержка вызова как через HTTP, так и напрямую
    if 'body' in event:
        body = json.loads(event['body'])
    else:
        body = event

    msg = MIMEText(body.get('body', ''), 'plain', 'utf-8')
    msg['Subject'] = body.get('subject', 'Help Desk digest')
    msg['From'] = os.environ['HELPDESK_MAILBOX']
    msg['To'] = os.environ['OPERATOR_EMAIL']  # Адресат фиксирован в env!

    with smtplib.SMTP_SSL(os.environ['SMTP_HOST'], 465, timeout=30) as smtp:
        smtp.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
        smtp.send_message(msg)

    return {"ok": True}