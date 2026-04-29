import random
import time
from itertools import combinations

import discord

from database import (
    get_or_create_player, create_match, get_pending_match, get_setting, set_setting,
    create_doubles_match, get_or_create_doubles_rating, get_or_create_team,
)

REACT_P1 = "\U0001f344"  # 🍄 for player 1 / team 1
REACT_P2 = "⭐"     # ⭐ for player 2 / team 2
REACT_ACCEPT = "✅"  # ✅
REACT_DECLINE = "❌"  # ❌

# All valid Mario Tennis courts
ALL_COURTS = (
    "Grass", "Hard", "Clay", "Wood", "Brick", "Carpet",
    "Mushroom", "Sand", "Ice", "Airship", "Forest", "Pinball",
    "Factory", "Wonder",
)

DEFAULT_ENABLED_COURTS = "Grass,Hard,Clay,Wood,Brick,Carpet,Sand,Forest"

ALL_CHARACTERS = (
    "Mario", "Luigi", "Peach", "Daisy", "Rosalina", "Pauline",
    "Wario", "Waluigi", "Toad", "Toadette", "Luma", "Yoshi",
    "Bowser", "Bowser Jr.", "Donkey Kong", "Boo", "Shy Guy",
    "Koopa Troopa", "Kamek", "Spike", "Diddy Kong", "Chain Chomp",
    "Birdo", "Koopa Paratroopa", "Petey Piranha", "Piranha Plant",
    "Boom Boom", "Blooper", "Dry Bowser", "Dry Bones", "Baby Mario",
    "Baby Luigi", "Baby Peach", "Wiggler", "Nabbit", "Goomba",
    "Baby Wario", "Baby Waluigi",
)

DEFAULT_REMATCH_COOLDOWN = 60
DEFAULT_QUEUE_TIMEOUT = 60
DEFAULT_INVITE_TIMEOUT = 600  # 10 minutes


def get_rematch_cooldown() -> int:
    return int(get_setting("rematch_cooldown", str(DEFAULT_REMATCH_COOLDOWN)))


def get_queue_timeout() -> int:
    return int(get_setting("queue_timeout", str(DEFAULT_QUEUE_TIMEOUT)))


def get_invite_timeout() -> int:
    return int(get_setting("invite_timeout", str(DEFAULT_INVITE_TIMEOUT)))


def get_enabled_courts() -> list[str]:
    courts_str = get_setting("enabled_courts", DEFAULT_ENABLED_COURTS)
    return [c.strip() for c in courts_str.split(",") if c.strip()]


def set_enabled_courts(courts: list[str]):
    set_setting("enabled_courts", ",".join(courts))


def get_banned_characters() -> list[str]:
    banned_str = get_setting("banned_characters", "")
    if not banned_str:
        return []
    return [c.strip() for c in banned_str.split(",") if c.strip()]


def set_banned_characters(characters: list[str]):
    set_setting("banned_characters", ",".join(characters))


def get_doubles_banned_characters() -> list[str]:
    banned_str = get_setting("doubles_banned_characters", "")
    if not banned_str:
        return []
    return [c.strip() for c in banned_str.split(",") if c.strip()]


def set_doubles_banned_characters(characters: list[str]):
    set_setting("doubles_banned_characters", ",".join(characters))


class MatchmakingQueue:
    def __init__(self):
        self.queue: dict[str, dict] = {}
        self.last_opponents: dict[str, str] = {}
        self.join_times: dict[str, float] = {}

    def record_match(self, p1_id: str, p2_id: str):
        self.last_opponents[p1_id] = p2_id
        self.last_opponents[p2_id] = p1_id

    def _is_on_cooldown(self, p1_id: str, p2_id: str) -> bool:
        if self.last_opponents.get(p1_id) != p2_id or self.last_opponents.get(p2_id) != p1_id:
            return False
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
        if len(self.queue) < 2:
            return None

        players = list(self.queue.values())
        players.sort(key=lambda p: p["elo"])

        candidates = []
        for i in range(len(players)):
            for j in range(i + 1, len(players)):
                diff = abs(players[i]["elo"] - players[j]["elo"])
                candidates.append((diff, players[i], players[j]))
        candidates.sort(key=lambda c: c[0])

        best_pair = None
        for diff, a, b in candidates:
            if not self._is_on_cooldown(a["discord_id"], b["discord_id"]):
                best_pair = (a, b)
                break

        if best_pair is None:
            return None

        p1, p2 = best_pair

        if get_pending_match(p1["discord_id"]) or get_pending_match(p2["discord_id"]):
            return None

        self.queue.pop(p1["discord_id"], None)
        self.queue.pop(p2["discord_id"], None)
        self.join_times.pop(p1["discord_id"], None)
        self.join_times.pop(p2["discord_id"], None)

        match_id = create_match(
            p1["discord_id"], p2["discord_id"], p1["elo"], p2["elo"]
        )

        return p1, p2, match_id


class DoublesQueue:
    def __init__(self):
        # Pre-formed teams: frozenset(id1, id2) -> {players: (p1, p2), team_elo: int}
        self.teams: dict[frozenset, dict] = {}
        # Solo players: discord_id -> player dict (with doubles elo in 'elo' key)
        self.solos: dict[str, dict] = {}
        self.join_times: dict[str, float] = {}
        # Recent solo teammates: discord_id -> list of partner ids (most recent last)
        self.recent_teammates: dict[str, list[str]] = {}

    def record_teammates(self, p1_id: str, p2_id: str):
        self.recent_teammates.setdefault(p1_id, []).append(p2_id)
        self.recent_teammates.setdefault(p2_id, []).append(p1_id)

    def _is_recent_teammate(self, p1_id: str, p2_id: str, pool_size: int) -> bool:
        # Each player can partner with (pool_size - 1) others.
        # They should cycle through all of them before repeating.
        max_history = pool_size - 2  # exclude self and current partner candidate
        if max_history < 1:
            return False
        history = self.recent_teammates.get(p1_id, [])[-max_history:]
        return p2_id in history

    def add_team(self, p1: dict, p2: dict, team_elo: int) -> frozenset:
        key = frozenset([p1["discord_id"], p2["discord_id"]])
        self.teams[key] = {"players": (p1, p2), "team_elo": team_elo}
        now = time.time()
        self.join_times[p1["discord_id"]] = now
        self.join_times[p2["discord_id"]] = now
        return key

    def add_solo(self, player: dict) -> dict:
        self.solos[player["discord_id"]] = player
        self.join_times[player["discord_id"]] = time.time()
        return player

    def remove_player(self, discord_id: str) -> str | None:
        """Remove a player. Returns partner's discord_id if they were in a team, None otherwise."""
        if discord_id in self.solos:
            self.solos.pop(discord_id)
            self.join_times.pop(discord_id, None)
            return None
        for key in list(self.teams.keys()):
            if discord_id in key:
                team = self.teams.pop(key)
                partner_id = None
                for p in team["players"]:
                    pid = p["discord_id"]
                    self.join_times.pop(pid, None)
                    if pid != discord_id:
                        partner_id = pid
                return partner_id
        return None

    def is_in_queue(self, discord_id: str) -> bool:
        if discord_id in self.solos:
            return True
        for key in self.teams:
            if discord_id in key:
                return True
        return False

    def queue_size(self) -> int:
        return len(self.solos) + sum(2 for _ in self.teams)

    def get_queue_entries(self) -> tuple[list[str], list[str]]:
        """Return (team_lines, solo_lines) for display."""
        team_lines = []
        for team in self.teams.values():
            p1, p2 = team["players"]
            team_lines.append(f"{p1['username']} & {p2['username']} (Team Elo: {team['team_elo']})")
        solo_lines = []
        for p in sorted(self.solos.values(), key=lambda x: x["elo"], reverse=True):
            solo_lines.append(f"{p['username']} (Doubles Elo: {p['elo']})")
        return team_lines, solo_lines

    def find_match(self) -> tuple[tuple, tuple] | None:
        """Find the best doubles match.

        Returns ((p1, p2), (p3, p4)) or None.
        Team 1 = p1 & p2, Team 2 = p3 & p4.
        """
        # Case 1: Team vs Team
        if len(self.teams) >= 2:
            team_list = list(self.teams.items())
            best_pair = None
            best_diff = float('inf')
            for i in range(len(team_list)):
                for j in range(i + 1, len(team_list)):
                    diff = abs(team_list[i][1]["team_elo"] - team_list[j][1]["team_elo"])
                    if diff < best_diff:
                        best_diff = diff
                        best_pair = (team_list[i], team_list[j])

            if best_pair:
                key1, team1 = best_pair[0]
                key2, team2 = best_pair[1]
                all_players = team1["players"] + team2["players"]
                for p in all_players:
                    if get_pending_match(p["discord_id"]):
                        return None
                self.teams.pop(key1)
                self.teams.pop(key2)
                for p in all_players:
                    self.join_times.pop(p["discord_id"], None)
                return team1["players"], team2["players"]

        # Case 2: 4+ solos → 2 balanced teams (with teammate rotation)
        if len(self.solos) >= 4:
            solo_list = sorted(self.solos.values(), key=lambda p: p["elo"])
            pool_size = len(solo_list)
            fresh_match = None
            fresh_diff = float('inf')
            fallback_match = None
            fallback_diff = float('inf')

            for group in combinations(solo_list, 4):
                players = list(group)
                splits = [
                    ((players[0], players[1]), (players[2], players[3])),
                    ((players[0], players[2]), (players[1], players[3])),
                    ((players[0], players[3]), (players[1], players[2])),
                ]
                for t1, t2 in splits:
                    t1_avg = (t1[0]["elo"] + t1[1]["elo"]) / 2
                    t2_avg = (t2[0]["elo"] + t2[1]["elo"]) / 2
                    diff = abs(t1_avg - t2_avg)

                    t1_recent = self._is_recent_teammate(t1[0]["discord_id"], t1[1]["discord_id"], pool_size)
                    t2_recent = self._is_recent_teammate(t2[0]["discord_id"], t2[1]["discord_id"], pool_size)

                    if not t1_recent and not t2_recent:
                        if diff < fresh_diff:
                            fresh_diff = diff
                            fresh_match = (t1, t2)
                    if diff < fallback_diff:
                        fallback_diff = diff
                        fallback_match = (t1, t2)

            best_match = fresh_match or fallback_match
            if best_match:
                t1, t2 = best_match
                all_players = list(t1) + list(t2)
                for p in all_players:
                    if get_pending_match(p["discord_id"]):
                        return None
                self.record_teammates(t1[0]["discord_id"], t1[1]["discord_id"])
                self.record_teammates(t2[0]["discord_id"], t2[1]["discord_id"])
                for p in all_players:
                    self.solos.pop(p["discord_id"], None)
                    self.join_times.pop(p["discord_id"], None)
                return t1, t2

        # Case 3: Team vs 2 solos
        if len(self.teams) >= 1 and len(self.solos) >= 2:
            solo_list = list(self.solos.values())
            pool_size = len(solo_list)
            fresh_match = None
            fresh_diff = float('inf')
            fallback_match = None
            fallback_diff = float('inf')

            for team_key, team in self.teams.items():
                team_elo = team["team_elo"]
                for i in range(len(solo_list)):
                    for j in range(i + 1, len(solo_list)):
                        avg = (solo_list[i]["elo"] + solo_list[j]["elo"]) / 2
                        diff = abs(team_elo - avg)
                        recent = self._is_recent_teammate(
                            solo_list[i]["discord_id"], solo_list[j]["discord_id"], pool_size
                        )
                        if not recent:
                            if diff < fresh_diff:
                                fresh_diff = diff
                                fresh_match = (team_key, team, (solo_list[i], solo_list[j]))
                        if diff < fallback_diff:
                            fallback_diff = diff
                            fallback_match = (team_key, team, (solo_list[i], solo_list[j]))

            best_match = fresh_match or fallback_match

            if best_match:
                team_key, team, solo_pair = best_match
                all_players = list(team["players"]) + list(solo_pair)
                for p in all_players:
                    if get_pending_match(p["discord_id"]):
                        return None
                self.record_teammates(solo_pair[0]["discord_id"], solo_pair[1]["discord_id"])
                self.teams.pop(team_key)
                for p in team["players"]:
                    self.join_times.pop(p["discord_id"], None)
                for p in solo_pair:
                    self.solos.pop(p["discord_id"], None)
                    self.join_times.pop(p["discord_id"], None)
                return team["players"], solo_pair

        return None


def pick_court() -> str:
    courts = get_enabled_courts()
    return random.choice(courts)


def get_match_length() -> str:
    return get_setting("match_length", "Quick Play")


def get_doubles_match_length() -> str:
    return get_setting("doubles_match_length", "Custom - 4 Games")


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
    embed.add_field(name="vs", value="​", inline=True)
    embed.add_field(
        name=f"{REACT_P2} {p2['username']}",
        value=f"Elo: {p2['elo']}",
        inline=True,
    )
    settings_text = f"**Court:** {court}\n**Ball Speed:** High\n**Mode:** Classic\n**Match Length:** {get_match_length()}"
    banned = get_banned_characters()
    if banned:
        settings_text += f"\n**Banned Characters:** {', '.join(banned)}"
    embed.add_field(
        name="Match Settings",
        value=settings_text,
        inline=False,
    )
    embed.set_footer(
        text="React with the winner's icon to report the result.\n"
        "Both players must agree. Conflicting votes = dispute."
    )
    return embed


def build_doubles_match_embed(team1: tuple, team2: tuple, match_id: int, court: str,
                               t1_p1_elo: int, t1_p2_elo: int,
                               t2_p1_elo: int, t2_p2_elo: int) -> discord.Embed:
    p1, p2 = team1
    p3, p4 = team2
    embed = discord.Embed(
        title="Doubles Match Found!",
        description=f"**Match #{match_id}**",
        color=discord.Color.green(),
    )
    embed.add_field(
        name=f"{REACT_P1} Team 1",
        value=f"{p1['username']} ({t1_p1_elo})\n{p2['username']} ({t1_p2_elo})",
        inline=True,
    )
    embed.add_field(name="vs", value="​", inline=True)
    embed.add_field(
        name=f"{REACT_P2} Team 2",
        value=f"{p3['username']} ({t2_p1_elo})\n{p4['username']} ({t2_p2_elo})",
        inline=True,
    )
    settings_text = f"**Court:** {court}\n**Ball Speed:** High\n**Mode:** Classic\n**Match Length:** {get_doubles_match_length()}"
    banned = get_doubles_banned_characters()
    if banned:
        settings_text += f"\n**Banned Characters:** {', '.join(banned)}"
    embed.add_field(
        name="Match Settings",
        value=settings_text,
        inline=False,
    )
    embed.set_footer(
        text="React with the winning team's icon to report the result.\n"
        "One player from each team must agree. Conflicting votes = dispute."
    )
    return embed
