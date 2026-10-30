"""PII-маскирование на границе записи в YDB.

Формат масок фиксирован заданием:
  телефон  -> +7 (***) ***-**-NN  (последние 2 цифры сохраняются)
  email    -> [email]
  карта    -> ****-****-****-****
"""
import re

# Телефон в свободной форме: +7/8, с пробелами, скобками, дефисами.
# Ловим и 11-значные (7XXXXXXXXXX / 8XXXXXXXXXX), и 10-значные (XXXXXXXXXX).
_PHONE_RE = re.compile(
    r'(?<![\d])'
    r'(?:\+7|8)?[\s\-]?'
    r'\(?\d{3}\)?[\s\-]?'
    r'\d{3}[\s\-]?\d{2}[\s\-]?(\d{2})'
    r'(?![\d])'
)

_EMAIL_RE = re.compile(r'[\w.+\-]+@[\w\-]+\.[\w.\-]+')

_CARD_RE = re.compile(r'(?<!\d)\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}(?!\d)')


def mask_phones(text: str) -> str:
    def _repl(m: re.Match) -> str:
        last2 = m.group(1)
        return f'+7 (***) ***-**-{last2}'
    return _PHONE_RE.sub(_repl, text)


def mask_emails(text: str) -> str:
    return _EMAIL_RE.sub('[email]', text)


def mask_cards(text: str) -> str:
    return _CARD_RE.sub('****-****-****-****', text)


def mask_pii(text: str) -> str:
    if not text:
        return text
    text = mask_cards(text)
    text = mask_emails(text)
    text = mask_phones(text)
    return text