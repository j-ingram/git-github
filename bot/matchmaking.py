import random
import time

import discord

from database import get_or_create_player, create_match, get_pending_match, get_setting, set_setting

REACT_P1 = "\U0001f344"  # 🍄 for player 1
REACT_P2 = "\u2b50"     # ⭐ for player 2

# All valid Mario Tennis courts
ALL_COURTS = (
    "Grass", "Hard", "Clay", "Wood", "Brick", "Carpet",
    "Mushroom", "Sand", "Ice", "Airship", "Forest", "Pinball",
    "Factory", "Wonder",
)

DEFAULT_ENABLED_COURTS = "Grass,Hard,Clay,Wood,Brick,Carpet,Sand,Forest"

DEFAULT_REMATCH_COOLDOWN = 60  # default seconds before same pair can be matched again


def get_rematch_cooldown() -> int:
    return int(get_setting("rematch_cooldown", str(DEFAULT_REMATCH_COOLDOWN)))


def get_enabled_courts() -> list[str]:
    courts_str = get_setting("enabled_courts", DEFAULT_ENABLED_COURTS)
    return [c.strip() for c in courts_str.split(",") if c.strip()]


def set_enabled_courts(courts: list[str]):
    set_setting("enabled_courts", ",".join(courts))


class MatchmakingQueue:
    def __init__(self):
        # dict of discord_id -> {discord_id, username, elo}
        self.queue: dict[str, dict] = {}
        # Track each player's last opponent: {player_id: opponent_id}
        self.last_opponents: dict[str, str] = {}
        # Track when each player joined the queue: {player_id: timestamp}
        self.join_times: dict[str, float] = {}

    def record_match(self, p1_id: str, p2_id: str):
        """Record that these two players just played each other."""
        self.last_opponents[p1_id] = p2_id
        self.last_opponents[p2_id] = p1_id

    def _is_on_cooldown(self, p1_id: str, p2_id: str) -> bool:
        """Check if this pair were last opponents and haven't waited long enough in queue."""
        if self.last_opponents.get(p1_id) != p2_id:
            return False
        # They were last opponents — check if the later joiner has waited 60s
        p1_join = self.join_times.get(p1_id, 0)
        p2_join = self.join_times.get(p2_id, 0)
        later_join = max(p1_join, p2_join)
        return time.time() - later_join < get_rematch_cooldown()

    def add_player(self, discord_id: str, username: str) -> dict:
        player = get_or_create_player(discord_id, username)
        self.queue[discord_id] = player
        self.join_times[discord_id] = time.time()
        return player

    def remove_player(self, discord_id: str) -> bool:
        self.join_times.pop(discord_id, None)
        return self.queue.pop(discord_id, None) is not None

    def is_in_queue(self, discord_id: str) -> bool:
        return discord_id in self.queue

    def queue_size(self) -> int:
        return len(self.queue)

    def find_match(self) -> tuple[dict, dict, int] | None:
        """Find the best match in the queue (two players with closest Elo).

        Skips pairs that have played each other within the cooldown period.
        Returns (player1, player2, match_id) or None if no valid match exists.
        """
        if len(self.queue) < 2:
            return None

        players = list(self.queue.values())
        players.sort(key=lambda p: p["elo"])

        # Build ALL candidate pairs sorted by Elo difference
        candidates = []
        for i in range(len(players)):
            for j in range(i + 1, len(players)):
                diff = abs(players[i]["elo"] - players[j]["elo"])
                candidates.append((diff, players[i], players[j]))
        candidates.sort(key=lambda c: c[0])

        # Find the best pair that isn't on cooldown
        best_pair = None
        for diff, a, b in candidates:
            if not self._is_on_cooldown(a["discord_id"], b["discord_id"]):
                best_pair = (a, b)
                break

        if best_pair is None:
            return None

        p1, p2 = best_pair

        # Check neither player has an unfinished match
        if get_pending_match(p1["discord_id"]) or get_pending_match(p2["discord_id"]):
            return None

        # Remove from queue and clean up join times
        self.queue.pop(p1["discord_id"], None)
        self.queue.pop(p2["discord_id"], None)
        self.join_times.pop(p1["discord_id"], None)
        self.join_times.pop(p2["discord_id"], None)

        match_id = create_match(
            p1["discord_id"], p2["discord_id"], p1["elo"], p2["elo"]
        )

        return p1, p2, match_id


def pick_court() -> str:
    courts = get_enabled_courts()
    return random.choice(courts)


def get_match_length() -> str:
    return get_setting("match_length", "Quick Play")


def build_match_embed(p1: dict, p2: dict, match_id: int, court: str) -> discord.Embed:
    embed = discord.Embed(
        title="Match Found!",
        description=f"**Match #{match_id}**",
        color=discord.Color.green(),
    )
    embed.add_field(
        name=f"{REACT_P1} {p1['username']}",
        value=f"Elo: {p1['elo']}",
        inline=True,
    )
    embed.add_field(name="vs", value="\u200b", inline=True)
    embed.add_field(
        name=f"{REACT_P2} {p2['username']}",
        value=f"Elo: {p2['elo']}",
        inline=True,
    )
    embed.add_field(
        name="Match Settings",
        value=f"**Court:** {court}\n**Ball Speed:** High\n**Mode:** Classic\n**Match Length:** {get_match_length()}",
        inline=False,
    )
    embed.set_footer(
        text="React with the winner's icon to report the result.\n"
        "Both players must agree. Conflicting votes = dispute."
    )
    return embed
