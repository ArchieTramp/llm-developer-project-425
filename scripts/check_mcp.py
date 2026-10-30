import os
import json
import requests
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

RESPONSES_URL = "https://rest-assistant.api.cloud.yandex.net/v1/responses"
FOLDER_ID = os.environ.get("FOLDER_ID")
SEARCH_INDEX_ID = os.environ.get("SEARCH_INDEX_ID")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL")

# Получаем IAM токен через yc (если запускаете в Git Bash, он уже может быть в env)
import subprocess

try:
    IAM_TOKEN = subprocess.check_output(["yc", "iam", "create-token"], text=True).strip()
except Exception as e:
    IAM_TOKEN = os.environ.get("YC_IAM_TOKEN")
    if not IAM_TOKEN:
        raise RuntimeError("Не удалось получить IAM токен и нет YC_IAM_TOKEN в env") from e


def make_request(use_mcp: bool, token: str):
    print(f"\n--- Тест: use_mcp={use_mcp}, token={'IAM' if token == IAM_TOKEN else 'CUSTOM'} ---")

    tools = [
        {"type": "file_search", "vector_store_ids": [SEARCH_INDEX_ID]}
    ]

    if use_mcp:
        if not MCP_SERVER_URL:
            print("MCP_SERVER_URL не задан в .env, пропускаем MCP.")
            return
        tools.append({
            "type": "mcp",
            "server_label": "ydb-tickets",
            "server_url": MCP_SERVER_URL,
            "require_approval": "never",
        })

    payload = {
        "model": f"gpt://{FOLDER_ID}/yandexgpt/latest",
        "instructions": "Ты — Help Desk-агент.",
        "input": [{"role": "user", "content": "привет"}],
        "tools": tools,
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        r = requests.post(RESPONSES_URL, headers=headers, json=payload, timeout=60)
        print(f"HTTP Status: {r.status_code}")

        if r.status_code != 200:
            print("Ошибка HTTP:", r.text[:500])
            return

        data = r.json()

        # Проверяем наличие ошибки mcp_error
        if "error" in data and data["error"]:
            print("❌ ОШИБКА В ОТВЕТЕ:")
            print(json.dumps(data["error"], indent=2, ensure_ascii=False))
        else:
            print("✅ Ответ получен без ошибок в корне.")

        # Смотрим, что в output
        if "output" in data:
            for item in data["output"]:
                if item.get("type") == "message":
                    text = "".join(c.get("text", "") for c in item.get("content", []) if c.get("type") == "output_text")
                    print(f"💬 Текст ответа: {text[:200]}...")
                elif item.get("type") == "mcp_list_tools":
                    print(f"🔧 MCP List Tools: {item}")
                elif item.get("type") == "file_search_call":
                    print(f"🔍 File Search: {item.get('id')}")

    except Exception as e:
        print(f"Исключение при запросе: {e}")


if __name__ == "__main__":
    print(f"URL: {MCP_SERVER_URL}")
    print(f"Index: {SEARCH_INDEX_ID}")

    # Тест 1: Без MCP (только база знаний)
    make_request(use_mcp=False, token=IAM_TOKEN)

    # Тест 2: С MCP (текущий IAM токен)
    make_request(use_mcp=True, token=IAM_TOKEN)

    # Тест 3: С MCP, но с API-ключом (если вы его создали)
    API_KEY = os.environ.get("YC_API_KEY")
    if API_KEY:
        make_request(use_mcp=True, token=API_KEY)
    else:
        print("\n(Пропущен тест с API-ключом: YC_API_KEY не задан в .env)")