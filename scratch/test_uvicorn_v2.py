import os
import re
import subprocess
import time


def test_run():
    env = os.environ.copy()
    env["PORT"] = "8889"
    env["HOST"] = "127.0.0.1"
    env["LOG_LEVEL"] = "debug"

    cmd = [".venv/bin/uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8889"]
    print(f"Running command: {' '.join(cmd)}")

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
    )

    # Wait for startup and migrations
    time.sleep(15)
    process.terminate()
    stdout, stderr = process.communicate()

    print("--- STDERR ---")
    print(stderr if stderr else "(empty)")

    # Check if sqlalchemy logs are formatted
    sqlalchemy_pattern = (
        r"\| \[DEBUG\] sqlalchemy\.pool\.impl\.NullPool: Created new connection"
    )
    alembic_pattern = (
        r"\| \[INFO\] alembic\.runtime\.migration: Context impl SQLiteImpl\."
    )

    if re.search(sqlalchemy_pattern, stderr):
        print("\n✅ SQLAlchemy logs formatted!")
    else:
        print("\n❌ SQLAlchemy logs NOT formatted.")

    if re.search(alembic_pattern, stderr):
        print("\n✅ Alembic logs formatted!")
    else:
        print("\n❌ Alembic logs NOT formatted.")


if __name__ == "__main__":
    test_run()
