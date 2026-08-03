import asyncio
import hmac
import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import chess
import chess.pgn
from fasthtml.common import (
    A,
    Div,
    Form,
    H1,
    H2,
    Link,
    Input,
    Label,
    Main,
    P,
    Option,
    Script,
    Style,
    Select,
    Table,
    Tbody,
    Td,
    Th,
    Thead,
    Title,
    Tr,
    fast_app,
    serve,
)
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.routing import WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

app, rt = fast_app()
APP_DIR = Path(__file__).resolve().parent
app.mount(
    "/sounds",
    StaticFiles(directory=APP_DIR / "sounds"),
    name="sounds",
)
SCHEMA_PATH = APP_DIR / "schema.sql"
DB_PATH = Path(os.environ.get("CHESS_DB_PATH", APP_DIR / "chess.db"))
game_connections = defaultdict(set)
game_locks = defaultdict(asyncio.Lock)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        has_game_table = db.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'parti'
            """
        ).fetchone()

    if not has_game_table:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        with sqlite3.connect(DB_PATH) as db:
            db.executescript(schema)

    migrate_location_schema()
    ensure_location_name_schema()
    ensure_location_rotation_schema()
    ensure_game_date_schema()
    ensure_clock_schema()
    ensure_real_clock_schema()
    ensure_initial_clock_schema()
    ensure_game_rotation_degrees_schema()
    ensure_event_log_schema()


def migrate_location_schema():
    with sqlite3.connect(DB_PATH) as db:
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(parti)")
        }
        if "plats_id" in columns:
            return

        db.execute("PRAGMA foreign_keys = OFF")
        db.executescript(
            """
            BEGIN IMMEDIATE;

            CREATE TABLE plats (
              id         INTEGER PRIMARY KEY,
              namn       TEXT NOT NULL DEFAULT '',
              latitud    REAL NOT NULL,
              longitud   REAL NOT NULL,
              rotation   INTEGER NOT NULL DEFAULT 0
                         CHECK (rotation BETWEEN -90 AND 90),
              storlek    REAL NOT NULL DEFAULT 800 CHECK (storlek > 0),
              UNIQUE (latitud, longitud, storlek)
            );

            INSERT INTO plats (latitud, longitud, storlek)
            SELECT DISTINCT latitud, longitud, storlek
            FROM parti;

            UPDATE plats SET namn = 'Plats ' || id;

            CREATE TABLE parti_new (
              id          INTEGER PRIMARY KEY,
              datum       TEXT NOT NULL DEFAULT (date('now'))
                          CHECK (datum = date(datum)),
              plats_id    INTEGER NOT NULL REFERENCES plats(id),
              rotation    INTEGER NOT NULL DEFAULT 0
                          CHECK (rotation IN (0, 90, 180, 270)),
              vit_id      INTEGER NOT NULL REFERENCES spelare(id),
              svart_id    INTEGER NOT NULL REFERENCES spelare(id),
              inkrement   INTEGER NOT NULL DEFAULT 30 CHECK (inkrement >= 0),
              vit_tid     REAL NOT NULL CHECK (vit_tid >= 0),
              svart_tid   REAL NOT NULL CHECK (svart_tid >= 0),
              vit_starttid   REAL NOT NULL DEFAULT 5400 CHECK (vit_starttid >= 0),
              svart_starttid REAL NOT NULL DEFAULT 5400 CHECK (svart_starttid >= 0),
              senast_startad REAL NOT NULL DEFAULT (
                (julianday('now') - 2440587.5) * 86400.0
              ),
              status      TEXT NOT NULL DEFAULT 'pågår'
                          CHECK (
                            status IN (
                              'pågår', 'remi', 'vit vinst', 'svart vinst'
                            )
                          ),
              CHECK (vit_id <> svart_id)
            );

            INSERT INTO parti_new (
              id, plats_id, rotation, vit_id, svart_id,
              inkrement, vit_tid, svart_tid,
              vit_starttid, svart_starttid, status
            )
            SELECT
              parti.id,
              plats.id,
              (CAST(((parti.rotation + 45) / 90) AS INTEGER) % 4) * 90,
              parti.vit_id,
              parti.svart_id,
              parti.inkrement,
              parti.vit_tid,
              parti.svart_tid,
              parti.vit_tid,
              parti.svart_tid,
              parti.status
            FROM parti
            JOIN plats
              ON plats.latitud = parti.latitud
             AND plats.longitud = parti.longitud
             AND plats.storlek = parti.storlek;

            DROP TABLE parti;
            ALTER TABLE parti_new RENAME TO parti;

            COMMIT;
            """
        )


def ensure_location_name_schema():
    with sqlite3.connect(DB_PATH) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(plats)")}
        if "namn" in columns:
            return
        db.execute("ALTER TABLE plats ADD COLUMN namn TEXT NOT NULL DEFAULT ''")
        db.execute(
            """
            UPDATE plats
            SET namn = CASE
                WHEN id = 1 THEN 'Skarpnäck 800'
                ELSE 'Plats ' || id
            END
            """
        )


def ensure_location_rotation_schema():
    with sqlite3.connect(DB_PATH) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(plats)")}
        if "rotation" not in columns:
            db.execute(
                """
                ALTER TABLE plats
                ADD COLUMN rotation INTEGER NOT NULL DEFAULT 0
                CHECK (rotation BETWEEN -90 AND 90)
                """
            )
            return

        table_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='plats'"
        ).fetchone()[0]
        if "BETWEEN -90 AND 90" in table_sql:
            return

        db.execute("PRAGMA foreign_keys = OFF")
        db.executescript(
            """
            BEGIN IMMEDIATE;

            CREATE TABLE plats_new (
              id         INTEGER PRIMARY KEY,
              namn       TEXT NOT NULL DEFAULT '',
              latitud    REAL NOT NULL,
              longitud   REAL NOT NULL,
              rotation   INTEGER NOT NULL DEFAULT 0
                         CHECK (rotation BETWEEN -90 AND 90),
              storlek    REAL NOT NULL DEFAULT 800 CHECK (storlek > 0),
              UNIQUE (latitud, longitud, storlek)
            );

            INSERT INTO plats_new (
              id, namn, latitud, longitud, rotation, storlek
            )
            SELECT
              id, namn, latitud, longitud,
              MAX(-90, MIN(90,
                CASE
                  WHEN rotation > 180 THEN rotation - 360
                  ELSE rotation
                END
              )),
              storlek
            FROM plats;

            DROP TABLE plats;
            ALTER TABLE plats_new RENAME TO plats;

            COMMIT;
            """
        )


def ensure_game_date_schema():
    with sqlite3.connect(DB_PATH) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(parti)")}
        if "datum" in columns:
            return
        db.execute("ALTER TABLE parti ADD COLUMN datum TEXT")
        db.execute("UPDATE parti SET datum = date('now') WHERE datum IS NULL")


def ensure_clock_schema():
    with sqlite3.connect(DB_PATH) as db:
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(parti)")
        }
        if "senast_startad" in columns:
            return
        db.execute(
            """
            ALTER TABLE parti
            ADD COLUMN senast_startad REAL NOT NULL DEFAULT 0
            """
        )
        db.execute(
            """
            UPDATE parti
            SET senast_startad =
                (julianday('now') - 2440587.5) * 86400.0
            """
        )
        db.execute(
            """
            CREATE TRIGGER IF NOT EXISTS parti_starta_klocka
            AFTER INSERT ON parti
            WHEN NEW.senast_startad = 0
            BEGIN
              UPDATE parti
              SET senast_startad =
                  (julianday('now') - 2440587.5) * 86400.0
              WHERE id = NEW.id;
            END
            """
        )


def ensure_real_clock_schema():
    with sqlite3.connect(DB_PATH) as db:
        column_types = {
            row[1]: row[2].upper()
            for row in db.execute("PRAGMA table_info(parti)")
        }
        if (
            column_types.get("vit_tid") == "REAL"
            and column_types.get("svart_tid") == "REAL"
            and column_types.get("senast_startad") == "REAL"
        ):
            return

        db.execute("PRAGMA foreign_keys = OFF")
        db.executescript(
            """
            BEGIN IMMEDIATE;

            CREATE TABLE parti_real_tid (
              id          INTEGER PRIMARY KEY,
              datum       TEXT NOT NULL DEFAULT (date('now'))
                          CHECK (datum = date(datum)),
              plats_id    INTEGER NOT NULL REFERENCES plats(id),
              rotation    INTEGER NOT NULL DEFAULT 0
                          CHECK (rotation IN (0, 90, 180, 270)),
              vit_id      INTEGER NOT NULL REFERENCES spelare(id),
              svart_id    INTEGER NOT NULL REFERENCES spelare(id),
              inkrement   INTEGER NOT NULL DEFAULT 30 CHECK (inkrement >= 0),
              vit_tid     REAL NOT NULL CHECK (vit_tid >= 0),
              svart_tid   REAL NOT NULL CHECK (svart_tid >= 0),
              senast_startad REAL NOT NULL DEFAULT (
                (julianday('now') - 2440587.5) * 86400.0
              ),
              status      TEXT NOT NULL DEFAULT 'pågår'
                          CHECK (
                            status IN (
                              'pågår', 'remi', 'vit vinst', 'svart vinst'
                            )
                          ),
              CHECK (vit_id <> svart_id)
            );

            INSERT INTO parti_real_tid (
              id, datum, plats_id, rotation, vit_id, svart_id, inkrement,
              vit_tid, svart_tid, senast_startad, status
            )
            SELECT
              id, datum, plats_id,
              CASE rotation
                WHEN 1 THEN 90
                WHEN 2 THEN 180
                WHEN 3 THEN 270
                ELSE rotation
              END,
              vit_id, svart_id, inkrement,
              CAST(vit_tid AS REAL), CAST(svart_tid AS REAL),
              CAST(senast_startad AS REAL), status
            FROM parti;

            DROP TABLE parti;
            ALTER TABLE parti_real_tid RENAME TO parti;

            COMMIT;
            """
        )


def ensure_initial_clock_schema():
    with sqlite3.connect(DB_PATH) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(parti)")}
        if "vit_starttid" not in columns:
            db.execute("ALTER TABLE parti ADD COLUMN vit_starttid REAL")
            db.execute("UPDATE parti SET vit_starttid = vit_tid")
        if "svart_starttid" not in columns:
            db.execute("ALTER TABLE parti ADD COLUMN svart_starttid REAL")
            db.execute("UPDATE parti SET svart_starttid = svart_tid")


def ensure_game_rotation_degrees_schema():
    with sqlite3.connect(DB_PATH) as db:
        table_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='parti'"
        ).fetchone()[0]
        if "rotation IN (0, 90, 180, 270)" in table_sql:
            return

        db.execute("PRAGMA foreign_keys = OFF")
        db.executescript(
            """
            BEGIN IMMEDIATE;

            CREATE TABLE parti_degrees (
              id          INTEGER PRIMARY KEY,
              datum       TEXT NOT NULL DEFAULT (date('now'))
                          CHECK (datum = date(datum)),
              plats_id    INTEGER NOT NULL REFERENCES plats(id),
              rotation    INTEGER NOT NULL DEFAULT 0
                          CHECK (rotation IN (0, 90, 180, 270)),
              vit_id      INTEGER NOT NULL REFERENCES spelare(id),
              svart_id    INTEGER NOT NULL REFERENCES spelare(id),
              inkrement   INTEGER NOT NULL DEFAULT 30 CHECK (inkrement >= 0),
              vit_tid     REAL NOT NULL CHECK (vit_tid >= 0),
              svart_tid   REAL NOT NULL CHECK (svart_tid >= 0),
              vit_starttid REAL NOT NULL DEFAULT 5400
                          CHECK (vit_starttid >= 0),
              svart_starttid REAL NOT NULL DEFAULT 5400
                          CHECK (svart_starttid >= 0),
              senast_startad REAL NOT NULL DEFAULT (
                (julianday('now') - 2440587.5) * 86400.0
              ),
              status      TEXT NOT NULL DEFAULT 'pågår'
                          CHECK (
                            status IN (
                              'pågår', 'remi', 'vit vinst', 'svart vinst'
                            )
                          ),
              CHECK (vit_id <> svart_id)
            );

            INSERT INTO parti_degrees (
              id, datum, plats_id, rotation, vit_id, svart_id, inkrement,
              vit_tid, svart_tid, vit_starttid, svart_starttid,
              senast_startad, status
            )
            SELECT
              id, datum, plats_id,
              CASE rotation
                WHEN 1 THEN 90
                WHEN 2 THEN 180
                WHEN 3 THEN 270
                ELSE rotation
              END,
              vit_id, svart_id, inkrement, vit_tid, svart_tid,
              vit_starttid, svart_starttid, senast_startad, status
            FROM parti;

            DROP TABLE parti;
            ALTER TABLE parti_degrees RENAME TO parti;

            CREATE TRIGGER IF NOT EXISTS parti_starta_klocka
            AFTER INSERT ON parti
            WHEN NEW.senast_startad = 0
            BEGIN
              UPDATE parti
              SET senast_startad =
                  (julianday('now') - 2440587.5) * 86400.0
              WHERE id = NEW.id;
            END;

            COMMIT;
            """
        )


def ensure_event_log_schema():
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS handelse (
              id        INTEGER PRIMARY KEY,
              timestamp TEXT NOT NULL DEFAULT (
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
              ),
              person    TEXT NOT NULL,
              text      TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trim_event_log
            AFTER INSERT ON handelse
            BEGIN
              DELETE FROM handelse
              WHERE timestamp < strftime(
                '%Y-%m-%dT%H:%M:%fZ', 'now', '-7 days'
              );
            END
            """
        )
        db.execute(
            """
            DELETE FROM handelse
            WHERE timestamp < strftime(
              '%Y-%m-%dT%H:%M:%fZ', 'now', '-7 days'
            )
            """
        )


init_db()


def log_event(person, text, db=None):
    owns_connection = db is None
    connection = db or sqlite3.connect(DB_PATH)
    try:
        connection.execute(
            "INSERT INTO handelse (person, text) VALUES (?, ?)",
            (str(person or "okänd"), str(text)),
        )
        if owns_connection:
            connection.commit()
    finally:
        if owns_connection:
            connection.close()


def get_game(game_id, player_id):
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            """
            SELECT
                parti.id,
                parti.vit_id,
                vit.namn AS vit_namn,
                parti.vit_tid,
                parti.svart_id,
                svart.namn AS svart_namn,
                parti.svart_tid,
                parti.rotation AS parti_rotation,
                plats.latitud,
                plats.longitud,
                plats.storlek,
                plats.rotation AS plats_rotation
            FROM parti
            JOIN spelare AS vit ON vit.id = parti.vit_id
            JOIN spelare AS svart ON svart.id = parti.svart_id
            JOIN plats ON plats.id = parti.plats_id
            WHERE parti.id = ?
              AND ? IN (parti.vit_id, parti.svart_id)
            """,
            (game_id, player_id),
        ).fetchone()


def load_position(db, game_id):
    board = chess.Board()
    moves = []
    for source, target in db.execute(
        """
        SELECT franruta, tillruta
        FROM drag
        WHERE parti_id = ?
        ORDER BY nummer
        """,
        (game_id,),
    ):
        uci = source + target
        piece = board.piece_at(chess.parse_square(source))
        if piece and piece.piece_type == chess.PAWN and target[1] in ("1", "8"):
            uci += "q"
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError("Databasen innehåller en ogiltig dragföljd.")
        board.push(move)
        moves.append({"from": source, "to": target})
    return board, moves


def make_game_state(db, game_id, board, moves):
    clocks = db.execute(
        """
        SELECT vit_tid, svart_tid, senast_startad
        FROM parti
        WHERE id = ?
        """,
        (game_id,),
    ).fetchone()
    white_time, black_time, started_at = clocks
    if not board.is_game_over():
        elapsed = max(0.0, time.time() - started_at)
        if board.turn == chess.WHITE:
            white_time = max(0, white_time - elapsed)
        else:
            black_time = max(0, black_time - elapsed)
    return {
        "type": "state",
        "fen": board.fen(),
        "moves": moves,
        "turn": "w" if board.turn == chess.WHITE else "b",
        "game_over": board.is_game_over(),
        "clocks": {"w": white_time, "b": black_time},
    }


def game_state(game_id):
    with sqlite3.connect(DB_PATH) as db:
        board, moves = load_position(db, game_id)
        return make_game_state(db, game_id, board, moves)


def save_move(game_id, player_id, source, target):
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        db.execute("BEGIN IMMEDIATE")
        game = db.execute(
            """
            SELECT
                vit_id, svart_id, inkrement,
                vit_tid, svart_tid, senast_startad
            FROM parti
            WHERE id = ?
            """,
            (game_id,),
        ).fetchone()
        if game is None or player_id not in game[:2]:
            raise ValueError("Spelaren hör inte till partiet.")

        board, moves = load_position(db, game_id)
        expected_player = game[0] if board.turn == chess.WHITE else game[1]
        if player_id != expected_player:
            raise ValueError("Det är inte din tur.")

        uci = source + target
        piece = board.piece_at(chess.parse_square(source))
        if piece and piece.piece_type == chess.PAWN and target[1] in ("1", "8"):
            uci += "q"
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError("Ogiltigt drag.")

        now = time.time()
        elapsed = max(0.0, now - game[5])
        if board.turn == chess.WHITE:
            white_time = max(0, game[3] - elapsed) + game[2]
            black_time = game[4]
        else:
            white_time = game[3]
            black_time = max(0, game[4] - elapsed) + game[2]

        number = len(moves) + 1
        db.execute(
            """
            INSERT INTO drag (parti_id, nummer, franruta, tillruta)
            VALUES (?, ?, ?, ?)
            """,
            (game_id, number, source, target),
        )
        db.execute(
            """
            UPDATE parti
            SET vit_tid = ?, svart_tid = ?, senast_startad = ?
            WHERE id = ?
            """,
            (white_time, black_time, now, game_id),
        )
        player_name = db.execute(
            "SELECT namn FROM spelare WHERE id = ?",
            (player_id,),
        ).fetchone()[0]
        log_event(
            player_name,
            f"Parti {game_id}: drag {number}, {source}–{target}.",
            db,
        )
        board.push(move)
        if board.is_game_over():
            log_event(
                player_name,
                f"Parti {game_id} avslutades med resultat {board.result()}.",
                db,
            )
        moves.append({"from": source, "to": target})
        return make_game_state(db, game_id, board, moves)


async def broadcast_game(game_id, message):
    connections = list(game_connections[game_id])
    results = await asyncio.gather(
        *(connection.send_json(message) for connection in connections),
        return_exceptions=True,
    )
    for connection, result in zip(connections, results):
        if isinstance(result, Exception):
            game_connections[game_id].discard(connection)


async def game_websocket(websocket):
    try:
        game_id = int(websocket.query_params["parti"])
        player_id = int(websocket.query_params["spelare"])
    except (KeyError, ValueError):
        await websocket.close(code=1008)
        return

    game = get_game(game_id, player_id)
    if game is None:
        await websocket.close(code=1008)
        return

    player_name = (
        game["vit_namn"] if player_id == game["vit_id"]
        else game["svart_namn"]
    )

    await websocket.accept()
    game_connections[game_id].add(websocket)
    log_event(player_name, f"Anslöt till parti {game_id}.")
    try:
        await websocket.send_json(game_state(game_id))
        while True:
            data = await websocket.receive_json()
            if data.get("type") != "move":
                continue
            source = data.get("from", "")
            target = data.get("to", "")
            if not (
                len(source) == 2
                and len(target) == 2
                and source[0] in "abcdefgh"
                and target[0] in "abcdefgh"
                and source[1] in "12345678"
                and target[1] in "12345678"
            ):
                log_event(
                    player_name,
                    f"Parti {game_id}: ogiltigt dragformat {source}–{target}.",
                )
                await websocket.send_json(
                    {"type": "error", "message": "Ogiltigt dragformat."}
                )
                continue
            try:
                async with game_locks[game_id]:
                    state = save_move(game_id, player_id, source, target)
                await broadcast_game(game_id, state)
            except ValueError as error:
                log_event(
                    player_name,
                    f"Parti {game_id}: drag {source}–{target} nekades: {error}",
                )
                await websocket.send_json(
                    {"type": "error", "message": str(error)}
                )
                await websocket.send_json(game_state(game_id))
    except WebSocketDisconnect:
        pass
    finally:
        game_connections[game_id].discard(websocket)
        log_event(player_name, f"Kopplade från parti {game_id}.")


app.router.routes.append(WebSocketRoute("/ws", game_websocket))


def format_clock(seconds):
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def admin_allowed(session):
    return session.get("admin") is True


def admin_connection():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def admin_redirect(section="spelare"):
    return RedirectResponse(f"/admin#{section}", status_code=303)


def admin_style():
    return Style(
        """
        main { max-width: 70rem; margin: 1rem auto; padding: 0 1rem; }
        #admin-nav { display: flex; gap: .35rem; margin-bottom: 1.5rem; border-bottom: 2px solid #bbb; }
        .admin-tab { padding: .65rem 1rem; text-decoration: none; border-radius: .4rem .4rem 0 0; }
        .admin-tab.active { color: white; background: #246; }
        .admin-section[hidden] { display: none; }
        form { display: flex; flex-wrap: wrap; align-items: end; gap: .65rem; margin: .75rem 0; }
        input, select, button { padding: .5rem; }
        input[type="number"] { appearance: textfield; -moz-appearance: textfield; }
        input[type="number"]::-webkit-inner-spin-button,
        input[type="number"]::-webkit-outer-spin-button {
            margin: 0;
            -webkit-appearance: none;
        }
        table { border-collapse: collapse; width: 100%; margin-bottom: 2rem; }
        table th, table td {
            border: 1px solid #aaa;
            padding: .1rem .3rem !important;
            line-height: 1.1;
            text-align: left;
        }
        .record { background: #f7f7f7; border: 1px solid #ddd; border-radius: .4rem; padding: .75rem; }
        .admin-section > form:first-of-type { border: 2px solid #8cb4d8; border-radius: .4rem; padding: .75rem; }
        td:last-child { white-space: nowrap; }
        .row-action { display: inline-block; margin: 0; }
        .row-action input {
            width: auto;
            height: 1.25rem;
            min-height: 0;
            margin: 0 !important;
        }
        .row-action input.reset-icon {
            height: 1.5rem;
            padding: .1rem .3rem;
            color: #064;
            font: bold 1.2rem/1 Arial, "Segoe UI Symbol", sans-serif;
        }
        .icon-action, .row-action input {
            display: inline-block;
            border: 0;
            background: transparent;
            padding: .1rem .25rem;
            font-size: 1rem;
            line-height: 1;
            text-decoration: none;
            cursor: pointer;
        }
        .admin-section { overflow-x: auto; }
        """
    )


@rt("/admin/login")
def get():
    return Title("Admin"), Main(
        H1("Admin"),
        Form(
            Label("Lösenord", Input(type="password", name="password", required=True)),
            Input(type="submit", value="Logga in"),
            method="post",
            action="/admin/login",
        ),
    )


@rt("/admin/login")
def post(password: str, session):
    if ADMIN_PASSWORD and hmac.compare_digest(password, ADMIN_PASSWORD):
        session["admin"] = True
        log_event("admin", "Loggade in.")
        return admin_redirect()
    log_event("okänd", "Misslyckat inloggningsförsök till admin.")
    return Title("Admin"), Main(H1("Inloggningen misslyckades"), A("Försök igen", href="/admin/login"))


@rt("/admin")
def get(session, edit_spelare: int = 0, edit_plats: int = 0, edit_parti: int = 0):
    if not admin_allowed(session):
        return RedirectResponse("/admin/login", status_code=303)
    with admin_connection() as db:
        players = db.execute("SELECT * FROM spelare ORDER BY id").fetchall()
        locations = db.execute("SELECT * FROM plats ORDER BY id").fetchall()
        games = db.execute(
            """
            SELECT
                parti.*,
                vit.namn AS vit_namn,
                svart.namn AS svart_namn,
                plats.namn AS plats_namn
            FROM parti
            JOIN spelare vit ON vit.id=parti.vit_id
            JOIN spelare svart ON svart.id=parti.svart_id
            JOIN plats ON plats.id=parti.plats_id
            ORDER BY parti.id DESC
            """
        ).fetchall()
        events = db.execute(
            "SELECT id, timestamp, person, text FROM handelse "
            "ORDER BY timestamp DESC, id DESC"
        ).fetchall()

    player_forms = [
        Form(
            Input(type="hidden", name="id", value=p["id"]),
            Input(name="namn", value=p["namn"], required=True),
            Input(name="telefon", value=p["telefon"], required=True),
            Input(type="email", name="mail", value=p["mail"], required=True),
            Input(type="submit", value="Spara"),
            Input(type="submit", value="Ta bort", formaction="/admin/spelare/delete"),
            method="post", action="/admin/spelare/save", cls="record",
        ) for p in players
    ]
    location_forms = [
        Form(
            Input(type="hidden", name="id", value=p["id"]),
            Input(name="namn", value=p["namn"], required=True),
            Input(type="number", step="any", name="latitud", value=p["latitud"], required=True),
            Input(type="number", step="any", name="longitud", value=p["longitud"], required=True),
            Input(type="number", name="rotation", value=p["rotation"], min=-90, max=90, required=True),
            Input(type="number", step="any", name="storlek", value=p["storlek"], required=True),
            Input(type="submit", value="Spara"),
            Input(type="submit", value="Ta bort", formaction="/admin/plats/delete"),
            method="post", action="/admin/plats/save", cls="record",
        ) for p in locations
    ]
    player_options = lambda selected: [Option(p["namn"], value=p["id"], selected=p["id"] == selected) for p in players]
    location_options = lambda selected: [Option(p["namn"], value=p["id"], selected=p["id"] == selected) for p in locations]
    game_forms = [
        Form(
            Input(type="hidden", name="id", value=g["id"]),
            Select(*location_options(g["plats_id"]), name="plats_id"),
            Select(*(Option(f"{x}°", value=x, selected=x == g["rotation"]) for x in (0, 90, 180, 270)), name="rotation"),
            Select(*player_options(g["vit_id"]), name="vit_id"),
            Select(*player_options(g["svart_id"]), name="svart_id"),
            Input(type="number", name="inkrement", value=g["inkrement"], min=0),
            Input(type="number", step="any", name="vit_tid", value=g["vit_tid"], min=0),
            Input(type="number", step="any", name="svart_tid", value=g["svart_tid"], min=0),
            Select(*(Option(x, selected=x == g["status"]) for x in ("pågår", "remi", "vit vinst", "svart vinst")), name="status"),
            Input(type="submit", value="Spara"),
            A("Drag", href=f'/admin/parti/{g["id"]}'),
            Input(type="submit", value="Ta bort", formaction="/admin/parti/delete"),
            method="post", action="/admin/parti/save", cls="record",
        ) for g in games
    ]
    selected_player = next((p for p in players if p["id"] == edit_spelare), None)
    selected_location = next((p for p in locations if p["id"] == edit_plats), None)
    selected_game = next((g for g in games if g["id"] == edit_parti), None)
    player_editor = Form(
        Input(type="hidden", name="id", value=selected_player["id"] if selected_player else 0),
        Label("Namn", Input(name="namn", value=selected_player["namn"] if selected_player else "", required=True)),
        Label("Telefon", Input(name="telefon", value=selected_player["telefon"] if selected_player else "", required=True)),
        Label("E-post", Input(type="email", name="mail", value=selected_player["mail"] if selected_player else "", required=True)),
        Input(type="submit", value="Spara ändringar" if selected_player else "Lägg till"),
        method="post", action="/admin/spelare/save",
    )
    player_table = Table(
        Thead(Tr(Th("ID"), Th("Namn"), Th("Telefon"), Th("E-post"), Th())),
        Tbody(*(Tr(
            Td(p["id"]), Td(p["namn"]), Td(p["telefon"]), Td(p["mail"]),
            Td(A("✏️", href=f'/admin?edit_spelare={p["id"]}#spelare', title="Redigera", aria_label="Redigera", cls="icon-action"), " ", Form(
                Input(type="hidden", name="id", value=p["id"]), Input(type="submit", value="🗑️", title="Ta bort", aria_label="Ta bort"),
                method="post", action="/admin/spelare/delete", cls="row-action"))
        ) for p in reversed(players))),
    )
    location_editor = Form(
        Input(type="hidden", name="id", value=selected_location["id"] if selected_location else 0),
        Label("Namn", Input(name="namn", value=selected_location["namn"] if selected_location else "", required=True)),
        Label("Latitud", Input(type="number", step="0.0000001", name="latitud", value=f'{selected_location["latitud"]:.7f}' if selected_location else "", required=True)),
        Label("Longitud", Input(type="number", step="0.0000001", name="longitud", value=f'{selected_location["longitud"]:.7f}' if selected_location else "", required=True)),
        Label("Rotation (°)", Input(type="number", name="rotation", value=selected_location["rotation"] if selected_location else 0, min=-90, max=90, required=True)),
        Label("Storlek", Input(type="number", step="any", name="storlek", value=selected_location["storlek"] if selected_location else 800, required=True)),
        Input(type="button", value="Använd min Position", id="use-current-position"),
        Input(type="submit", value="Spara ändringar" if selected_location else "Lägg till"),
        P(id="current-position-status", aria_live="polite"),
        method="post",
        action="/admin/plats/save",
        id="location-editor",
    )
    location_table = Table(
        Thead(Tr(Th("ID"), Th("Namn"), Th("Latitud"), Th("Longitud"), Th("Rotation"), Th("Storlek"), Th())),
        Tbody(*(Tr(Td(p["id"]), Td(p["namn"]), Td(f'{p["latitud"]:.7f}'), Td(f'{p["longitud"]:.7f}'), Td(f'{p["rotation"]}°'), Td(p["storlek"]), Td(
            A("✏️", href=f'/admin/plats/{p["id"]}/karta', title="Redigera på karta", aria_label="Redigera på karta", cls="icon-action"), " ", Form(
                Input(type="hidden", name="id", value=p["id"]), Input(type="submit", value="🗑️", title="Ta bort", aria_label="Ta bort"),
                method="post", action="/admin/plats/delete", cls="row-action"))) for p in reversed(locations))),
    )
    sg = selected_game
    game_editor = Form(
        Input(type="hidden", name="id", value=sg["id"] if sg else 0),
        Label("Datum", Input(type="date", name="datum", value=sg["datum"] if sg else time.strftime("%Y-%m-%d"), required=True)),
        Label("Plats", Select(*location_options(sg["plats_id"] if sg else None), name="plats_id")),
        Label("Rotation", Select(*(Option(f"{x}°", value=x, selected=bool(sg and x == sg["rotation"])) for x in (0, 90, 180, 270)), name="rotation")),
        Label("Vit", Select(*player_options(sg["vit_id"] if sg else None), name="vit_id")),
        Label("Svart", Select(*player_options(sg["svart_id"] if sg else None), name="svart_id")),
        Label("Inkrement", Input(type="number", name="inkrement", value=sg["inkrement"] if sg else 30, min=0)),
        Label("Vit tid", Input(type="number", step="any", name="vit_tid", value=sg["vit_tid"] if sg else 5400, min=0)),
        Label("Svart tid", Input(type="number", step="any", name="svart_tid", value=sg["svart_tid"] if sg else 5400, min=0)),
        Label("Vit starttid", Input(type="number", step="any", name="vit_starttid", value=sg["vit_starttid"] if sg else 5400, min=0)),
        Label("Svart starttid", Input(type="number", step="any", name="svart_starttid", value=sg["svart_starttid"] if sg else 5400, min=0)),
        Label("Status", Select(*(Option(x, selected=bool(sg and x == sg["status"])) for x in ("pågår", "remi", "vit vinst", "svart vinst")), name="status")),
        Input(type="submit", value="Spara ändringar" if sg else "Lägg till"), method="post", action="/admin/parti/save",
    )
    game_table = Table(
        Thead(Tr(Th("ID"), Th("Datum"), Th("Plats"), Th("Vit"), Th("Svart"), Th("Status"), Th())),
        Tbody(*(Tr(Td(g["id"]), Td(g["datum"]), Td(g["plats_namn"]), Td(g["vit_namn"]), Td(g["svart_namn"]), Td(g["status"]), Td(
            A("Drag", href=f'/admin/parti/{g["id"]}'), " · ", A("PGN", href=f'/admin/parti/{g["id"]}/pgn'), " · ",
            A("✏️", href=f'/admin?edit_parti={g["id"]}#partier', title="Redigera", aria_label="Redigera", cls="icon-action"), " ", Form(
                Input(type="hidden", name="id", value=g["id"]), Input(type="submit", value="🗑️", title="Ta bort", aria_label="Ta bort"),
                method="post", action="/admin/parti/delete", cls="row-action"), " ", Form(
                Input(type="hidden", name="id", value=g["id"]), Input(type="submit", value="⟳", title="Återställ parti", aria_label="Återställ parti", cls="reset-icon", onclick="return confirm('Återställ partiet och radera alla drag?')"),
                method="post", action="/admin/parti/reset", cls="row-action"))) for g in games)),
    )
    event_table = Table(
        Thead(Tr(Th("ID"), Th("Tid"), Th("Person"), Th("Händelse"))),
        Tbody(*(
            Tr(Td(event["id"]), Td(event["timestamp"]), Td(event["person"]), Td(event["text"]))
            for event in events
        )),
    )
    clear_event_log_form = Form(
        Input(
            type="submit",
            value="Nollställ loggen",
            onclick="return confirm('Nollställ loggen? Alla logghändelser raderas.')",
        ),
        method="post",
        action="/admin/logg/clear",
    )
    return Title("Admin"), admin_style(), Script(
        """
        document.addEventListener("DOMContentLoaded", () => {
            const tabs = document.querySelectorAll(".admin-tab");
            const sections = document.querySelectorAll(".admin-section");
            function showSection(name) {
                sections.forEach((section) => {
                    section.hidden = section.dataset.section !== name;
                });
                tabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
                history.replaceState(null, "", `#${name}`);
            }
            tabs.forEach((tab) => tab.addEventListener("click", (event) => {
                event.preventDefault();
                showSection(tab.dataset.tab);
            }));
            const initial = location.hash.slice(1);
            showSection([...tabs].some((tab) => tab.dataset.tab === initial) ? initial : "spelare");

            const positionButton = document.getElementById("use-current-position");
            const positionStatus = document.getElementById("current-position-status");
            const locationForm = document.getElementById("location-editor");
            function pasteCoordinates(event) {
                const match = event.clipboardData.getData("text").match(
                    /^\\s*\\(?\\s*([+-]?\\d+(?:\\.\\d+)?)\\s*[,;]\\s*([+-]?\\d+(?:\\.\\d+)?)\\s*\\)?\\s*$/
                );
                if (!match) return;
                const latitude = Number(match[1]);
                const longitude = Number(match[2]);
                if (
                    latitude < -90 || latitude > 90 ||
                    longitude < -180 || longitude > 180
                ) return;
                event.preventDefault();
                locationForm.elements.latitud.value = latitude.toFixed(7);
                locationForm.elements.longitud.value = longitude.toFixed(7);
                positionStatus.textContent = "Koordinaterna infogades.";
            }
            locationForm.elements.latitud.addEventListener("paste", pasteCoordinates);
            locationForm.elements.longitud.addEventListener("paste", pasteCoordinates);
            positionButton.addEventListener("click", () => {
                if (!navigator.geolocation) {
                    positionStatus.textContent =
                        "Webbläsaren saknar stöd för positionering.";
                    return;
                }
                positionButton.disabled = true;
                positionStatus.textContent = "Hämtar din position …";
                navigator.geolocation.getCurrentPosition(
                    (position) => {
                        locationForm.elements.latitud.value =
                            position.coords.latitude.toFixed(7);
                        locationForm.elements.longitud.value =
                            position.coords.longitude.toFixed(7);
                        positionStatus.textContent =
                            `Position infogad (noggrannhet ` +
                            `${position.coords.accuracy.toFixed(0)} m).`;
                        positionButton.disabled = false;
                    },
                    (error) => {
                        positionStatus.textContent = error.code === 1
                            ? "Tillåt platsåtkomst och försök igen."
                            : "Positionen kunde inte hämtas. Försök igen.";
                        positionButton.disabled = false;
                    },
                    {
                        enableHighAccuracy: true,
                        maximumAge: 0,
                        timeout: 15000
                    }
                );
            });
        });
        """
    ), Main(
        H1("Administration"),
        Div(A("Spelare", href="#spelare", cls="admin-tab", data_tab="spelare"), A("Platser", href="#platser", cls="admin-tab", data_tab="platser"), A("Partier", href="#partier", cls="admin-tab", data_tab="partier"), A("Logg", href="#logg", cls="admin-tab", data_tab="logg"), id="admin-nav"),
        Div(H2("Spelare"), player_table, player_editor, cls="admin-section", data_section="spelare"),
        Div(H2("Platser"), location_table, location_editor, cls="admin-section", data_section="platser"),
        Div(H2("Partier"), game_table, game_editor, cls="admin-section", data_section="partier"),
        Div(H2("Logg"), clear_event_log_form, event_table, cls="admin-section", data_section="logg"),
    )


def require_admin(session):
    return admin_allowed(session) or RedirectResponse("/admin/login", status_code=303)


@rt("/admin/logg/clear")
def post(session):
    if not admin_allowed(session):
        return RedirectResponse("/admin/login", status_code=303)
    with admin_connection() as db:
        db.execute("DELETE FROM handelse")
    return admin_redirect("logg")


@rt("/admin/spelare/save")
def post(session, namn: str, telefon: str, mail: str, id: int = 0):
    if not admin_allowed(session): return RedirectResponse("/admin/login", status_code=303)
    with admin_connection() as db:
        if id:
            db.execute("UPDATE spelare SET namn=?,telefon=?,mail=? WHERE id=?", (namn, telefon, mail, id))
            log_event("admin", f"Uppdaterade spelare {id}: {namn}.", db)
        else:
            cursor = db.execute("INSERT INTO spelare(namn,telefon,mail) VALUES(?,?,?)", (namn, telefon, mail))
            log_event("admin", f"Skapade spelare {cursor.lastrowid}: {namn}.", db)
    return admin_redirect("spelare")


@rt("/admin/spelare/delete")
def post(session, id: int):
    if not admin_allowed(session): return RedirectResponse("/admin/login", status_code=303)
    with admin_connection() as db:
        player = db.execute("SELECT namn FROM spelare WHERE id=?", (id,)).fetchone()
        db.execute("DELETE FROM spelare WHERE id=?", (id,))
        log_event("admin", f"Tog bort spelare {id}: {player['namn'] if player else 'okänd'}.", db)
    return admin_redirect("spelare")


@rt("/admin/plats/save")
def post(session, namn: str, latitud: float, longitud: float, rotation: int, storlek: float, id: int = 0):
    if not admin_allowed(session): return RedirectResponse("/admin/login", status_code=303)
    if rotation < -90 or rotation > 90:
        return PlainTextResponse("Rotation måste vara mellan -90 och +90 grader.", status_code=400)
    latitud = round(latitud, 7)
    longitud = round(longitud, 7)
    with admin_connection() as db:
        if id:
            db.execute("UPDATE plats SET namn=?,latitud=?,longitud=?,rotation=?,storlek=? WHERE id=?", (namn, latitud, longitud, rotation, storlek, id))
            log_event("admin", f"Uppdaterade plats {id}: {namn}.", db)
        else:
            cursor = db.execute("INSERT INTO plats(namn,latitud,longitud,rotation,storlek) VALUES(?,?,?,?,?)", (namn, latitud, longitud, rotation, storlek))
            log_event("admin", f"Skapade plats {cursor.lastrowid}: {namn}.", db)
    return admin_redirect("platser")


@rt("/admin/plats/{plats_id}/karta")
def get(session, plats_id: int):
    if not admin_allowed(session):
        return RedirectResponse("/admin/login", status_code=303)
    with admin_connection() as db:
        location = db.execute(
            "SELECT * FROM plats WHERE id = ?", (plats_id,)
        ).fetchone()
    if location is None:
        return PlainTextResponse("Platsen hittades inte.", status_code=404)

    return (
        Title(f'Redigera {location["namn"]}'),
        Link(
            rel="stylesheet",
            href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
        ),
        Script(src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"),
        Style(
            """
            main { max-width: 70rem; margin: 1rem auto; padding: 0 1rem; }
            #location-map { height: min(70vh, 650px); margin: 1rem 0; }
            #position-tools { display: flex; gap: .75rem; align-items: center; }
            form { display: flex; flex-wrap: wrap; gap: .75rem; align-items: end; }
            label { display: grid; gap: .2rem; }
            input { padding: .45rem; }
            input[type="number"] { appearance: textfield; -moz-appearance: textfield; }
            input[name="latitud"]::-webkit-inner-spin-button,
            input[name="latitud"]::-webkit-outer-spin-button,
            input[name="longitud"]::-webkit-inner-spin-button,
            input[name="longitud"]::-webkit-outer-spin-button,
            input[name="rotation"]::-webkit-inner-spin-button,
            input[name="rotation"]::-webkit-outer-spin-button {
                margin: 0;
                -webkit-appearance: none;
            }
            .center-arrows {
                display: grid;
                grid-template-areas:
                    "counterclockwise north clockwise"
                    "west . east"
                    ". south .";
                gap: .2rem;
                align-self: center;
            }
            .center-arrows input { min-width: 3rem; padding: .3rem; }
            #move-north { grid-area: north; }
            #move-east { grid-area: east; }
            #move-south { grid-area: south; }
            #move-west { grid-area: west; }
            #rotate-counterclockwise { grid-area: counterclockwise; }
            #rotate-clockwise { grid-area: clockwise; }
            """
        ),
        Script(
            """
            document.addEventListener("DOMContentLoaded", () => {
                const form = document.getElementById("location-form");
                const latitude = form.elements.latitud;
                const longitude = form.elements.longitud;
                const size = form.elements.storlek;
                const rotation = form.elements.rotation;
                function pasteCoordinates(event) {
                    const match = event.clipboardData.getData("text").match(
                        /^\\s*\\(?\\s*([+-]?\\d+(?:\\.\\d+)?)\\s*[,;]\\s*([+-]?\\d+(?:\\.\\d+)?)\\s*\\)?\\s*$/
                    );
                    if (!match) return;
                    const pastedLatitude = Number(match[1]);
                    const pastedLongitude = Number(match[2]);
                    if (
                        pastedLatitude < -90 || pastedLatitude > 90 ||
                        pastedLongitude < -180 || pastedLongitude > 180
                    ) return;
                    event.preventDefault();
                    latitude.value = pastedLatitude.toFixed(7);
                    longitude.value = pastedLongitude.toFixed(7);
                    updateMap();
                }
                latitude.addEventListener("paste", pasteCoordinates);
                longitude.addEventListener("paste", pasteCoordinates);
                const map = L.map("location-map").setView(
                    [Number(latitude.value), Number(longitude.value)], 15
                );
                L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
                    maxZoom: 19,
                    attribution:
                        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
                }).addTo(map);

                const centerMarker = L.marker(map.getCenter(), {
                    draggable: true
                }).addTo(map).bindTooltip("Brädets mittpunkt");
                const squareCenters = L.layerGroup().addTo(map);
                let positionMarker = null;

                function showPosition(position) {
                    const latlng = [
                        position.coords.latitude,
                        position.coords.longitude
                    ];
                    if (positionMarker) {
                        positionMarker.setLatLng(latlng);
                    } else {
                        positionMarker = L.circleMarker(latlng, {
                            radius: 8,
                            color: "#0645ad",
                            weight: 3,
                            fillColor: "#39f",
                            fillOpacity: 0.9
                        }).addTo(map).bindTooltip("Din position");
                    }
                    document.getElementById("position-status").textContent =
                        `Din position: ${latlng[0].toFixed(6)}, ` +
                        `${latlng[1].toFixed(6)}`;
                }

                function positionError(error) {
                    document.getElementById("position-status").textContent =
                        error.code === 1
                            ? "Tillåt platsåtkomst för att visa din position."
                            : "Din position kunde inte hämtas.";
                }

                function startPositionTracking() {
                    if (!navigator.geolocation) {
                        document.getElementById("position-status").textContent =
                            "Webbläsaren saknar stöd för positionering.";
                        return;
                    }
                    navigator.geolocation.watchPosition(
                        showPosition,
                        positionError,
                        {
                            enableHighAccuracy: true,
                            maximumAge: 1000,
                            timeout: 15000
                        }
                    );
                }

                function centerPoints() {
                    const lat = Number(latitude.value);
                    const lon = Number(longitude.value);
                    const cellSize = Number(size.value) / 8;
                    const angle = Number(rotation.value) * Math.PI / 180;
                    const points = [];
                    for (let rank = 0; rank < 8; rank++) {
                        for (let file = 0; file < 8; file++) {
                            const fileOffset = (file - 3.5) * cellSize;
                            const rankOffset = (rank - 3.5) * cellSize;
                        const east =
                                Math.sin(angle) * rankOffset +
                                Math.sin(angle + Math.PI / 2) * fileOffset;
                        const north =
                                Math.cos(angle) * rankOffset +
                                Math.cos(angle + Math.PI / 2) * fileOffset;
                            points.push({
                                name: "abcdefgh"[file] + (rank + 1),
                                latlng: [
                                    lat + north / 111320,
                                    lon + east /
                                        (111320 * Math.cos(lat * Math.PI / 180))
                                ]
                            });
                        }
                    }
                    return points;
                }

                function updateMap(fit = false) {
                    const center = [Number(latitude.value), Number(longitude.value)];
                    centerMarker.setLatLng(center);
                    squareCenters.clearLayers();
                    const points = centerPoints();
                    const arrivalRadius = Number(size.value) / 16;
                    points.forEach((point) => {
                        L.circle(point.latlng, {
                            radius: arrivalRadius,
                            color: "#b00",
                            weight: 2,
                            fillColor: "#e33",
                            fillOpacity: 0.8
                        }).bindTooltip(point.name).addTo(squareCenters);
                    });
                    if (fit) {
                        map.fitBounds(
                            L.latLngBounds(points.map((point) => point.latlng)),
                            {padding: [30, 30]}
                        );
                    }
                }

                function setCenter(latlng) {
                    latitude.value = latlng.lat.toFixed(7);
                    longitude.value = latlng.lng.toFixed(7);
                    updateMap();
                }

                function moveCenter(northMeters, eastMeters) {
                    const lat = Number(latitude.value);
                    const lon = Number(longitude.value);
                    setCenter({
                        lat: lat + northMeters / 111320,
                        lng: lon + eastMeters /
                            (111320 * Math.cos(lat * Math.PI / 180))
                    });
                }

                document.getElementById("move-north").addEventListener(
                    "click", () => moveCenter(1, 0)
                );
                document.getElementById("move-east").addEventListener(
                    "click", () => moveCenter(0, 1)
                );
                document.getElementById("move-south").addEventListener(
                    "click", () => moveCenter(-1, 0)
                );
                document.getElementById("move-west").addEventListener(
                    "click", () => moveCenter(0, -1)
                );
                function rotateCenter(change) {
                    rotation.value = Math.max(
                        -90,
                        Math.min(90, Number(rotation.value) + change)
                    );
                    updateMap();
                }
                document.getElementById("rotate-counterclockwise").addEventListener(
                    "click", () => rotateCenter(-1)
                );
                document.getElementById("rotate-clockwise").addEventListener(
                    "click", () => rotateCenter(1)
                );

                map.on("click", (event) => setCenter(event.latlng));
                centerMarker.on("drag", (event) => setCenter(event.target.getLatLng()));
                [latitude, longitude, size, rotation].forEach((input) => {
                    input.addEventListener("input", () => updateMap());
                });
                updateMap(true);
                startPositionTracking();
            });
            """
        ),
        Main(
            A("Till platser", href="/admin#platser"),
            H1(f'Redigera {location["namn"]}'),
            Div(
                Div("Hämtar din position …", id="position-status"),
                id="position-tools",
            ),
            Div(id="location-map"),
            Form(
                Input(type="hidden", name="id", value=location["id"]),
                Label("Namn", Input(name="namn", value=location["namn"], required=True)),
                Label("Latitud", Input(type="number", step="0.0000001", name="latitud", value=f'{location["latitud"]:.7f}', required=True)),
                Label("Longitud", Input(type="number", step="0.0000001", name="longitud", value=f'{location["longitud"]:.7f}', required=True)),
                Div(
                    Input(type="button", value="↶ −", id="rotate-counterclockwise", title="Rotera 1 grad moturs"),
                    Input(type="button", value="+ ↷", id="rotate-clockwise", title="Rotera 1 grad medurs"),
                    Input(type="button", value="↑ N", id="move-north", title="Flytta cirka 1 meter norrut"),
                    Input(type="button", value="→ Ö", id="move-east", title="Flytta cirka 1 meter österut"),
                    Input(type="button", value="↓ S", id="move-south", title="Flytta cirka 1 meter söderut"),
                    Input(type="button", value="← V", id="move-west", title="Flytta cirka 1 meter västerut"),
                    cls="center-arrows",
                    aria_label="Flytta centrumpunkten",
                ),
                Label("Rotation (°)", Input(type="number", name="rotation", min=-90, max=90, value=location["rotation"], required=True)),
                Label("Storlek (m)", Input(type="number", step="any", name="storlek", min=1, value=location["storlek"], required=True)),
                Input(type="submit", value="Spara"),
                id="location-form", method="post", action="/admin/plats/save",
            ),
        ),
    )


@rt("/admin/plats/delete")
def post(session, id: int):
    if not admin_allowed(session): return RedirectResponse("/admin/login", status_code=303)
    with admin_connection() as db:
        location = db.execute("SELECT namn FROM plats WHERE id=?", (id,)).fetchone()
        db.execute("DELETE FROM plats WHERE id=?", (id,))
        log_event("admin", f"Tog bort plats {id}: {location['namn'] if location else 'okänd'}.", db)
    return admin_redirect("platser")


@rt("/admin/parti/save")
def post(session, datum: str, plats_id: int, rotation: int, vit_id: int, svart_id: int, inkrement: int, vit_tid: float, svart_tid: float, vit_starttid: float, svart_starttid: float, status: str, id: int = 0):
    if not admin_allowed(session): return RedirectResponse("/admin/login", status_code=303)
    if rotation not in (0, 90, 180, 270):
        return PlainTextResponse("Ogiltig partirotation.", status_code=400)
    with admin_connection() as db:
        values = (datum, plats_id, rotation, vit_id, svart_id, inkrement, vit_tid, svart_tid, vit_starttid, svart_starttid, time.time(), status)
        if id:
            db.execute("UPDATE parti SET datum=?,plats_id=?,rotation=?,vit_id=?,svart_id=?,inkrement=?,vit_tid=?,svart_tid=?,vit_starttid=?,svart_starttid=?,senast_startad=?,status=? WHERE id=?", values + (id,))
            log_event("admin", f"Uppdaterade parti {id}; status {status}.", db)
        else:
            cursor = db.execute("INSERT INTO parti(datum,plats_id,rotation,vit_id,svart_id,inkrement,vit_tid,svart_tid,vit_starttid,svart_starttid,senast_startad,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", values)
            log_event("admin", f"Skapade parti {cursor.lastrowid}; status {status}.", db)
    return admin_redirect("partier")


@rt("/admin/parti/delete")
def post(session, id: int):
    if not admin_allowed(session): return RedirectResponse("/admin/login", status_code=303)
    with admin_connection() as db:
        db.execute("DELETE FROM parti WHERE id=?", (id,))
        log_event("admin", f"Tog bort parti {id}.", db)
    return admin_redirect("partier")


@rt("/admin/parti/reset")
async def post(session, id: int):
    if not admin_allowed(session):
        return RedirectResponse("/admin/login", status_code=303)
    async with game_locks[id]:
        with admin_connection() as db:
            db.execute("DELETE FROM drag WHERE parti_id = ?", (id,))
            db.execute(
                """
                UPDATE parti
                SET vit_tid = vit_starttid,
                    svart_tid = svart_starttid,
                    senast_startad = ?,
                    status = 'pågår'
                WHERE id = ?
                """,
                (time.time(), id),
            )
            log_event("admin", f"Återställde parti {id} och raderade alla drag.", db)
        state = game_state(id)
    await broadcast_game(id, state)
    return admin_redirect("partier")


@rt("/admin/parti/{parti_id}/pgn")
def get(session, parti_id: int):
    if not admin_allowed(session):
        return RedirectResponse("/admin/login", status_code=303)
    with admin_connection() as db:
        info = db.execute(
            """
            SELECT vit.namn, svart.namn, parti.status, plats.namn, parti.datum
            FROM parti
            JOIN spelare vit ON vit.id = parti.vit_id
            JOIN spelare svart ON svart.id = parti.svart_id
            JOIN plats ON plats.id = parti.plats_id
            WHERE parti.id = ?
            """,
            (parti_id,),
        ).fetchone()
        if info is None:
            return PlainTextResponse("Partiet hittades inte.", status_code=404)
        moves = db.execute(
            "SELECT franruta, tillruta FROM drag WHERE parti_id=? ORDER BY nummer",
            (parti_id,),
        ).fetchall()

    pgn_game = chess.pgn.Game()
    pgn_game.headers["Event"] = f"Parti {parti_id}"
    pgn_game.headers["Site"] = info[3]
    pgn_game.headers["White"] = info[0]
    pgn_game.headers["Black"] = info[1]
    pgn_game.headers["Date"] = info[4].replace("-", ".")
    result = {"vit vinst": "1-0", "svart vinst": "0-1", "remi": "1/2-1/2"}.get(info[2], "*")
    pgn_game.headers["Result"] = result
    board = pgn_game.board()
    node = pgn_game
    for source, target in moves:
        uci = source + target
        piece = board.piece_at(chess.parse_square(source))
        if piece and piece.piece_type == chess.PAWN and target[1] in ("1", "8"):
            uci += "q"
        move = chess.Move.from_uci(uci)
        node = node.add_variation(move)
        board.push(move)
    text = pgn_game.accept(chess.pgn.StringExporter(headers=True, variations=False, comments=False))
    return PlainTextResponse(
        text + "\n",
        media_type="application/x-chess-pgn",
        headers={"Content-Disposition": f'attachment; filename="parti-{parti_id}.pgn"'},
    )


@rt("/admin/parti/{parti_id}")
def get(session, parti_id: int, sida: int = 1):
    if not admin_allowed(session): return RedirectResponse("/admin/login", status_code=303)
    sida = max(1, sida); per_page = 20
    with admin_connection() as db:
        total = db.execute("SELECT COUNT(*) FROM drag WHERE parti_id=?", (parti_id,)).fetchone()[0]
        moves = db.execute("SELECT nummer,franruta,tillruta FROM drag WHERE parti_id=? ORDER BY nummer LIMIT ? OFFSET ?", (parti_id, per_page, (sida-1)*per_page)).fetchall()
    pages = max(1, (total + per_page - 1) // per_page)
    return Title(f"Parti {parti_id}"), admin_style(), Main(
        A("Till administration", href="/admin"), H1(f"Parti {parti_id}"),
        Table(Thead(Tr(Th("Nr"), Th("Från"), Th("Till"))), Tbody(*(Tr(Td(m["nummer"]), Td(m["franruta"]), Td(m["tillruta"])) for m in moves))),
        P(*(A(str(page), href=f"/admin/parti/{parti_id}?sida={page}") for page in range(1, pages+1))),
    )


@rt("/")
def get(parti: int = 1, spelare: int = 1):
    game = get_game(parti, spelare)
    if game is None:
        return (
            Title("Partiet hittades inte"),
            Main(
                H1("Partiet hittades inte"),
                P("Kontrollera parametrarna parti och spelare i URL:en."),
            ),
        )

    if spelare == game["vit_id"]:
        player = (game["vit_namn"], game["vit_tid"], "w")
        opponent = (game["svart_namn"], game["svart_tid"], "b")
    else:
        player = (game["svart_namn"], game["svart_tid"], "b")
        opponent = (game["vit_namn"], game["vit_tid"], "w")

    return (
        Title("Schack"),
        Link(
            rel="stylesheet",
            href="https://cdnjs.cloudflare.com/ajax/libs/chessboard-js/1.0.0/chessboard-1.0.0.min.css",
        ),
        Script(src="https://code.jquery.com/jquery-3.7.1.min.js"),
        Script(
            src="https://cdnjs.cloudflare.com/ajax/libs/chess.js/0.10.3/chess.min.js"
        ),
        Script(
            src="https://cdnjs.cloudflare.com/ajax/libs/chessboard-js/1.0.0/chessboard-1.0.0.min.js"
        ),
        Style(
            """
            html, body { height: 100%; overflow: hidden; }
            body { margin: 0; }
            main {
                width: min(100vw, 260px);
                height: 100dvh;
                margin: 0 auto;
                overflow: hidden;
            }
            @media (max-width: 600px) {
                main {
                    width: 100vw;
                    box-sizing: border-box;
                    padding: 0 1px;
                }
            }
            .player {
                display: flex;
                align-items: baseline;
                justify-content: space-between;
                gap: 1rem;
                margin: 0 0 .5rem;
            }
            .player h2, .player p { margin: 0 0 .125rem; }
            .clock { font: 700 1.5rem monospace; }
            #chessboard { width: 100%; }
            #board-map { position: relative; }
            .board-marker {
                position: absolute;
                z-index: 20;
                width: 14px;
                height: 14px;
                border: 2px solid white;
                border-radius: 50%;
                box-shadow: 0 0 3px #000;
                pointer-events: none;
                transform: translate(-50%, -50%);
            }
            #player-marker { background: #1687ff; }
            .target-marker { background: #e33; }
            #chessboard .last-move-from {
                background: #a9d18e !important;
            }
            #chessboard .last-move-to {
                background: #70ad47 !important;
            }
            #chessboard .selected-square {
                box-shadow: inset 0 0 0 4px #2980b9;
            }
            #selection-status { min-height: 1.25rem; margin: .4rem 0; }
            #navigation-status {
                min-height: 3rem;
                margin: .4rem 0;
                font-size: 130%;
            }
            #chess-status { min-height: 1.5rem; }
            """
        ),
        Script(
            """
            document.addEventListener("DOMContentLoaded", () => {
            const game = new Chess();
            const boardElement = document.getElementById("chessboard");
            const playerColor = boardElement.dataset.color;
            const clocks = {
                w: document.getElementById("clock-w"),
                b: document.getElementById("clock-b")
            };
            const clockSeconds = {
                w: Number(clocks.w.dataset.seconds),
                b: Number(clocks.b.dataset.seconds)
            };
            let activeClock = null;
            let lastClockUpdate = Date.now();
            let selectedSquare = null;
            let pendingMove = null;
            let positionWatch = null;
            let compassBearing = null;
            let compassStarted = false;
            let compassStatus = "Kompass väntar";
            let board;
            let socket;

            function deviceHeading(event) {
                if (Number.isFinite(event.webkitCompassHeading)) {
                    return event.webkitCompassHeading;
                }
                if (event.absolute && Number.isFinite(event.alpha)) {
                    const screenAngle = screen.orientation
                        ? screen.orientation.angle
                        : Number(window.orientation) || 0;
                    return (360 - event.alpha + screenAngle + 360) % 360;
                }
                return null;
            }

            function updateCompass(event) {
                const heading = deviceHeading(event);
                if (heading === null) return;
                compassBearing = heading;
                compassStatus = "Kompass aktiv";
            }

            async function startCompass() {
                if (compassStarted) return true;
                if (!("DeviceOrientationEvent" in window)) {
                    compassStatus = "Kompass stöds inte";
                    return false;
                }
                try {
                    if (typeof DeviceOrientationEvent.requestPermission === "function") {
                        const permission =
                            await DeviceOrientationEvent.requestPermission(true);
                        if (permission !== "granted") {
                            compassStatus = "Kompassåtkomst nekad";
                            return false;
                        }
                    }
                    window.addEventListener(
                        "deviceorientationabsolute",
                        updateCompass,
                        true
                    );
                    window.addEventListener(
                        "deviceorientation",
                        updateCompass,
                        true
                    );
                    compassStarted = true;
                    compassStatus = "Kompass söker";
                    return true;
                } catch (error) {
                    compassBearing = null;
                    compassStatus = `Kompassfel: ${error.message || "okänt fel"}`;
                    return false;
                }
            }

            function renderClock(color) {
                const total = Math.max(
                    0,
                    Math.floor(clockSeconds[color])
                );
                const hours = Math.floor(total / 3600);
                const minutes = Math.floor((total % 3600) / 60);
                const seconds = total % 60;
                clocks[color].textContent =
                    [hours, minutes, seconds]
                        .map((value) => String(value).padStart(2, "0"))
                        .join(":");
            }

            function advanceClock() {
                const now = Date.now();
                const elapsed = Math.floor((now - lastClockUpdate) / 1000);
                if (activeClock && elapsed > 0) {
                    clockSeconds[activeClock] = Math.max(
                        0,
                        clockSeconds[activeClock] - elapsed
                    );
                    renderClock(activeClock);
                    lastClockUpdate += elapsed * 1000;
                } else if (!activeClock) {
                    lastClockUpdate = now;
                }
            }

            function activateClock(color) {
                advanceClock();
                activeClock = color;
                lastClockUpdate = Date.now();
            }

            function updateStatus() {
                let status;
                if (game.in_checkmate()) {
                    status = `Schackmatt – ${game.turn() === "w" ? "svart" : "vit"} vann.`;
                } else if (game.in_draw()) {
                    status = "Remi.";
                } else {
                    status = `${game.turn() === "w" ? "Vit" : "Svart"} vid draget`;
                    if (game.in_check()) status += " – schack!";
                    status += ".";
                }
                document.getElementById("chess-status").textContent = status;
            }

            function onDragStart(source, piece) {
                if (pendingMove) return false;
                if (!socket || socket.readyState !== WebSocket.OPEN) return false;
                if (game.game_over()) return false;
                if (game.turn() !== playerColor) return false;
                if (
                    (game.turn() === "w" && piece.startsWith("b")) ||
                    (game.turn() === "b" && piece.startsWith("w"))
                ) return false;
            }

            function squareCenter(square) {
                const size = Number(boardElement.dataset.size);
                const squareSize = size / 8;
                const fileOffset =
                    (square.charCodeAt(0) - "a".charCodeAt(0) - 3.5) *
                    squareSize;
                const rankOffset = (Number(square[1]) - 1 - 3.5) * squareSize;
                // Geografisk bäring: 0° = norr och positiv riktning medurs.
                // Därför är östkomponenten sin(bäring) och
                // nordkomponenten cos(bäring), till skillnad från
                // matematisk vinkel där 0° ligger längs x-axeln.
                const angle = Number(boardElement.dataset.rotation) * Math.PI / 180;
                const east =
                    Math.sin(angle) * rankOffset +
                    Math.sin(angle + Math.PI / 2) * fileOffset;
                const north =
                    Math.cos(angle) * rankOffset +
                    Math.cos(angle + Math.PI / 2) * fileOffset;
                const latitude = Number(boardElement.dataset.latitude);
                const longitude = Number(boardElement.dataset.longitude);
                return {
                    latitude: latitude + north / 111320,
                    longitude:
                        longitude + east /
                        (111320 * Math.cos(latitude * Math.PI / 180))
                };
            }

            function distanceMeters(latitude, longitude, target) {
                const radius = 6371000;
                const lat1 = latitude * Math.PI / 180;
                const lat2 = target.latitude * Math.PI / 180;
                const deltaLat = lat2 - lat1;
                const deltaLon =
                    (target.longitude - longitude) * Math.PI / 180;
                const a =
                    Math.sin(deltaLat / 2) ** 2 +
                    Math.cos(lat1) * Math.cos(lat2) *
                    Math.sin(deltaLon / 2) ** 2;
                return 2 * radius * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
            }

            function bearingDegrees(latitude, longitude, target) {
                const lat1 = latitude * Math.PI / 180;
                const lat2 = target.latitude * Math.PI / 180;
                const deltaLon =
                    (target.longitude - longitude) * Math.PI / 180;
                const y = Math.sin(deltaLon) * Math.cos(lat2);
                const x =
                    Math.cos(lat1) * Math.sin(lat2) -
                    Math.sin(lat1) * Math.cos(lat2) * Math.cos(deltaLon);
                return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
            }

            function prepareSpeech() {
                if (!("speechSynthesis" in window)) return;
                window.speechSynthesis.cancel();
                const utterance = new SpeechSynthesisUtterance("");
                utterance.lang = "sv-SE";
                window.speechSynthesis.speak(utterance);
            }

            function speakSquare(square) {
                if (!("speechSynthesis" in window)) return;
                window.speechSynthesis.cancel();
                const utterance = new SpeechSynthesisUtterance(square);
                utterance.lang = "sv-SE";
                utterance.rate = 0.85;
                window.speechSynthesis.speak(utterance);
            }

            const squareAudio = new Audio(
                "/sounds/squares/female/a.mp3"
            );
            squareAudio.preload = "auto";

            function unlockAudio() {
                if (squareAudio.dataset.unlocked) {
                    return Promise.resolve(true);
                }
                squareAudio.dataset.unlocked = "true";
                squareAudio.volume = 0;
                const playing = squareAudio.play();
                if (!playing) {
                    squareAudio.volume = 1;
                    return Promise.resolve(true);
                }
                return playing.then(() => {
                    squareAudio.pause();
                    squareAudio.currentTime = 0;
                    squareAudio.volume = 1;
                    return true;
                }).catch(() => {
                    squareAudio.volume = 1;
                    delete squareAudio.dataset.unlocked;
                    return false;
                });
            }

            function playSquarePart(part) {
                return new Promise((resolve, reject) => {
                    squareAudio.onended = resolve;
                    squareAudio.onerror = reject;
                    squareAudio.src = `/sounds/squares/female/${part}.mp3`;
                    squareAudio.currentTime = 0;
                    const playback = squareAudio.play();
                    if (playback) playback.catch(reject);
                });
            }

            async function playSquareSound(square) {
                try {
                    await playSquarePart(square);
                } catch (_) {
                    speakSquare(square);
                }
            }

            const audioButton = document.getElementById("enable-audio");
            audioButton.addEventListener("click", () => {
                // Safari kräver att både ljud- och sensorbehörighet begärs
                // direkt i klickhändelsen, innan någon annan Promise inväntas.
                const compassPromise = startCompass();
                const audioPromise = unlockAudio();
                Promise.all([audioPromise, compassPromise]).then(
                    ([unlocked, compassEnabled]) => {
                        audioButton.value = unlocked && compassEnabled
                            ? "Ljud och kompass aktiverade"
                            : "Försök aktivera igen";
                        document.getElementById("audio-status").textContent =
                            `${unlocked ? "Ljud aktivt" : "Ljud kunde inte aktiveras"}. ` +
                            `${compassEnabled ? "Kompass aktiv" : compassStatus}.`;
                    }
                );
            });

            // iOS tillåter senare uppspelning först efter en användargest.
            // pointerdown körs även när spelaren väljer en ruta på brädet.
            boardElement.addEventListener("pointerdown", unlockAudio, {
                once: true,
                capture: true
            });

            function markerPosition(file, rank) {
                let x = file / 8 * 100;
                let y = (1 - rank / 8) * 100;
                if (playerColor === "b") {
                    x = 100 - x;
                    y = 100 - y;
                }
                return {
                    x: Math.max(1, Math.min(99, x)),
                    y: Math.max(1, Math.min(99, y))
                };
            }

            function showMarker(id, file, rank) {
                const marker = document.getElementById(id);
                const point = markerPosition(file, rank);
                marker.style.left = `${point.x}%`;
                marker.style.top = `${point.y}%`;
                marker.hidden = false;
            }

            function updateTargetMarkers(targetSquares) {
                const markers = [
                    document.getElementById("target-marker-1"),
                    document.getElementById("target-marker-2")
                ];
                markers.forEach((marker, index) => {
                    const square = targetSquares[index];
                    if (!square) {
                        marker.hidden = true;
                        return;
                    }
                    const point = markerPosition(
                        square.charCodeAt(0) - "a".charCodeAt(0) + 0.5,
                        Number(square[1]) - 0.5
                    );
                    marker.style.left = `${point.x}%`;
                    marker.style.top = `${point.y}%`;
                    marker.title = `Mål ${square}`;
                    marker.hidden = false;
                });
            }

            function updateBoardMarkers(position, targetSquares) {
                const latitude = Number(boardElement.dataset.latitude);
                const longitude = Number(boardElement.dataset.longitude);
                const north =
                    (position.coords.latitude - latitude) * 111320;
                const east =
                    (position.coords.longitude - longitude) *
                    111320 * Math.cos(latitude * Math.PI / 180);
                // Inversen till samma geografiska bäringssystem:
                // 0° = norr, 90° = öster, medurs rotation.
                const angle = Number(boardElement.dataset.rotation) * Math.PI / 180;
                const rankOffset = east * Math.sin(angle) + north * Math.cos(angle);
                const fileOffset =
                    east * Math.sin(angle + Math.PI / 2) +
                    north * Math.cos(angle + Math.PI / 2);
                const squareSize = Number(boardElement.dataset.size) / 8;
                showMarker(
                    "player-marker",
                    fileOffset / squareSize + 4,
                    rankOffset / squareSize + 4
                );
                updateTargetMarkers(targetSquares);
            }

            function stopNavigation() {
                if (positionWatch !== null) {
                    navigator.geolocation.clearWatch(positionWatch);
                    positionWatch = null;
                }
                document.getElementById("player-marker").hidden = true;
                updateTargetMarkers([]);
            }

            function updateNavigation(position) {
                if (!pendingMove) return;
                const targets = pendingMove.remaining.map((square) => {
                    const target = squareCenter(square);
                    return {
                        square,
                        target,
                        distance: distanceMeters(
                            position.coords.latitude,
                            position.coords.longitude,
                            target
                        )
                    };
                });
                const nearest = targets.reduce((best, candidate) =>
                    candidate.distance < best.distance ? candidate : best
                );
                const bearing = bearingDegrees(
                    position.coords.latitude,
                    position.coords.longitude,
                    nearest.target
                );
                const relativeDirection = compassBearing === null
                    ? null
                    : (bearing - compassBearing + 540) % 360 - 180;
                const compassText = compassBearing === null
                    ? compassStatus
                    : `mobil ${compassBearing.toFixed(0)}°`;
                const directionText = relativeDirection === null
                    ? "riktning saknas"
                    : Math.abs(relativeDirection) < 0.5
                        ? "rakt fram"
                        : `${relativeDirection > 0 ? "höger" : "vänster"} ` +
                            `${Math.abs(relativeDirection).toFixed(0)}°`;
                const arrivalRadius =
                    Number(boardElement.dataset.size) / 16;
                updateBoardMarkers(position, pendingMove.remaining);
                document.getElementById("navigation-status").textContent =
                    `${pendingMove.remaining.join(" · ")} · ` +
                    `${bearing.toFixed(0)}° · ${nearest.distance.toFixed(0)} m · ` +
                    `${compassText} · ${directionText}`;

                const arrived = targets.reduce((best, candidate) => {
                    if (candidate.distance > arrivalRadius) return best;
                    return !best || candidate.distance < best.distance
                        ? candidate
                        : best;
                }, null);
                if (!arrived) return;

                playSquareSound(arrived.square);
                pendingMove.remaining = pendingMove.remaining.filter(
                    (square) => square !== arrived.square
                );
                updateBoardMarkers(position, pendingMove.remaining);
                if (pendingMove.remaining.length > 0) {
                    document.getElementById("navigation-status").textContent =
                        `Framkomst till ${arrived.square} bekräftad. ` +
                        `Kvar att besöka: ${pendingMove.remaining[0]}.`;
                } else {
                    const move = pendingMove;
                    pendingMove = null;
                    stopNavigation();
                    document.getElementById("navigation-status").textContent =
                        `Framkomst till ${arrived.square} bekräftad. Draget skickas.`;
                    socket.send(JSON.stringify({
                        type: "move",
                        from: move.from,
                        to: move.to
                    }));
                }
            }

            async function startNavigation(source, target) {
                if (!navigator.geolocation) {
                    document.getElementById("navigation-status").textContent =
                        "Webbläsaren saknar stöd för GPS-position.";
                    return false;
                }
                pendingMove = {
                    from: source,
                    to: target,
                    remaining: [source, target]
                };
                updateTargetMarkers(pendingMove.remaining);
                await startCompass();
                prepareSpeech();
                document.getElementById("navigation-status").textContent =
                    `Besök ${source} och ${target} i valfri ordning.`;
                positionWatch = navigator.geolocation.watchPosition(
                    updateNavigation,
                    (error) => {
                        document.getElementById("navigation-status").textContent =
                            `GPS-fel: ${error.message}`;
                    },
                    {enableHighAccuracy: true, maximumAge: 0, timeout: 15000}
                );
                return true;
            }

            function sendMove(source, target) {
                if (!socket || socket.readyState !== WebSocket.OPEN) {
                    return false;
                }
                const move = game.move({
                    from: source,
                    to: target,
                    promotion: "q"
                });
                if (move === null) return false;
                game.undo();
                return startNavigation(source, target);
            }

            function onDrop(source, target) {
                sendMove(source, target);
                return "snapback";
            }

            function onSnapEnd() {
                board.position(game.fen());
            }

            function highlightLastMove(moves) {
                boardElement
                    .querySelectorAll(".last-move-from, .last-move-to")
                    .forEach((square) => {
                        square.classList.remove(
                            "last-move-from",
                            "last-move-to"
                        );
                    });
                if (moves.length === 0) return;

                const move = moves[moves.length - 1];
                boardElement
                    .querySelector(`.square-${move.from}`)
                    ?.classList.add("last-move-from");
                boardElement
                    .querySelector(`.square-${move.to}`)
                    ?.classList.add("last-move-to");
            }

            function selectSquare(square) {
                boardElement
                    .querySelector(".selected-square")
                    ?.classList.remove("selected-square");
                selectedSquare = square;
                document.getElementById("selection-status").textContent =
                    square ? `Vald ruta: ${square}` : "";
                if (square) {
                    boardElement
                        .querySelector(`.square-${square}`)
                        ?.classList.add("selected-square");
                }
            }

            board = Chessboard("chessboard", {
                draggable: true,
                position: "start",
                orientation: playerColor === "w" ? "white" : "black",
                pieceTheme:
                    "https://chessboardjs.com/img/chesspieces/wikipedia/{piece}.png",
                onDragStart,
                onDrop,
                onSnapEnd
            });

            function handleSquareClick(event) {
                const squareElement = event.target.closest("[data-square]");
                if (!squareElement) return;
                const selectionStatus =
                    document.getElementById("selection-status");
                if (game.game_over()) {
                    selectionStatus.textContent = "Partiet är avslutat.";
                    return;
                }
                if (!socket || socket.readyState !== WebSocket.OPEN) {
                    selectionStatus.textContent = "Ingen serveranslutning.";
                    return;
                }
                if (game.turn() !== playerColor) {
                    selectionStatus.textContent = "Det är inte din tur.";
                    return;
                }
                if (pendingMove) {
                    selectionStatus.textContent =
                        "Slutför GPS-besöket för det valda draget.";
                    return;
                }

                const square = squareElement.dataset.square;
                const piece = game.get(square);
                const pieceImage = squareElement.querySelector("img");
                const displayedPiece =
                    pieceImage?.dataset.piece ||
                    pieceImage?.src.split("/").pop()?.split(".")[0];
                const ownPiece =
                    piece?.color === playerColor ||
                    displayedPiece?.startsWith(playerColor);
                if (!selectedSquare) {
                    if (ownPiece) selectSquare(square);
                    else {
                        selectionStatus.textContent =
                            `Klickad ruta: ${square} – välj en egen pjäs.`;
                    }
                    return;
                }

                if (square === selectedSquare) {
                    selectSquare(null);
                } else if (sendMove(selectedSquare, square)) {
                    selectSquare(null);
                } else if (ownPiece) {
                    selectSquare(square);
                } else {
                    selectSquare(null);
                    selectionStatus.textContent = "Ogiltig tillruta.";
                }
            }

            boardElement.addEventListener(
                "pointerdown",
                handleSquareClick,
                true
            );

            const protocol = location.protocol === "https:" ? "wss" : "ws";
            socket = new WebSocket(
                `${protocol}://${location.host}/ws` +
                `?parti=${boardElement.dataset.parti}` +
                `&spelare=${boardElement.dataset.spelare}`
            );

            socket.addEventListener("message", (event) => {
                const message = JSON.parse(event.data);
                if (message.type === "state") {
                    game.load(message.fen);
                    board.position(message.fen);
                    selectSquare(null);
                    highlightLastMove(message.moves);
                    clockSeconds.w = message.clocks.w;
                    clockSeconds.b = message.clocks.b;
                    renderClock("w");
                    renderClock("b");
                    activateClock(message.game_over ? null : message.turn);
                    updateStatus();
                } else if (message.type === "error") {
                    document.getElementById("chess-status").textContent =
                        message.message;
                }
            });

            socket.addEventListener("close", () => {
                document.getElementById("chess-status").textContent =
                    "Anslutningen till servern bröts.";
            });

            window.addEventListener("resize", () => board.resize());
            window.setInterval(advanceClock, 250);
            document.getElementById("chess-status").textContent =
                "Ansluter till partiet …";
            });
            """,
        ),
        Main(
            Div(
                H2(opponent[0]),
                P(
                    format_clock(opponent[1]),
                    id=f"clock-{opponent[2]}",
                    cls="clock",
                    data_seconds=str(opponent[1]),
                ),
                cls="player opponent",
            ),
            Div(
                Div(
                    id="chessboard",
                    data_parti=str(parti),
                    data_spelare=str(spelare),
                    data_color="w" if spelare == game["vit_id"] else "b",
                    data_latitude=str(game["latitud"]),
                    data_longitude=str(game["longitud"]),
                    data_size=str(game["storlek"]),
                    data_rotation=str(
                        (game["plats_rotation"] + game["parti_rotation"])
                        % 360
                    ),
                ),
                Div(id="player-marker", cls="board-marker", title="Din position", hidden=True),
                Div(id="target-marker-1", cls="board-marker target-marker", title="Mål", hidden=True),
                Div(id="target-marker-2", cls="board-marker target-marker", title="Mål", hidden=True),
                id="board-map",
            ),
            Div(
                H2(player[0]),
                P(
                    format_clock(player[1]),
                    id=f"clock-{player[2]}",
                    cls="clock",
                    data_seconds=str(player[1]),
                ),
                cls="player current-player",
            ),
            P(id="selection-status", aria_live="polite"),
            P(id="navigation-status", aria_live="polite"),
            Input(
                type="button",
                value="Aktivera ljud och kompass",
                id="enable-audio",
            ),
            P(id="audio-status", aria_live="polite"),
            P(id="chess-status", aria_live="polite"),
        ),
    )


if __name__ == "__main__":
    serve(host="127.0.0.1", port=8000, reload=True)
