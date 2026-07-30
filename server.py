import os
import sqlite3
from pathlib import Path

from fasthtml.common import Button, H1, Main, P, Title, fast_app, serve

app, rt = fast_app()
DB_PATH = Path(
    os.environ.get("COUNTER_DB_PATH", Path(__file__).with_name("counter.db"))
)


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS counter (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                value INTEGER NOT NULL
            )
            """
        )
        db.execute("INSERT OR IGNORE INTO counter (id, value) VALUES (1, 0)")


def get_counter():
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute("SELECT value FROM counter WHERE id = 1").fetchone()
        return row[0]


def increment_counter():
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        db.execute("UPDATE counter SET value = value + 1 WHERE id = 1")
        row = db.execute("SELECT value FROM counter WHERE id = 1").fetchone()
        return row[0]


init_db()


@rt("/")
def get():
    return (
        Title("Counter"),
        Main(
            H1("Hej, världen!"),
            P(str(get_counter()), id="counter"),
            Button(
                "Räkna upp",
                hx_post="/increment",
                hx_target="#counter",
                hx_swap="outerHTML",
            ),
        ),
    )


@rt("/increment")
def post():
    return P(str(increment_counter()), id="counter")


if __name__ == "__main__":
    serve(host="127.0.0.1", port=8000, reload=False)
