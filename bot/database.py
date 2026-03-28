import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "matchmaking.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS players (
            discord_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            elo INTEGER NOT NULL DEFAULT 1000,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS bans (
            discord_id TEXT PRIMARY KEY,
            banned_by TEXT NOT NULL,
            reason TEXT,
            banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player1_id TEXT NOT NULL,
            player2_id TEXT NOT NULL,
            winner_id TEXT,
            player1_elo_before INTEGER NOT NULL,
            player2_elo_before INTEGER NOT NULL,
            player1_elo_after INTEGER,
            player2_elo_after INTEGER,
            thread_id TEXT,
            message_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY (player1_id) REFERENCES players(discord_id),
            FOREIGN KEY (player2_id) REFERENCES players(discord_id)
        );
    """)
    conn.commit()
    conn.close()


def get_or_create_player(discord_id: str, username: str) -> dict:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM players WHERE discord_id = ?", (discord_id,))
    player = cursor.fetchone()
    if player is None:
        cursor.execute(
            "INSERT INTO players (discord_id, username, elo) VALUES (?, ?, 1000)",
            (discord_id, username),
        )
        conn.commit()
        cursor.execute("SELECT * FROM players WHERE discord_id = ?", (discord_id,))
        player = cursor.fetchone()
    conn.close()
    return dict(player)


def update_player_username(discord_id: str, username: str):
    conn = get_connection()
    conn.execute(
        "UPDATE players SET username = ? WHERE discord_id = ?", (username, discord_id)
    )
    conn.commit()
    conn.close()


def get_player(discord_id: str) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM players WHERE discord_id = ?", (discord_id,))
    player = cursor.fetchone()
    conn.close()
    return dict(player) if player else None


def create_match(player1_id: str, player2_id: str, p1_elo: int, p2_elo: int) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO matches (player1_id, player2_id, player1_elo_before, player2_elo_before)
           VALUES (?, ?, ?, ?)""",
        (player1_id, player2_id, p1_elo, p2_elo),
    )
    conn.commit()
    match_id = cursor.lastrowid
    conn.close()
    return match_id


def complete_match(match_id: int, winner_id: str, p1_elo_after: int, p2_elo_after: int):
    conn = get_connection()
    conn.execute(
        """UPDATE matches
           SET winner_id = ?, player1_elo_after = ?, player2_elo_after = ?,
               completed_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (winner_id, p1_elo_after, p2_elo_after, match_id),
    )
    conn.commit()
    conn.close()


def update_player_stats(discord_id: str, new_elo: int, won: bool):
    conn = get_connection()
    if won:
        conn.execute(
            "UPDATE players SET elo = ?, wins = wins + 1 WHERE discord_id = ?",
            (new_elo, discord_id),
        )
    else:
        conn.execute(
            "UPDATE players SET elo = ?, losses = losses + 1 WHERE discord_id = ?",
            (new_elo, discord_id),
        )
    conn.commit()
    conn.close()


def get_leaderboard(limit: int = 10) -> list[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM players ORDER BY elo DESC LIMIT ?", (limit,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_match_thread(match_id: int, thread_id: str, message_id: str):
    conn = get_connection()
    conn.execute(
        "UPDATE matches SET thread_id = ?, message_id = ? WHERE id = ?",
        (thread_id, message_id, match_id),
    )
    conn.commit()
    conn.close()


def get_match_by_message(message_id: str) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM matches WHERE message_id = ? AND winner_id IS NULL",
        (message_id,),
    )
    match = cursor.fetchone()
    conn.close()
    return dict(match) if match else None


def reset_season():
    conn = get_connection()
    conn.execute("UPDATE players SET elo = 1000, wins = 0, losses = 0")
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    conn.close()
    return count


def set_player_elo(discord_id: str, elo: int) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE players SET elo = ? WHERE discord_id = ?", (elo, discord_id))
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def ban_player(discord_id: str, banned_by: str, reason: str | None = None) -> bool:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO bans (discord_id, banned_by, reason) VALUES (?, ?, ?)",
            (discord_id, banned_by, reason),
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False


def unban_player(discord_id: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM bans WHERE discord_id = ?", (discord_id,))
    conn.commit()
    removed = cursor.rowcount > 0
    conn.close()
    return removed


def is_banned(discord_id: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM bans WHERE discord_id = ?", (discord_id,))
    result = cursor.fetchone() is not None
    conn.close()
    return result


def get_pending_match(player_id: str) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT * FROM matches
           WHERE (player1_id = ? OR player2_id = ?) AND winner_id IS NULL
           ORDER BY created_at DESC LIMIT 1""",
        (player_id, player_id),
    )
    match = cursor.fetchone()
    conn.close()
    return dict(match) if match else None
