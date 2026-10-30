"""Применяет schema.sql к YDB Serverless.

Запуск (в Git Bash):
  export YDB_ENDPOINT=$(grep '^YDB_ENDPOINT=' .env | cut -d= -f2)
  export YDB_DATABASE=$(grep '^YDB_DATABASE=' .env | cut -d= -f2)
  export YC_IAM_TOKEN=$(yc iam create-token | tr -d '\r\n')
  MSYS_NO_PATHCONV=1 ./.venv/Scripts/python.exe scripts/init_schema.py src/ydb_tickets/schema.sql
"""
import os
import sys

import ydb


def main(path: str) -> None:
    with open(path, "r", encoding="utf-8") as f:
        sql = f.read()

    driver = ydb.Driver(
        endpoint=os.environ["YDB_ENDPOINT"],
        database=os.environ["YDB_DATABASE"],
        credentials=ydb.credentials.AccessTokenCredentials(os.environ["YC_IAM_TOKEN"]),
    )

    try:
        driver.wait(timeout=10, fail_fast=True)
    except Exception as e:
        driver.stop()
        raise RuntimeError(f"YDB connect error: {e}") from e

    # В современных версиях SDK для выполнения запросов используется QuerySessionPool
    pool = ydb.QuerySessionPool(driver)

    try:
        statements = [s.strip() for s in sql.split(";") if s.strip()]

        for stmt in statements:
            # execute_with_retries выполняет запрос и автоматически ретраит при ошибках
            pool.execute_with_retries(stmt)
            first_line = stmt.splitlines()[0] if stmt.splitlines() else stmt
            print(f"OK: {first_line[:100]}")
    finally:
        driver.stop()

    print("SCHEMA_APPLIED")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "src/ydb_tickets/schema.sql")