import asyncio
import hmac
import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import chess
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
from starlette.responses import RedirectResponse
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect

app, rt = fast_app()
APP_DIR = Path(__file__).resolve().parent
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
    ensure_clock_schema()
    ensure_real_clock_schema()


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
              latitud    REAL NOT NULL,
              longitud   REAL NOT NULL,
              storlek    REAL NOT NULL DEFAULT 800 CHECK (storlek > 0),
              UNIQUE (latitud, longitud, storlek)
            );

            INSERT INTO plats (latitud, longitud, storlek)
            SELECT DISTINCT latitud, longitud, storlek
            FROM parti;

            CREATE TABLE parti_new (
              id          INTEGER PRIMARY KEY,
              plats_id    INTEGER NOT NULL REFERENCES plats(id),
              rotation    INTEGER NOT NULL DEFAULT 0
                          CHECK (rotation IN (0, 1, 2, 3)),
              vit_id      INTEGER NOT NULL REFERENCES spelare(id),
              svart_id    INTEGER NOT NULL REFERENCES spelare(id),
              inkrement   INTEGER NOT NULL DEFAULT 0 CHECK (inkrement >= 0),
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

            INSERT INTO parti_new (
              id, plats_id, rotation, vit_id, svart_id,
              inkrement, vit_tid, svart_tid, status
            )
            SELECT
              parti.id,
              plats.id,
              CAST(((parti.rotation + 45) / 90) AS INTEGER) % 4,
              parti.vit_id,
              parti.svart_id,
              parti.inkrement,
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
              plats_id    INTEGER NOT NULL REFERENCES plats(id),
              rotation    INTEGER NOT NULL DEFAULT 0
                          CHECK (rotation IN (0, 1, 2, 3)),
              vit_id      INTEGER NOT NULL REFERENCES spelare(id),
              svart_id    INTEGER NOT NULL REFERENCES spelare(id),
              inkrement   INTEGER NOT NULL DEFAULT 0 CHECK (inkrement >= 0),
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
              id, plats_id, rotation, vit_id, svart_id, inkrement,
              vit_tid, svart_tid, senast_startad, status
            )
            SELECT
              id, plats_id, rotation, vit_id, svart_id, inkrement,
              CAST(vit_tid AS REAL), CAST(svart_tid AS REAL),
              CAST(senast_startad AS REAL), status
            FROM parti;

            DROP TABLE parti;
            ALTER TABLE parti_real_tid RENAME TO parti;

            COMMIT;
            """
        )


init_db()


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
                parti.svart_tid
            FROM parti
            JOIN spelare AS vit ON vit.id = parti.vit_id
            JOIN spelare AS svart ON svart.id = parti.svart_id
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
        board.push(move)
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

    if get_game(game_id, player_id) is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    game_connections[game_id].add(websocket)
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
                await websocket.send_json(
                    {"type": "error", "message": "Ogiltigt dragformat."}
                )
                continue
            try:
                async with game_locks[game_id]:
                    state = save_move(game_id, player_id, source, target)
                await broadcast_game(game_id, state)
            except ValueError as error:
                await websocket.send_json(
                    {"type": "error", "message": str(error)}
                )
                await websocket.send_json(game_state(game_id))
    except WebSocketDisconnect:
        pass
    finally:
        game_connections[game_id].discard(websocket)


app.router.routes.append(WebSocketRoute("/ws", game_websocket))


def format_clock(seconds):
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def get_database_tables():
    with sqlite3.connect(DB_PATH) as db:
        table_names = [
            row[0]
            for row in db.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]

        tables = []
        for table_name in table_names:
            quoted_name = '"' + table_name.replace('"', '""') + '"'
            cursor = db.execute(f"SELECT * FROM {quoted_name}")
            columns = [column[0] for column in cursor.description]
            tables.append((table_name, columns, cursor.fetchall()))
        return tables


def admin_allowed(session):
    return session.get("admin") is True


def admin_connection():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def admin_redirect(section="oversikt"):
    return RedirectResponse(f"/admin#{section}", status_code=303)


def admin_style():
    return Style(
        """
        main { max-width: 70rem; margin: 1rem auto; padding: 0 1rem; }
        #admin-nav { display: grid; gap: .35rem; max-width: 24rem; margin-bottom: 1.5rem; }
        #admin-nav select { font-size: 1.1rem; }
        .admin-section[hidden] { display: none; }
        form { display: flex; flex-wrap: wrap; align-items: end; gap: .65rem; margin: .75rem 0; }
        input, select, button { padding: .5rem; }
        table { border-collapse: collapse; width: 100%; margin-bottom: 2rem; }
        th, td { border: 1px solid #aaa; padding: .4rem; text-align: left; }
        .record { background: #f7f7f7; border: 1px solid #ddd; border-radius: .4rem; padding: .75rem; }
        .admin-section > form:first-of-type { border: 2px solid #8cb4d8; border-radius: .4rem; padding: .75rem; }
        .row-action { display: inline; margin: 0; }
        .row-action input { padding: .25rem .4rem; }
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
        return admin_redirect()
    return Title("Admin"), Main(H1("Inloggningen misslyckades"), A("Försök igen", href="/admin/login"))


@rt("/admin")
def get(session, edit_spelare: int = 0, edit_plats: int = 0, edit_parti: int = 0):
    if not admin_allowed(session):
        return RedirectResponse("/admin/login", status_code=303)
    with admin_connection() as db:
        players = db.execute("SELECT * FROM spelare ORDER BY namn").fetchall()
        locations = db.execute("SELECT * FROM plats ORDER BY id").fetchall()
        games = db.execute(
            """
            SELECT parti.*, vit.namn AS vit_namn, svart.namn AS svart_namn
            FROM parti
            JOIN spelare vit ON vit.id=parti.vit_id
            JOIN spelare svart ON svart.id=parti.svart_id
            ORDER BY parti.id DESC
            """
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
            Input(type="number", step="any", name="latitud", value=p["latitud"], required=True),
            Input(type="number", step="any", name="longitud", value=p["longitud"], required=True),
            Input(type="number", step="any", name="storlek", value=p["storlek"], required=True),
            Input(type="submit", value="Spara"),
            Input(type="submit", value="Ta bort", formaction="/admin/plats/delete"),
            method="post", action="/admin/plats/save", cls="record",
        ) for p in locations
    ]
    player_options = lambda selected: [Option(p["namn"], value=p["id"], selected=p["id"] == selected) for p in players]
    location_options = lambda selected: [Option(f'{p["id"]}: {p["latitud"]}, {p["longitud"]}', value=p["id"], selected=p["id"] == selected) for p in locations]
    game_forms = [
        Form(
            Input(type="hidden", name="id", value=g["id"]),
            Select(*location_options(g["plats_id"]), name="plats_id"),
            Select(*(Option(str(x), value=x, selected=x == g["rotation"]) for x in range(4)), name="rotation"),
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
        Thead(Tr(Th("ID"), Th("Namn"), Th("Telefon"), Th("E-post"), Th("Kommandon"))),
        Tbody(*(Tr(
            Td(p["id"]), Td(p["namn"]), Td(p["telefon"]), Td(p["mail"]),
            Td(A("Redigera", href=f'/admin?edit_spelare={p["id"]}#spelare'), " ", Form(
                Input(type="hidden", name="id", value=p["id"]), Input(type="submit", value="Ta bort"),
                method="post", action="/admin/spelare/delete", cls="row-action"))
        ) for p in players)),
    )
    location_editor = Form(
        Input(type="hidden", name="id", value=selected_location["id"] if selected_location else 0),
        Label("Latitud", Input(type="number", step="any", name="latitud", value=selected_location["latitud"] if selected_location else "", required=True)),
        Label("Longitud", Input(type="number", step="any", name="longitud", value=selected_location["longitud"] if selected_location else "", required=True)),
        Label("Storlek", Input(type="number", step="any", name="storlek", value=selected_location["storlek"] if selected_location else 800, required=True)),
        Input(type="submit", value="Spara ändringar" if selected_location else "Lägg till"), method="post", action="/admin/plats/save",
    )
    location_table = Table(
        Thead(Tr(Th("ID"), Th("Latitud"), Th("Longitud"), Th("Storlek"), Th("Kommandon"))),
        Tbody(*(Tr(Td(p["id"]), Td(p["latitud"]), Td(p["longitud"]), Td(p["storlek"]), Td(
            A("Redigera", href=f'/admin?edit_plats={p["id"]}#platser'), " ", Form(
                Input(type="hidden", name="id", value=p["id"]), Input(type="submit", value="Ta bort"),
                method="post", action="/admin/plats/delete", cls="row-action"))) for p in locations)),
    )
    sg = selected_game
    game_editor = Form(
        Input(type="hidden", name="id", value=sg["id"] if sg else 0),
        Label("Plats", Select(*location_options(sg["plats_id"] if sg else None), name="plats_id")),
        Label("Rotation", Select(*(Option(str(x), value=x, selected=bool(sg and x == sg["rotation"])) for x in range(4)), name="rotation")),
        Label("Vit", Select(*player_options(sg["vit_id"] if sg else None), name="vit_id")),
        Label("Svart", Select(*player_options(sg["svart_id"] if sg else None), name="svart_id")),
        Label("Inkrement", Input(type="number", name="inkrement", value=sg["inkrement"] if sg else 0, min=0)),
        Label("Vit tid", Input(type="number", step="any", name="vit_tid", value=sg["vit_tid"] if sg else 5400, min=0)),
        Label("Svart tid", Input(type="number", step="any", name="svart_tid", value=sg["svart_tid"] if sg else 5400, min=0)),
        Label("Status", Select(*(Option(x, selected=bool(sg and x == sg["status"])) for x in ("pågår", "remi", "vit vinst", "svart vinst")), name="status")),
        Input(type="submit", value="Spara ändringar" if sg else "Lägg till"), method="post", action="/admin/parti/save",
    )
    game_table = Table(
        Thead(Tr(Th("ID"), Th("Plats"), Th("Vit"), Th("Svart"), Th("Status"), Th("Kommandon"))),
        Tbody(*(Tr(Td(g["id"]), Td(g["plats_id"]), Td(g["vit_namn"]), Td(g["svart_namn"]), Td(g["status"]), Td(
            A("Drag", href=f'/admin/parti/{g["id"]}'), " · ",
            A("Redigera", href=f'/admin?edit_parti={g["id"]}#partier'), " ", Form(
                Input(type="hidden", name="id", value=g["id"]), Input(type="submit", value="Ta bort"),
                method="post", action="/admin/parti/delete", cls="row-action"))) for g in games)),
    )
    return Title("Admin"), admin_style(), Script(
        """
        document.addEventListener("DOMContentLoaded", () => {
            const selector = document.getElementById("admin-table");
            const sections = document.querySelectorAll(".admin-section");
            function showSection(name) {
                sections.forEach((section) => {
                    section.hidden = section.dataset.section !== name;
                });
                selector.value = name;
                history.replaceState(null, "", `#${name}`);
            }
            selector.addEventListener("change", () => showSection(selector.value));
            const initial = location.hash.slice(1);
            showSection([...selector.options].some((o) => o.value === initial) ? initial : "oversikt");
        });
        """
    ), Main(
        H1("Administration"),
        Label("Välj tabell", Select(Option("Översikt", value="oversikt"), Option("Spelare", value="spelare"), Option("Platser", value="platser"), Option("Partier", value="partier"), id="admin-table"), id="admin-nav"),
        Div(H2("Pågående partier"), *(P(A(f'Parti {g["id"]}: {g["vit_namn"]}–{g["svart_namn"]}', href=f'/admin/parti/{g["id"]}')) for g in games if g["status"] == "pågår"), cls="admin-section", data_section="oversikt"),
        Div(H2("Spelare"), player_editor, player_table, cls="admin-section", data_section="spelare"),
        Div(H2("Platser"), location_editor, location_table, cls="admin-section", data_section="platser"),
        Div(H2("Partier"), game_editor, game_table, cls="admin-section", data_section="partier"),
    )


def require_admin(session):
    return admin_allowed(session) or RedirectResponse("/admin/login", status_code=303)


@rt("/admin/spelare/save")
def post(session, namn: str, telefon: str, mail: str, id: int = 0):
    if not admin_allowed(session): return RedirectResponse("/admin/login", status_code=303)
    with admin_connection() as db:
        if id: db.execute("UPDATE spelare SET namn=?,telefon=?,mail=? WHERE id=?", (namn, telefon, mail, id))
        else: db.execute("INSERT INTO spelare(namn,telefon,mail) VALUES(?,?,?)", (namn, telefon, mail))
    return admin_redirect("spelare")


@rt("/admin/spelare/delete")
def post(session, id: int):
    if not admin_allowed(session): return RedirectResponse("/admin/login", status_code=303)
    with admin_connection() as db: db.execute("DELETE FROM spelare WHERE id=?", (id,))
    return admin_redirect("spelare")


@rt("/admin/plats/save")
def post(session, latitud: float, longitud: float, storlek: float, id: int = 0):
    if not admin_allowed(session): return RedirectResponse("/admin/login", status_code=303)
    with admin_connection() as db:
        if id: db.execute("UPDATE plats SET latitud=?,longitud=?,storlek=? WHERE id=?", (latitud, longitud, storlek, id))
        else: db.execute("INSERT INTO plats(latitud,longitud,storlek) VALUES(?,?,?)", (latitud, longitud, storlek))
    return admin_redirect("platser")


@rt("/admin/plats/delete")
def post(session, id: int):
    if not admin_allowed(session): return RedirectResponse("/admin/login", status_code=303)
    with admin_connection() as db: db.execute("DELETE FROM plats WHERE id=?", (id,))
    return admin_redirect("platser")


@rt("/admin/parti/save")
def post(session, plats_id: int, rotation: int, vit_id: int, svart_id: int, inkrement: int, vit_tid: float, svart_tid: float, status: str, id: int = 0):
    if not admin_allowed(session): return RedirectResponse("/admin/login", status_code=303)
    with admin_connection() as db:
        values = (plats_id, rotation, vit_id, svart_id, inkrement, vit_tid, svart_tid, time.time(), status)
        if id: db.execute("UPDATE parti SET plats_id=?,rotation=?,vit_id=?,svart_id=?,inkrement=?,vit_tid=?,svart_tid=?,senast_startad=?,status=? WHERE id=?", values + (id,))
        else: db.execute("INSERT INTO parti(plats_id,rotation,vit_id,svart_id,inkrement,vit_tid,svart_tid,senast_startad,status) VALUES(?,?,?,?,?,?,?,?,?)", values)
    return admin_redirect("partier")


@rt("/admin/parti/delete")
def post(session, id: int):
    if not admin_allowed(session): return RedirectResponse("/admin/login", status_code=303)
    with admin_connection() as db: db.execute("DELETE FROM parti WHERE id=?", (id,))
    return admin_redirect("partier")


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
            let board;
            let socket;

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
                if (!socket || socket.readyState !== WebSocket.OPEN) return false;
                if (game.game_over()) return false;
                if (game.turn() !== playerColor) return false;
                if (
                    (game.turn() === "w" && piece.startsWith("b")) ||
                    (game.turn() === "b" && piece.startsWith("w"))
                ) return false;
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
                socket.send(JSON.stringify({
                    type: "move",
                    from: source,
                    to: target
                }));
                return true;
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
                id="chessboard",
                data_parti=str(parti),
                data_spelare=str(spelare),
                data_color="w" if spelare == game["vit_id"] else "b",
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
            P(id="chess-status", aria_live="polite"),
        ),
    )


@rt("/db")
def db_view():
    sections = []
    for table_name, columns, rows in get_database_tables():
        body = (
            Tbody(
                *(
                    Tr(
                        *(
                            Td("" if value is None else str(value))
                            for value in row
                        )
                    )
                    for row in rows
                )
            )
            if rows
            else Tbody(Tr(Td("Tabellen är tom", colspan=str(len(columns)))))
        )
        sections.extend(
            (
                H2(table_name),
                Table(
                    Thead(Tr(*(Th(column) for column in columns))),
                    body,
                ),
            )
        )

    return (
        Title("Databas"),
        Style(
            """
            main { padding: 1rem; }
            table { border-collapse: collapse; margin-bottom: 2rem; }
            th, td {
                border: 1px solid #aaa;
                padding: .4rem .6rem;
                text-align: left;
                white-space: nowrap;
            }
            th { background: #eee; }
            """
        ),
        Main(H1("Databasens innehåll"), *sections),
    )


if __name__ == "__main__":
    serve(host="127.0.0.1", port=8000, reload=True)
