# AI-агент службы поддержки

[![hexlet-check](https://github.com/ArchieTramp/llm-developer-project-425/actions/workflows/hexlet-check.yml/badge.svg)](https://github.com/ArchieTramp/llm-developer-project-425/actions)

Соберите Help Desk-агента на Yandex AI Studio: почтовый агент (IMAP/SMTP) принимает обращения сотрудников, ищет ответ в корпоративной базе знаний (RAG) и при необходимости заводит тикет и сохраняет переписку в YDB Serverless.

## Как это работает

1. **Приём почты**: Cloud Function `email-poller` раз в минуту забирает новые письма по IMAP.
2. **RAG**: Агент ищет ответ в базе знаний (Vector Store).
3. **Действия**: Агент отвечает или создаёт тикет (команда `/ticket`).
4. **Ответ**: Письмо отправляется по SMTP, история и телеметрия (токены, latency) пишутся в YDB.

## Стек

- **Язык**: Python 3.12
- **Инфраструктура**: Yandex Cloud Functions, YDB Serverless, Lockbox, Yandex AI Studio (Responses API)
- **Библиотеки**: `ydb`, `requests`

## Установка

1. Склонируйте репозиторий:
```bash
git clone https://github.com/ArchieTramp/llm-developer-project-425.git
cd llm-developer-project-425