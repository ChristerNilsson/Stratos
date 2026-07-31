import asyncio
import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import chess
from fasthtml.common import (
    Div,
    H1,
    H2,
    Link,
    Main,
    P,
    Script,
    Style,
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
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect

app, rt = fast_app()
APP_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = APP_DIR / "schema.sql"
DB_PATH = Path(os.environ.get("CHESS_DB_PATH", APP_DIR / "chess.db"))
game_connections = defaultdict(set)
game_locks = defaultdict(asyncio.Lock)


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
