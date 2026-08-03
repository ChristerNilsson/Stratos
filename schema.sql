PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS spelare (
  id       INTEGER PRIMARY KEY,
  namn     TEXT NOT NULL,
  telefon  TEXT NOT NULL,
  mail     TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS plats (
  id         INTEGER PRIMARY KEY,
  namn       TEXT NOT NULL,
  latitud    REAL NOT NULL,
  longitud   REAL NOT NULL,
  rotation   INTEGER NOT NULL DEFAULT 0 CHECK (rotation BETWEEN -90 AND 90),
  storlek    REAL NOT NULL DEFAULT 800 CHECK (storlek > 0),
  UNIQUE (latitud, longitud, storlek)
);

CREATE TABLE IF NOT EXISTS parti (
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
              CHECK (status IN ('pågår', 'remi', 'vit vinst', 'svart vinst')),
  CHECK (vit_id <> svart_id)
);

CREATE TABLE IF NOT EXISTS drag (
  parti_id   INTEGER NOT NULL REFERENCES parti(id) ON DELETE CASCADE,
  nummer     INTEGER NOT NULL CHECK (nummer > 0),
  franruta   TEXT NOT NULL CHECK (franruta GLOB '[a-h][1-8]'),
  tillruta   TEXT NOT NULL CHECK (tillruta GLOB '[a-h][1-8]'),
  PRIMARY KEY (parti_id, nummer)
);

CREATE INDEX IF NOT EXISTS drag_parti_idx ON drag(parti_id, nummer);

CREATE TABLE IF NOT EXISTS handelse (
  id        INTEGER PRIMARY KEY,
  timestamp TEXT NOT NULL DEFAULT (
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
  ),
  person    TEXT NOT NULL,
  text      TEXT NOT NULL
);

-- Exempeldata för det parti som visas i klienten.
INSERT OR IGNORE INTO spelare (id, namn, telefon, mail) VALUES
  (1, 'Sture Stolpe', '+46 70 123 45 67', 'sture@example.se'),
  (2, 'Örjan Kärrsgård', '+46 70 987 65 43', 'orjan@example.se');

INSERT OR IGNORE INTO plats (id, namn, latitud, longitud, storlek) VALUES
  (1, 'Skarpnäck 800', 59.26996327, 18.14979067, 800);

INSERT OR IGNORE INTO parti (
  id, plats_id, rotation,
  vit_id, svart_id, inkrement, vit_tid, svart_tid, status
) VALUES (
  1, 1, 0,
  1, 2, 30, 5400, 5400, 'pågår'
);

-- INSERT OR IGNORE INTO drag (parti_id, nummer, franruta, tillruta) VALUES
--   (1, 1, 'e2', 'e4'),
--   (1, 2, 'g8', 'f6'),
--   (1, 3, 'b1', 'c3'),
--   (1, 4, 'f6', 'e4');
