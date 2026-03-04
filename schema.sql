CREATE TABLE IF NOT EXISTS current (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    index_value REAL NOT NULL,
    timestamp TEXT NOT NULL,
    num_constituents INTEGER NOT NULL,
    weighted_entropy REAL NOT NULL,
    divisor REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS constituents (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    source_type TEXT NOT NULL,
    num_outcomes INTEGER NOT NULL,
    probabilities TEXT NOT NULL,  -- JSON array
    normalized_entropy REAL NOT NULL,
    weight REAL NOT NULL,
    volume_1mo REAL NOT NULL,
    end_date TEXT,
    rank INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS history (
    timestamp TEXT PRIMARY KEY,
    value REAL NOT NULL,
    num_constituents INTEGER NOT NULL,
    weighted_entropy REAL NOT NULL,
    divisor REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event TEXT NOT NULL,
    data TEXT NOT NULL  -- full JSON blob
);

CREATE TABLE IF NOT EXISTS state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL  -- full JSON blob for engine resumption
);
