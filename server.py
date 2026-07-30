import os
import sqlite3
from pathlib import Path

from fasthtml.common import Button, H1, Main, P, Script, Title, fast_app, serve

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
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                ),
                message TEXT NOT NULL
            )
            """
        )


def get_counter():
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute("SELECT value FROM counter WHERE id = 1").fetchone()
        return row[0]


def increment_counter(message):
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        db.execute("UPDATE counter SET value = value + 1 WHERE id = 1")
        db.execute("INSERT INTO log (message) VALUES (?)", (message,))
        row = db.execute("SELECT value FROM counter WHERE id = 1").fetchone()
        return row[0]


init_db()


@rt("/")
def get():
    return (
        Title("Counter"),
        Script(
            """
            function incrementWithLocation(button) {
                if (!navigator.geolocation) {
                    alert("Webbläsaren saknar stöd för GPS-position.");
                    return;
                }

                button.disabled = true;
                navigator.geolocation.getCurrentPosition(
                    (position) => {
                        const coords = position.coords;
                        const message =
                            `WGS84 lat=${coords.latitude.toFixed(7)}, ` +
                            `lon=${coords.longitude.toFixed(7)}, ` +
                            `accuracy=${coords.accuracy.toFixed(1)}m`;

                        htmx.ajax("POST", "/increment", {
                            target: "#counter",
                            swap: "outerHTML",
                            values: {message: message}
                        }).finally(() => {
                            button.disabled = false;
                        });
                    },
                    (error) => {
                        button.disabled = false;
                        alert(`Kunde inte läsa GPS-positionen: ${error.message}`);
                    },
                    {
                        enableHighAccuracy: true,
                        timeout: 10000,
                        maximumAge: 0
                    }
                );
            }
            """
        ),
        Main(
            H1("Hej, världen!"),
            P(str(get_counter()), id="counter"),
            Button(
                "Räkna upp och spara position",
                type="button",
                onclick="incrementWithLocation(this)",
            ),
        ),
    )


@rt("/increment")
def post(message: str):
    return P(str(increment_counter(message)), id="counter")


if __name__ == "__main__":
    serve(host="127.0.0.1", port=8000, reload=False)
