import random
import time

import discord

from database import get_or_create_player, create_match, get_pending_match

REACT_P1 = "\U0001f534"  # Red circle for player 1
REACT_P2 = "\U0001f535"  # Blue circle for player 2

COURT_TYPES = ("Grass", "Hard", "Clay", "Wood", "Brick", "Carpet", "Sand", "Forest")

REMATCH_COOLDOWN = 60  # seconds before same pair can be matched again


class MatchmakingQueue:
    def __init__(self):
        # dict of discord_id -> {discord_id, username, elo}
        self.queue: dict[str, dict] = {}
        # Track recent matches: {frozenset(p1_id, p2_id): timestamp}
        self.recent_matches: dict[frozenset[str], float] = {}

    def record_match(self, p1_id: str, p2_id: str):
        self.recent_matches[frozenset((p1_id, p2_id))] = time.time()

    def _is_on_cooldown(self, p1_id: str, p2_id: str) -> bool:
        pair = frozenset((p1_id, p2_id))
        last_time = self.recent_matches.get(pair)
        if last_time is None:
            return False
        if time.time() - last_time < REMATCH_COOLDOWN:
            return True
        # Cooldown expired, clean up
        del self.recent_matches[pair]
        return False

    def add_player(self, discord_id: str, username: str) -> dict:
        player = get_or_create_player(discord_id, username)
        self.queue[discord_id] = player
        return player

    def remove_player(self, discord_id: str) -> bool:
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

        # Build sorted candidate pairs by Elo difference
        candidates = []
        for i in range(len(players) - 1):
            diff = abs(players[i]["elo"] - players[i + 1]["elo"])
            candidates.append((diff, players[i], players[i + 1]))
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

        # Remove from queue
        self.queue.pop(p1["discord_id"], None)
        self.queue.pop(p2["discord_id"], None)

        match_id = create_match(
            p1["discord_id"], p2["discord_id"], p1["elo"], p2["elo"]
        )

        return p1, p2, match_id


def pick_court() -> str:
    return random.choice(COURT_TYPES)


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
        value=f"**Court:** {court}\n**Ball Speed:** High\n**Mode:** Classic",
        inline=False,
    )
    embed.set_footer(
        text="React with the winner's icon to report the result.\n"
        "Both players must agree. Conflicting votes = dispute."
    )
    return embed
