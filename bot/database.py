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
            elo INTEGER NOT NULL DEFAULT 1500,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS bans (
            discord_id TEXT PRIMARY KEY,
            banned_by TEXT NOT NULL,
            reason TEXT,
            banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
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

        CREATE TABLE IF NOT EXISTS player_ratings (
            discord_id TEXT NOT NULL,
            game_mode TEXT NOT NULL,
            elo INTEGER NOT NULL DEFAULT 1500,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (discord_id, game_mode),
            FOREIGN KEY (discord_id) REFERENCES players(discord_id)
        );

        CREATE TABLE IF NOT EXISTS teams (
            player1_id TEXT NOT NULL,
            player2_id TEXT NOT NULL,
            elo INTEGER NOT NULL DEFAULT 1500,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (player1_id, player2_id),
            FOREIGN KEY (player1_id) REFERENCES players(discord_id),
            FOREIGN KEY (player2_id) REFERENCES players(discord_id)
        );
    """)

    # Migration: add columns to matches table for doubles support
    cursor.execute("PRAGMA table_info(matches)")
    columns = [row[1] for row in cursor.fetchall()]

    if "game_mode" not in columns:
        cursor.execute("ALTER TABLE matches ADD COLUMN game_mode TEXT NOT NULL DEFAULT 'singles'")
    if "player3_id" not in columns:
        cursor.execute("ALTER TABLE matches ADD COLUMN player3_id TEXT")
    if "player4_id" not in columns:
        cursor.execute("ALTER TABLE matches ADD COLUMN player4_id TEXT")
    if "player3_elo_before" not in columns:
        cursor.execute("ALTER TABLE matches ADD COLUMN player3_elo_before INTEGER")
    if "player4_elo_before" not in columns:
        cursor.execute("ALTER TABLE matches ADD COLUMN player4_elo_before INTEGER")
    if "player3_elo_after" not in columns:
        cursor.execute("ALTER TABLE matches ADD COLUMN player3_elo_after INTEGER")
    if "player4_elo_after" not in columns:
        cursor.execute("ALTER TABLE matches ADD COLUMN player4_elo_after INTEGER")

    conn.commit()
    conn.close()


def _sort_team_ids(p1_id: str, p2_id: str) -> tuple[str, str]:
    return (min(p1_id, p2_id), max(p1_id, p2_id))


def get_or_create_player(discord_id: str, username: str) -> dict:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM players WHERE discord_id = ?", (discord_id,))
    player = cursor.fetchone()
    if player is None:
        cursor.execute(
            "INSERT INTO players (discord_id, username, elo) VALUES (?, ?, 1500)",
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
        """INSERT INTO matches (player1_id, player2_id, player1_elo_before, player2_elo_before, game_mode)
           VALUES (?, ?, ?, ?, 'singles')""",
        (player1_id, player2_id, p1_elo, p2_elo),
    )
    conn.commit()
    match_id = cursor.lastrowid
    conn.close()
    return match_id


def create_doubles_match(t1_p1_id: str, t1_p2_id: str, t2_p1_id: str, t2_p2_id: str,
                          t1_p1_elo: int, t1_p2_elo: int, t2_p1_elo: int, t2_p2_elo: int) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO matches (player1_id, player2_id, player3_id, player4_id,
               player1_elo_before, player2_elo_before, player3_elo_before, player4_elo_before,
               game_mode)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'doubles')""",
        (t1_p1_id, t1_p2_id, t2_p1_id, t2_p2_id,
         t1_p1_elo, t1_p2_elo, t2_p1_elo, t2_p2_elo),
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


def complete_doubles_match(match_id: int, winner_id: str,
                            p1_after: int, p2_after: int, p3_after: int, p4_after: int):
    conn = get_connection()
    conn.execute(
        """UPDATE matches
           SET winner_id = ?, player1_elo_after = ?, player2_elo_after = ?,
               player3_elo_after = ?, player4_elo_after = ?,
               completed_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (winner_id, p1_after, p2_after, p3_after, p4_after, match_id),
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


def get_or_create_doubles_rating(discord_id: str, username: str) -> dict:
    get_or_create_player(discord_id, username)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM player_ratings WHERE discord_id = ? AND game_mode = 'doubles'",
        (discord_id,),
    )
    row = cursor.fetchone()
    if row is None:
        cursor.execute(
            "INSERT INTO player_ratings (discord_id, game_mode, elo) VALUES (?, 'doubles', 1500)",
            (discord_id,),
        )
        conn.commit()
        cursor.execute(
            "SELECT * FROM player_ratings WHERE discord_id = ? AND game_mode = 'doubles'",
            (discord_id,),
        )
        row = cursor.fetchone()
    conn.close()
    return dict(row)


def get_doubles_rating(discord_id: str) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM player_ratings WHERE discord_id = ? AND game_mode = 'doubles'",
        (discord_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def update_doubles_stats(discord_id: str, new_elo: int, won: bool):
    conn = get_connection()
    if won:
        conn.execute(
            "UPDATE player_ratings SET elo = ?, wins = wins + 1 WHERE discord_id = ? AND game_mode = 'doubles'",
            (new_elo, discord_id),
        )
    else:
        conn.execute(
            "UPDATE player_ratings SET elo = ?, losses = losses + 1 WHERE discord_id = ? AND game_mode = 'doubles'",
            (new_elo, discord_id),
        )
    conn.commit()
    conn.close()


def get_or_create_team(p1_id: str, p2_id: str) -> dict:
    p1, p2 = _sort_team_ids(p1_id, p2_id)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM teams WHERE player1_id = ? AND player2_id = ?", (p1, p2),
    )
    row = cursor.fetchone()
    if row is None:
        cursor.execute(
            "INSERT INTO teams (player1_id, player2_id, elo) VALUES (?, ?, 1500)",
            (p1, p2),
        )
        conn.commit()
        cursor.execute(
            "SELECT * FROM teams WHERE player1_id = ? AND player2_id = ?", (p1, p2),
        )
        row = cursor.fetchone()
    conn.close()
    return dict(row)


def get_team(p1_id: str, p2_id: str) -> dict | None:
    p1, p2 = _sort_team_ids(p1_id, p2_id)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM teams WHERE player1_id = ? AND player2_id = ?", (p1, p2),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def update_team_stats(p1_id: str, p2_id: str, new_elo: int, won: bool):
    p1, p2 = _sort_team_ids(p1_id, p2_id)
    conn = get_connection()
    if won:
        conn.execute(
            "UPDATE teams SET elo = ?, wins = wins + 1 WHERE player1_id = ? AND player2_id = ?",
            (new_elo, p1, p2),
        )
    else:
        conn.execute(
            "UPDATE teams SET elo = ?, losses = losses + 1 WHERE player1_id = ? AND player2_id = ?",
            (new_elo, p1, p2),
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


def get_doubles_leaderboard(limit: int = 10) -> list[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT pr.*, p.username FROM player_ratings pr
           JOIN players p ON pr.discord_id = p.discord_id
           WHERE pr.game_mode = 'doubles'
           ORDER BY pr.elo DESC LIMIT ?""",
        (limit,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_team_leaderboard(limit: int = 10) -> list[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT t.*, p1.username as player1_username, p2.username as player2_username
           FROM teams t
           JOIN players p1 ON t.player1_id = p1.discord_id
           JOIN players p2 ON t.player2_id = p2.discord_id
           ORDER BY t.elo DESC LIMIT ?""",
        (limit,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_player_teams(discord_id: str, limit: int = 5) -> list[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT t.*, p1.username as player1_username, p2.username as player2_username
           FROM teams t
           JOIN players p1 ON t.player1_id = p1.discord_id
           JOIN players p2 ON t.player2_id = p2.discord_id
           WHERE t.player1_id = ? OR t.player2_id = ?
           ORDER BY t.elo DESC LIMIT ?""",
        (discord_id, discord_id, limit),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_player_rank(discord_id: str) -> tuple[int, int] | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM players")
    total = cursor.fetchone()[0]
    cursor.execute(
        "SELECT COUNT(*) FROM players AS p2 WHERE p2.elo > (SELECT elo FROM players WHERE discord_id = ?)",
        (discord_id,),
    )
    above = cursor.fetchone()[0]
    conn.close()
    return (above + 1, total)


def get_doubles_player_rank(discord_id: str) -> tuple[int, int] | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM player_ratings WHERE game_mode = 'doubles'")
    total = cursor.fetchone()[0]
    if total == 0:
        conn.close()
        return None
    cursor.execute(
        """SELECT COUNT(*) FROM player_ratings
           WHERE game_mode = 'doubles' AND elo > (
               SELECT elo FROM player_ratings WHERE discord_id = ? AND game_mode = 'doubles'
           )""",
        (discord_id,),
    )
    result = cursor.fetchone()
    conn.close()
    if result is None:
        return None
    return (result[0] + 1, total)


def get_team_rank(p1_id: str, p2_id: str) -> tuple[int, int] | None:
    p1, p2 = _sort_team_ids(p1_id, p2_id)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM teams")
    total = cursor.fetchone()[0]
    if total == 0:
        conn.close()
        return None
    cursor.execute(
        """SELECT COUNT(*) FROM teams WHERE elo > (
               SELECT elo FROM teams WHERE player1_id = ? AND player2_id = ?
           )""",
        (p1, p2),
    )
    result = cursor.fetchone()
    conn.close()
    if result is None:
        return None
    return (result[0] + 1, total)


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
    conn.execute("UPDATE players SET elo = 1500, wins = 0, losses = 0")
    conn.execute("DELETE FROM player_ratings")
    conn.execute("DELETE FROM teams")
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    conn.close()
    return count


def reset_singles():
    conn = get_connection()
    conn.execute("UPDATE players SET elo = 1500, wins = 0, losses = 0")
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    conn.close()
    return count


def reset_doubles():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(DISTINCT discord_id) FROM player_ratings WHERE game_mode = 'doubles'")
    count = cursor.fetchone()[0]
    conn.execute("DELETE FROM player_ratings")
    conn.execute("DELETE FROM teams")
    conn.commit()
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


def set_doubles_elo(discord_id: str, elo: int) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE player_ratings SET elo = ? WHERE discord_id = ? AND game_mode = 'doubles'",
        (elo, discord_id),
    )
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def set_team_elo(p1_id: str, p2_id: str, elo: int) -> bool:
    p1, p2 = _sort_team_ids(p1_id, p2_id)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE teams SET elo = ? WHERE player1_id = ? AND player2_id = ?",
        (elo, p1, p2),
    )
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


def get_match_by_thread(thread_id: str) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM matches WHERE thread_id = ? AND winner_id IS NULL",
        (thread_id,),
    )
    match = cursor.fetchone()
    conn.close()
    return dict(match) if match else None


def get_match_by_id(match_id: int) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM matches WHERE id = ? AND winner_id IS NULL",
        (match_id,),
    )
    match = cursor.fetchone()
    conn.close()
    return dict(match) if match else None


def get_pending_match(player_id: str) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT * FROM matches
           WHERE (player1_id = ? OR player2_id = ? OR player3_id = ? OR player4_id = ?)
           AND winner_id IS NULL
           ORDER BY created_at DESC LIMIT 1""",
        (player_id, player_id, player_id, player_id),
    )
    match = cursor.fetchone()
    conn.close()
    return dict(match) if match else None


def get_setting(key: str, default: str = None) -> str | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row["value"] if row else default


def get_expired_matches(minutes: int = 30) -> list[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT * FROM matches
           WHERE winner_id IS NULL
           AND created_at <= datetime('now', ?)""",
        (f"-{minutes} minutes",),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_setting(key: str, value: str):
    conn = get_connection()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
        (key, value, value),
    )
    conn.commit()
    conn.close()
