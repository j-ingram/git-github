import asyncio
import os
import time

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from database import (
    init_db,
    get_or_create_player,
    get_leaderboard,
    get_doubles_leaderboard,
    get_team_leaderboard,
    get_player_teams,
    get_pending_match,
    complete_match,
    complete_doubles_match,
    create_doubles_match,
    update_player_stats,
    update_doubles_stats,
    update_team_stats,
    get_doubles_rating,
    get_or_create_doubles_rating,
    get_or_create_team,
    get_player,
    update_player_username,
    set_match_thread,
    get_match_by_message,
    get_match_by_thread,
    get_match_by_id,
    reset_season,
    reset_singles,
    reset_doubles,
    set_player_elo,
    ban_player,
    unban_player,
    is_banned,
    get_player_rank,
    get_doubles_player_rank,
    get_team_rank,
    set_setting,
    get_setting,
    get_expired_matches,
)
from elo import calculate_new_ratings, expected_score, get_k_factor
from matchmaking import (
    MatchmakingQueue, DoublesQueue,
    build_match_embed, build_doubles_match_embed,
    pick_court, REACT_P1, REACT_P2, REACT_ACCEPT, REACT_DECLINE,
    ALL_COURTS, get_enabled_courts, set_enabled_courts, get_match_length,
    ALL_CHARACTERS, get_banned_characters, set_banned_characters,
    get_queue_timeout, get_invite_timeout,
)

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_IDS = set(os.getenv("ADMIN_IDS", "").split(","))
MATCH_LOG_CHANNEL = os.getenv("MATCH_LOG_CHANNEL")
MATCHMAKING_CHANNEL = os.getenv("MATCHMAKING_CHANNEL")
GUILD_ID = os.getenv("GUILD_ID")


def is_admin(interaction: discord.Interaction) -> bool:
    return str(interaction.user.id) in ADMIN_IDS


def is_matchmaking_channel(interaction: discord.Interaction) -> bool:
    if not MATCHMAKING_CHANNEL:
        return True  # No restriction if not configured
    if str(interaction.channel_id) == MATCHMAKING_CHANNEL:
        return True
    # Allow match threads that are children of the matchmaking channel
    if isinstance(interaction.channel, discord.Thread):
        return str(interaction.channel.parent_id) == MATCHMAKING_CHANNEL
    return False


WRONG_CHANNEL_MSG = f"This command can only be used in <#{MATCHMAKING_CHANNEL}>." if MATCHMAKING_CHANNEL else ""


intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)
queue = MatchmakingQueue()
doubles_queue = DoublesQueue()

# Track votes per match message: {message_id: {discord_id: emoji}}
match_votes: dict[int, dict[str, str]] = {}

# Track cancel requests per match: {match_id: set of discord_ids who requested cancel}
cancel_requests: dict[int, set[str]] = {}

# Track which channel each queued player joined from: {discord_id: channel_id}
queue_channels: dict[str, int] = {}

# Track vote timeout tasks: {message_id: asyncio.Task}
vote_timers: dict[int, asyncio.Task] = {}

# Track pending doubles invites: {inviter_id: {partner_id, message_id, channel_id}}
doubles_invites: dict[str, dict] = {}

# Track invite message IDs back to inviter: {message_id: inviter_id}
invite_messages: dict[int, str] = {}

# Track invite timeout tasks: {inviter_id: asyncio.Task}
invite_timers: dict[str, asyncio.Task] = {}

DEFAULT_VOTE_TIMEOUT = 300  # 5 minutes default
DEFAULT_MATCH_EXPIRE = 30  # 30 minutes default


def get_vote_timeout() -> int:
    return int(get_setting("vote_timeout", str(DEFAULT_VOTE_TIMEOUT)))


def get_match_expire_minutes() -> int:
    return int(get_setting("match_expire_minutes", str(DEFAULT_MATCH_EXPIRE)))


async def log_to_match_channel(embed: discord.Embed):
    """Post an embed to the match log channel if configured."""
    if not MATCH_LOG_CHANNEL:
        print("[Match Log] MATCH_LOG_CHANNEL not set in .env")
        return
    try:
        log_channel = bot.get_channel(int(MATCH_LOG_CHANNEL))
        if log_channel is None:
            log_channel = await bot.fetch_channel(int(MATCH_LOG_CHANNEL))
        await log_channel.send(embed=embed)
        print(f"[Match Log] Posted to channel {MATCH_LOG_CHANNEL}")
    except discord.HTTPException as e:
        print(f"[Match Log] Failed to post: {e}")


def get_player_team(match: dict, discord_id: str) -> int | None:
    """Return 1 if player is on team 1, 2 if on team 2, None if not in match."""
    if discord_id in (match["player1_id"], match["player2_id"]):
        return 1
    if discord_id in (match.get("player3_id"), match.get("player4_id")):
        return 2
    return None


async def resolve_doubles_match(match: dict, winning_team: int, channel: discord.abc.Messageable, message_id: int):
    """Resolve a doubles match. winning_team is 1 or 2."""
    p1_r = get_or_create_doubles_rating(match["player1_id"], get_player(match["player1_id"])["username"])
    p2_r = get_or_create_doubles_rating(match["player2_id"], get_player(match["player2_id"])["username"])
    p3_r = get_or_create_doubles_rating(match["player3_id"], get_player(match["player3_id"])["username"])
    p4_r = get_or_create_doubles_rating(match["player4_id"], get_player(match["player4_id"])["username"])

    t1_avg = (p1_r["elo"] + p2_r["elo"]) / 2
    t2_avg = (p3_r["elo"] + p4_r["elo"]) / 2

    team1 = get_or_create_team(match["player1_id"], match["player2_id"])
    team2 = get_or_create_team(match["player3_id"], match["player4_id"])

    new_elos = {}
    for rating, opp_avg, won in [
        (p1_r, t2_avg, winning_team == 1), (p2_r, t2_avg, winning_team == 1),
        (p3_r, t1_avg, winning_team == 2), (p4_r, t1_avg, winning_team == 2),
    ]:
        k = get_k_factor(rating["wins"] + rating["losses"])
        exp = expected_score(rating["elo"], opp_avg)
        new_elo = round(rating["elo"] + k * ((1 if won else 0) - exp))
        new_elos[rating["discord_id"]] = new_elo
        update_doubles_stats(rating["discord_id"], new_elo, won)

    t1_k = get_k_factor(team1["wins"] + team1["losses"])
    t2_k = get_k_factor(team2["wins"] + team2["losses"])
    t1_exp = expected_score(team1["elo"], team2["elo"])
    t2_exp = expected_score(team2["elo"], team1["elo"])
    new_t1_elo = round(team1["elo"] + t1_k * ((1 if winning_team == 1 else 0) - t1_exp))
    new_t2_elo = round(team2["elo"] + t2_k * ((1 if winning_team == 2 else 0) - t2_exp))
    update_team_stats(match["player1_id"], match["player2_id"], new_t1_elo, winning_team == 1)
    update_team_stats(match["player3_id"], match["player4_id"], new_t2_elo, winning_team == 2)

    winner_id = match["player1_id"] if winning_team == 1 else match["player3_id"]
    complete_doubles_match(
        match["id"], winner_id,
        new_elos[match["player1_id"]], new_elos[match["player2_id"]],
        new_elos[match["player3_id"]], new_elos[match["player4_id"]],
    )

    p1 = get_player(match["player1_id"])
    p2 = get_player(match["player2_id"])
    p3 = get_player(match["player3_id"])
    p4 = get_player(match["player4_id"])

    if winning_team == 1:
        winner_names = f"{p1['username']} & {p2['username']}"
        loser_names = f"{p3['username']} & {p4['username']}"
        w_delta1 = new_elos[p1["discord_id"]] - p1_r["elo"]
        w_delta2 = new_elos[p2["discord_id"]] - p2_r["elo"]
        l_delta3 = new_elos[p3["discord_id"]] - p3_r["elo"]
        l_delta4 = new_elos[p4["discord_id"]] - p4_r["elo"]
        winner_value = (f"{p1['username']} {p1_r['elo']} → {new_elos[p1['discord_id']]} (+{w_delta1})\n"
                        f"{p2['username']} {p2_r['elo']} → {new_elos[p2['discord_id']]} (+{w_delta2})")
        loser_value = (f"{p3['username']} {p3_r['elo']} → {new_elos[p3['discord_id']]} ({l_delta3})\n"
                       f"{p4['username']} {p4_r['elo']} → {new_elos[p4['discord_id']]} ({l_delta4})")
        team_win = f"{new_t1_elo} (+{new_t1_elo - team1['elo']})"
        team_loss = f"{new_t2_elo} ({new_t2_elo - team2['elo']})"
    else:
        winner_names = f"{p3['username']} & {p4['username']}"
        loser_names = f"{p1['username']} & {p2['username']}"
        w_delta3 = new_elos[p3["discord_id"]] - p3_r["elo"]
        w_delta4 = new_elos[p4["discord_id"]] - p4_r["elo"]
        l_delta1 = new_elos[p1["discord_id"]] - p1_r["elo"]
        l_delta2 = new_elos[p2["discord_id"]] - p2_r["elo"]
        winner_value = (f"{p3['username']} {p3_r['elo']} → {new_elos[p3['discord_id']]} (+{w_delta3})\n"
                        f"{p4['username']} {p4_r['elo']} → {new_elos[p4['discord_id']]} (+{w_delta4})")
        loser_value = (f"{p1['username']} {p1_r['elo']} → {new_elos[p1['discord_id']]} ({l_delta1})\n"
                       f"{p2['username']} {p2_r['elo']} → {new_elos[p2['discord_id']]} ({l_delta2})")
        team_win = f"{new_t2_elo} (+{new_t2_elo - team2['elo']})"
        team_loss = f"{new_t1_elo} ({new_t1_elo - team1['elo']})"

    result_embed = discord.Embed(
        title=f"Doubles Match #{match['id']} Result",
        color=discord.Color.gold(),
    )
    result_embed.add_field(name=f"Winners — {winner_names}", value=winner_value, inline=False)
    result_embed.add_field(name=f"Losers — {loser_names}", value=loser_value, inline=False)
    result_embed.add_field(name="Winning Team Elo", value=team_win, inline=True)
    result_embed.add_field(name="Losing Team Elo", value=team_loss, inline=True)
    await channel.send(embed=result_embed)
    await log_to_match_channel(result_embed)

    match_votes.pop(message_id, None)
    cancel_requests.pop(match["id"], None)
    vote_timers.pop(message_id, None)

    try:
        await channel.send("**Match complete!** This thread will now close.")
        await channel.edit(archived=True, locked=True)
    except discord.HTTPException:
        pass


async def doubles_vote_timeout(message_id: int, channel_id: int, match_id: str):
    await asyncio.sleep(get_vote_timeout())
    votes = match_votes.get(message_id)
    if votes is None:
        return
    match = get_match_by_message(str(message_id))
    if not match:
        return
    team1_votes = {did: v for did, v in votes.items() if get_player_team(match, did) == 1}
    team2_votes = {did: v for did, v in votes.items() if get_player_team(match, did) == 2}

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return

    if team1_votes and not team2_votes:
        t1_vote = next(iter(team1_votes.values()))
        winning_team = 1 if t1_vote == REACT_P1 else 2
        await channel.send("⏰ Team 2 did not respond in time. Team 1's result has been accepted.")
        await resolve_doubles_match(match, winning_team, channel, message_id)
    elif team2_votes and not team1_votes:
        t2_vote = next(iter(team2_votes.values()))
        winning_team = 1 if t2_vote == REACT_P1 else 2
        await channel.send("⏰ Team 1 did not respond in time. Team 2's result has been accepted.")
        await resolve_doubles_match(match, winning_team, channel, message_id)
    vote_timers.pop(message_id, None)


async def invite_timeout(inviter_id: str):
    await asyncio.sleep(get_invite_timeout())
    invite = doubles_invites.pop(inviter_id, None)
    if not invite:
        return
    invite_messages.pop(invite["message_id"], None)
    invite_timers.pop(inviter_id, None)
    channel = bot.get_channel(invite["channel_id"])
    if channel:
        await channel.send(
            f"<@{inviter_id}> your doubles invite to <@{invite['partner_id']}> has expired."
        )


async def resolve_match(match: dict, winner_emoji: str, channel: discord.abc.Messageable, message_id: int):
    """Resolve a match based on the winning emoji. Updates Elo, sends result, and closes thread."""
    winner_id = match["player1_id"] if winner_emoji == REACT_P1 else match["player2_id"]
    loser_id = match["player2_id"] if winner_id == match["player1_id"] else match["player1_id"]

    winner_player = get_player(winner_id)
    loser_player = get_player(loser_id)

    new_winner_elo, new_loser_elo = calculate_new_ratings(
        winner_player["elo"], loser_player["elo"],
        winner_player["wins"] + winner_player["losses"],
        loser_player["wins"] + loser_player["losses"],
    )

    if winner_id == match["player1_id"]:
        p1_elo_after, p2_elo_after = new_winner_elo, new_loser_elo
    else:
        p1_elo_after, p2_elo_after = new_loser_elo, new_winner_elo

    complete_match(match["id"], winner_id, p1_elo_after, p2_elo_after)
    update_player_stats(winner_id, new_winner_elo, won=True)
    update_player_stats(loser_id, new_loser_elo, won=False)

    winner_delta = new_winner_elo - winner_player["elo"]
    loser_delta = new_loser_elo - loser_player["elo"]

    result_embed = discord.Embed(
        title=f"Match #{match['id']} Result",
        color=discord.Color.gold(),
    )
    result_embed.add_field(
        name="Winner",
        value=f"**{winner_player['username']}** {winner_player['elo']} \u2192 {new_winner_elo} (+{winner_delta})",
        inline=False,
    )
    result_embed.add_field(
        name="Loser",
        value=f"**{loser_player['username']}** {loser_player['elo']} \u2192 {new_loser_elo} ({loser_delta})",
        inline=False,
    )
    await channel.send(embed=result_embed)

    # Log to match history channel
    await log_to_match_channel(result_embed)

    # Record recent match for cooldown
    queue.record_match(match["player1_id"], match["player2_id"])

    # Clean up
    match_votes.pop(message_id, None)
    cancel_requests.pop(match["id"], None)
    vote_timers.pop(message_id, None)

    # Archive and lock the thread
    try:
        await channel.send("**Match complete!** This thread will now close.")
        await channel.edit(archived=True, locked=True)
    except discord.HTTPException:
        pass


async def vote_timeout(message_id: int, channel_id: int, match_id_str: str, voter_id: str, other_id: str, voter_emoji: str):
    """Wait 5 minutes, then accept the first voter's result if the other player hasn't voted."""
    await asyncio.sleep(get_vote_timeout())

    votes = match_votes.get(message_id)
    if votes is None:
        return  # Match already resolved

    # If the other player still hasn't voted, accept the first voter's result
    if other_id not in votes:
        match = get_match_by_message(str(message_id))
        if not match:
            return

        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return

        await channel.send(
            f"<@{other_id}> did not respond in time. "
            f"<@{voter_id}>'s result has been accepted."
        )
        await resolve_match(match, voter_emoji, channel, message_id)


@bot.event
async def on_ready():
    init_db()
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        tree.clear_commands(guild=None)
        await tree.sync()
    else:
        await tree.sync()
    if not check_queue_matches.is_running():
        check_queue_matches.start()
    if not check_expired_matches.is_running():
        check_expired_matches.start()
    print(f"Bot is online as {bot.user}")


async def try_create_match(channel: discord.TextChannel) -> bool:
    """Attempt to find and create a match. Returns True if a match was made."""
    result = queue.find_match()
    if not result:
        return False

    p1, p2, match_id = result
    queue_channels.pop(p1["discord_id"], None)
    queue_channels.pop(p2["discord_id"], None)

    court = pick_court()
    match_length = get_match_length()
    embed = build_match_embed(p1, p2, match_id, court)

    thread = await channel.create_thread(
        name=f"Match #{match_id}: {p1['username']} vs {p2['username']}",
        type=discord.ChannelType.private_thread,
    )

    match_msg = await thread.send(
        f"<@{p1['discord_id']}> <@{p2['discord_id']}>", embed=embed
    )
    await match_msg.add_reaction(REACT_P1)
    await match_msg.add_reaction(REACT_P2)

    banned = get_banned_characters()
    banned_text = ""
    if banned:
        banned_text = (
            f"\n\n**Banned characters:** The following characters are **banned** and may not be used: "
            f"**{', '.join(banned)}**. Using a banned character may result in a forfeit."
        )

    await thread.send(
        f"**How to play:**\n"
        f"1. One player creates a **private match** in Mario Tennis and shares the room code here\n"
        f"2. The other player joins using the room code\n"
        f"3. Play with the settings shown above: **{court}** court, **High** ball speed, **Classic** mode, **{match_length}**\n"
        f"4. After the match, both players react above with the winner's icon ({REACT_P1} or {REACT_P2})\n\n"
        f"**No-show rule:** If your opponent does not respond in this thread within {get_vote_timeout() // 60} minute(s), "
        f"report yourself as the winner by reacting above. If they don't dispute within {get_vote_timeout() // 60} minute(s), "
        f"the result will be accepted automatically.\n\n"
        f"**Cancellation:** If both players agree not to play, either player can type `/cancel`. "
        f"The other player must also type `/cancel` to confirm. No Elo changes will be applied.\n\n"
        f"**Auto-expire:** If no result is reported within **{get_match_expire_minutes()} minutes**, "
        f"this match will be automatically cancelled with no Elo changes."
        f"{banned_text}"
    )

    set_match_thread(match_id, str(thread.id), str(match_msg.id))
    match_votes[match_msg.id] = {}

    await channel.send(
        f"**Match #{match_id}** created! "
        f"<@{p1['discord_id']}> vs <@{p2['discord_id']}> \u2014 "
        f"check your private thread."
    )
    return True


async def try_create_doubles_match(channel: discord.TextChannel) -> bool:
    result = doubles_queue.find_match()
    if not result:
        return False

    team1, team2 = result
    p1, p2 = team1
    p3, p4 = team2

    p1_r = get_or_create_doubles_rating(p1["discord_id"], p1["username"])
    p2_r = get_or_create_doubles_rating(p2["discord_id"], p2["username"])
    p3_r = get_or_create_doubles_rating(p3["discord_id"], p3["username"])
    p4_r = get_or_create_doubles_rating(p4["discord_id"], p4["username"])

    match_id = create_doubles_match(
        p1["discord_id"], p2["discord_id"], p3["discord_id"], p4["discord_id"],
        p1_r["elo"], p2_r["elo"], p3_r["elo"], p4_r["elo"],
    )

    court = pick_court()
    match_length = get_match_length()
    embed = build_doubles_match_embed(
        team1, team2, match_id, court,
        p1_r["elo"], p2_r["elo"], p3_r["elo"], p4_r["elo"],
    )

    thread = await channel.create_thread(
        name=f"Doubles #{match_id}",
        type=discord.ChannelType.private_thread,
    )

    for p in [p1, p2, p3, p4]:
        await thread.add_user(discord.Object(id=int(p["discord_id"])))

    match_msg = await thread.send(
        f"<@{p1['discord_id']}> <@{p2['discord_id']}> <@{p3['discord_id']}> <@{p4['discord_id']}>",
        embed=embed,
    )
    await match_msg.add_reaction(REACT_P1)
    await match_msg.add_reaction(REACT_P2)

    banned = get_banned_characters()
    banned_text = ""
    if banned:
        banned_text = (
            f"\n\n**Banned characters:** The following characters are **banned** and may not be used: "
            f"**{', '.join(banned)}**. Using a banned character may result in a forfeit."
        )

    await thread.send(
        f"**How to play:**\n"
        f"1. One player creates a **private match** in Mario Tennis and shares the room code here\n"
        f"2. All players join using the room code\n"
        f"3. Play with the settings shown above: **{court}** court, **High** ball speed, **Classic** mode, **{match_length}**\n"
        f"4. After the match, one player from each team reacts above with the winning team's icon ({REACT_P1} = Team 1, {REACT_P2} = Team 2)\n\n"
        f"**No-show rule:** If one team does not react within {get_vote_timeout() // 60} minute(s), "
        f"the other team's result will be accepted automatically.\n\n"
        f"**Cancellation:** If all players agree not to play, use `/cancel`. "
        f"One player from each team must confirm. No Elo changes will be applied.\n\n"
        f"**Auto-expire:** If no result is reported within **{get_match_expire_minutes()} minutes**, "
        f"this match will be automatically cancelled with no Elo changes."
        f"{banned_text}"
    )

    set_match_thread(match_id, str(thread.id), str(match_msg.id))
    match_votes[match_msg.id] = {}

    await channel.send(
        f"**Doubles Match #{match_id}** created! "
        f"{p1['username']} & {p2['username']} vs {p3['username']} & {p4['username']} — "
        f"check your private thread."
    )
    return True


@tasks.loop(seconds=10)
async def check_queue_matches():
    """Periodically check the queue for matches and remove idle players."""
    # Remove players who have been in the queue too long
    timeout_seconds = get_queue_timeout() * 60
    now = time.time()
    for discord_id in list(queue.queue.keys()):
        join_time = queue.join_times.get(discord_id, now)
        if now - join_time >= timeout_seconds:
            player = queue.queue.get(discord_id)
            queue.remove_player(discord_id)
            ch_id = queue_channels.pop(discord_id, None)
            if player and ch_id:
                channel = bot.get_channel(ch_id)
                if channel:
                    await channel.send(
                        f"<@{discord_id}> you have been removed from the queue due to inactivity "
                        f"({get_queue_timeout()} minute timeout). Use `/join` to re-enter."
                    )

    # Remove idle doubles players
    for discord_id in list(doubles_queue.solos.keys()):
        join_time = doubles_queue.join_times.get(discord_id, now)
        if now - join_time >= timeout_seconds:
            doubles_queue.remove_player(discord_id)
            ch_id = queue_channels.pop(discord_id, None)
            if ch_id:
                channel = bot.get_channel(ch_id)
                if channel:
                    await channel.send(
                        f"<@{discord_id}> you have been removed from the doubles queue due to inactivity "
                        f"({get_queue_timeout()} minute timeout). Use `/join_doubles` to re-enter."
                    )
    for team_key in list(doubles_queue.teams.keys()):
        sample_id = next(iter(team_key))
        join_time = doubles_queue.join_times.get(sample_id, now)
        if now - join_time >= timeout_seconds:
            team = doubles_queue.teams.get(team_key)
            if team:
                for p in team["players"]:
                    doubles_queue.join_times.pop(p["discord_id"], None)
                    queue_channels.pop(p["discord_id"], None)
                doubles_queue.teams.pop(team_key, None)
                first_ch = queue_channels.get(next(iter(team_key)))
                ch_id = first_ch or (list(queue_channels.values())[0] if queue_channels else None)
                if ch_id:
                    channel = bot.get_channel(ch_id)
                    if channel:
                        p1, p2 = team["players"]
                        await channel.send(
                            f"<@{p1['discord_id']}> <@{p2['discord_id']}> your team has been removed from the "
                            f"doubles queue due to inactivity ({get_queue_timeout()} minute timeout)."
                        )

    # Singles matching
    if queue.queue_size() >= 2:
        channels_seen = set()
        for discord_id in list(queue.queue.keys()):
            ch_id = queue_channels.get(discord_id)
            if ch_id:
                channels_seen.add(ch_id)
        for ch_id in channels_seen:
            channel = bot.get_channel(ch_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(ch_id)
                except discord.HTTPException:
                    continue
            while await try_create_match(channel):
                pass
            break

    # Doubles matching
    if doubles_queue.queue_size() >= 4 or (doubles_queue.teams and doubles_queue.queue_size() >= 2):
        d_channels = set()
        for discord_id in list(doubles_queue.solos.keys()):
            ch_id = queue_channels.get(discord_id)
            if ch_id:
                d_channels.add(ch_id)
        for team in doubles_queue.teams.values():
            for p in team["players"]:
                ch_id = queue_channels.get(p["discord_id"])
                if ch_id:
                    d_channels.add(ch_id)
        for ch_id in d_channels:
            channel = bot.get_channel(ch_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(ch_id)
                except discord.HTTPException:
                    continue
            while await try_create_doubles_match(channel):
                pass
            break


@check_queue_matches.before_loop
async def before_check_queue():
    await bot.wait_until_ready()


@tasks.loop(minutes=1)
async def check_expired_matches():
    """Auto-cancel matches that have been pending for too long without a result."""
    expire_mins = get_match_expire_minutes()
    expired = get_expired_matches(expire_mins)
    for match in expired:
        is_doubles = match["game_mode"] == "doubles"

        # Skip disputed matches
        if match["message_id"]:
            votes = match_votes.get(int(match["message_id"]), {})
            if is_doubles:
                team1_voted = match["player1_id"] in votes or match["player2_id"] in votes
                team2_voted = match["player3_id"] in votes or match["player4_id"] in votes
                if team1_voted and team2_voted:
                    continue  # Disputed doubles — leave for admin
            else:
                if match["player1_id"] in votes and match["player2_id"] in votes:
                    continue  # Disputed singles — leave for admin

        # Cancel the match
        if is_doubles:
            complete_doubles_match(
                match["id"], "expired",
                match["player1_elo_before"], match["player2_elo_before"],
                match["player3_elo_before"], match["player4_elo_before"],
            )
            desc = (f"<@{match['player1_id']}> & <@{match['player2_id']}> vs "
                    f"<@{match['player3_id']}> & <@{match['player4_id']}>\n"
                    f"Auto-cancelled after {expire_mins} minutes. No Elo changes.")
        else:
            complete_match(
                match["id"], "expired",
                match["player1_elo_before"], match["player2_elo_before"],
            )
            desc = (f"<@{match['player1_id']}> vs <@{match['player2_id']}>\n"
                    f"Auto-cancelled after {expire_mins} minutes. No Elo changes.")

        # Clean up in-memory tracking
        if match["message_id"]:
            msg_id = int(match["message_id"])
            match_votes.pop(msg_id, None)
            timer = vote_timers.pop(msg_id, None)
            if timer:
                timer.cancel()
        cancel_requests.pop(match["id"], None)

        # Notify in the thread and close it
        if match["thread_id"]:
            try:
                thread = bot.get_channel(int(match["thread_id"]))
                if thread is None:
                    thread = await bot.fetch_channel(int(match["thread_id"]))
                await thread.send(
                    f"**Match #{match['id']} has been automatically cancelled** — "
                    f"no result was reported within {expire_mins} minutes. No Elo changes applied."
                )
                await thread.edit(archived=True, locked=True)
            except discord.HTTPException:
                pass

        expire_embed = discord.Embed(
            title=f"Match #{match['id']} Expired",
            description=desc,
            color=discord.Color.light_grey(),
        )
        await log_to_match_channel(expire_embed)


@check_expired_matches.before_loop
async def before_check_expired():
    await bot.wait_until_ready()


@tree.command(name="join", description="Join the matchmaking queue")
async def join_queue(interaction: discord.Interaction):
    if not is_matchmaking_channel(interaction):
        await interaction.response.send_message(WRONG_CHANNEL_MSG, ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    username = interaction.user.display_name

    # Update username in case it changed
    existing = get_player(discord_id)
    if existing and existing["username"] != username:
        update_player_username(discord_id, username)

    if is_banned(discord_id):
        await interaction.response.send_message(
            "You are banned from matchmaking.", ephemeral=True
        )
        return

    if queue.is_in_queue(discord_id):
        await interaction.response.send_message(
            "You are already in the singles queue!", ephemeral=True
        )
        return

    if doubles_queue.is_in_queue(discord_id):
        await interaction.response.send_message(
            "You are already in the doubles queue. Use `/leave` to leave before joining singles.", ephemeral=True
        )
        return

    pending = get_pending_match(discord_id)
    if pending:
        await interaction.response.send_message(
            f"You have an unfinished match (#{pending['id']}). "
            "Finish your current match or use `/cancel` to cancel it. "
            f"The match will automatically close after {get_match_expire_minutes()} minutes if no result is reported. "
            "If your thread was deleted, ask an admin to run "
            f"`/admin_cancel {pending['id']}`.",
            ephemeral=True,
        )
        return

    player = queue.add_player(discord_id, username)
    queue_channels[discord_id] = interaction.channel_id
    await interaction.response.send_message(
        f"**{username}** joined the queue! (Elo: {player['elo']}) "
        f"Players in queue: {queue.queue_size()}"
    )

    # Try to find a match immediately
    await try_create_match(interaction.channel)


@tree.command(name="leave", description="Leave the matchmaking queue")
async def leave_queue(interaction: discord.Interaction):
    if not is_matchmaking_channel(interaction):
        await interaction.response.send_message(WRONG_CHANNEL_MSG, ephemeral=True)
        return

    discord_id = str(interaction.user.id)

    # Cancel a pending doubles invite (as inviter)
    invite = doubles_invites.pop(discord_id, None)
    if invite:
        invite_messages.pop(invite["message_id"], None)
        timer = invite_timers.pop(discord_id, None)
        if timer:
            timer.cancel()
        await interaction.response.send_message(
            f"**{interaction.user.display_name}** cancelled their doubles invite."
        )
        return

    # Leave singles queue
    if queue.remove_player(discord_id):
        queue_channels.pop(discord_id, None)
        await interaction.response.send_message(
            f"**{interaction.user.display_name}** left the singles queue."
        )
        return

    # Leave doubles queue
    partner_id = doubles_queue.remove_player(discord_id)
    if partner_id is not None or doubles_queue.solos.get(discord_id) is None:
        queue_channels.pop(discord_id, None)
        msg = f"**{interaction.user.display_name}** left the doubles queue."
        if partner_id:
            queue_channels.pop(partner_id, None)
            msg += f" <@{partner_id}> has also been removed from the queue."
        await interaction.response.send_message(msg)
        return

    await interaction.response.send_message(
        "You are not in any queue.", ephemeral=True
    )


@tree.command(name="queue", description="See who is in the matchmaking queue")
async def view_queue(interaction: discord.Interaction):
    if not is_matchmaking_channel(interaction):
        await interaction.response.send_message(WRONG_CHANNEL_MSG, ephemeral=True)
        return

    embed = discord.Embed(title="Matchmaking Queue", color=discord.Color.blue())

    if queue.queue_size() > 0:
        lines = [
            f"{i}. **{p['username']}** (Elo: {p['elo']})"
            for i, p in enumerate(sorted(queue.queue.values(), key=lambda x: x["elo"], reverse=True), 1)
        ]
        embed.add_field(name=f"Singles ({queue.queue_size()} player(s))", value="\n".join(lines), inline=False)

    team_lines, solo_lines = doubles_queue.get_queue_entries()
    if team_lines or solo_lines:
        d_lines = []
        if team_lines:
            d_lines += [f"**Teams:**"] + [f"• {l}" for l in team_lines]
        if solo_lines:
            d_lines += [f"**Solo:**"] + [f"• {l}" for l in solo_lines]
        embed.add_field(
            name=f"Doubles ({doubles_queue.queue_size()} player(s))",
            value="\n".join(d_lines),
            inline=False,
        )

    if queue.queue_size() == 0 and doubles_queue.queue_size() == 0:
        await interaction.response.send_message("Both queues are empty.", ephemeral=True)
        return

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="stats", description="View your stats or another player's stats")
@app_commands.describe(player="The player to look up (defaults to yourself)")
async def stats(
    interaction: discord.Interaction, player: discord.Member | None = None
):
    if not is_matchmaking_channel(interaction):
        await interaction.response.send_message(WRONG_CHANNEL_MSG, ephemeral=True)
        return

    target = player or interaction.user
    discord_id = str(target.id)
    p = get_or_create_player(discord_id, target.display_name)

    total = p["wins"] + p["losses"]
    win_rate = f"{p['wins'] / total * 100:.1f}%" if total > 0 else "N/A"
    rank, total_players = get_player_rank(discord_id)

    embed = discord.Embed(title=f"{p['username']}'s Stats", color=discord.Color.purple())
    embed.add_field(name="Singles Rank", value=f"#{rank} of {total_players}", inline=True)
    embed.add_field(name="Singles Elo", value=str(p["elo"]), inline=True)
    embed.add_field(name="​", value="​", inline=True)
    embed.add_field(name="Wins", value=str(p["wins"]), inline=True)
    embed.add_field(name="Losses", value=str(p["losses"]), inline=True)
    embed.add_field(name="Win Rate", value=win_rate, inline=True)

    d_rating = get_doubles_rating(discord_id)
    if d_rating:
        d_total = d_rating["wins"] + d_rating["losses"]
        d_wr = f"{d_rating['wins'] / d_total * 100:.1f}%" if d_total > 0 else "N/A"
        d_rank = get_doubles_player_rank(discord_id)
        d_rank_str = f"#{d_rank[0]} of {d_rank[1]}" if d_rank else "N/A"
        embed.add_field(name="Doubles Rank", value=d_rank_str, inline=True)
        embed.add_field(name="Doubles Elo", value=str(d_rating["elo"]), inline=True)
        embed.add_field(name="​", value="​", inline=True)
        embed.add_field(name="Doubles Wins", value=str(d_rating["wins"]), inline=True)
        embed.add_field(name="Doubles Losses", value=str(d_rating["losses"]), inline=True)
        embed.add_field(name="Doubles Win Rate", value=d_wr, inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="leaderboard", description="View the top players")
async def leaderboard(interaction: discord.Interaction):
    if not is_matchmaking_channel(interaction):
        await interaction.response.send_message(WRONG_CHANNEL_MSG, ephemeral=True)
        return

    top = get_leaderboard(10)
    if not top:
        await interaction.response.send_message(
            "No players yet!", ephemeral=True
        )
        return

    discord_id = str(interaction.user.id)
    top_ids = {p["discord_id"] for p in top}

    lines = []
    for i, p in enumerate(top, 1):
        medal = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}.get(i, f"{i}.")
        marker = " \u2190 You" if p["discord_id"] == discord_id else ""
        lines.append(
            f"{medal} **{p['username']}** - Elo: {p['elo']} "
            f"({p['wins']}W / {p['losses']}L){marker}"
        )

    # If the caller isn't in the top 10, append their position
    if discord_id not in top_ids:
        caller = get_player(discord_id)
        if caller:
            rank, _ = get_player_rank(discord_id)
            lines.append("...")
            lines.append(
                f"{rank}. **{caller['username']}** - Elo: {caller['elo']} "
                f"({caller['wins']}W / {caller['losses']}L) \u2190 You"
            )

    embed = discord.Embed(
        title="Leaderboard - Mario Tennis",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="cancel", description="Request to cancel your pending match")
async def cancel_match(interaction: discord.Interaction):
    if not is_matchmaking_channel(interaction):
        await interaction.response.send_message(WRONG_CHANNEL_MSG, ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    pending = get_pending_match(discord_id)
    if not pending:
        await interaction.response.send_message(
            "You don't have a pending match.", ephemeral=True
        )
        return

    match_id = pending["id"]
    is_doubles = pending["game_mode"] == "doubles"

    if match_id not in cancel_requests:
        cancel_requests[match_id] = set()

    cancel_requests[match_id].add(discord_id)

    if is_doubles:
        # For doubles: need one player from each team to agree
        team1_ids = {pending["player1_id"], pending["player2_id"]}
        team2_ids = {pending["player3_id"], pending["player4_id"]}
        agreed = cancel_requests[match_id]
        both_teams_agreed = bool(agreed & team1_ids) and bool(agreed & team2_ids)
    else:
        both_teams_agreed = len(cancel_requests[match_id]) >= 2

    if both_teams_agreed:
        if is_doubles:
            complete_doubles_match(
                match_id, "cancelled",
                pending["player1_elo_before"], pending["player2_elo_before"],
                pending["player3_elo_before"], pending["player4_elo_before"],
            )
            desc = (f"<@{pending['player1_id']}> & <@{pending['player2_id']}> vs "
                    f"<@{pending['player3_id']}> & <@{pending['player4_id']}>\n"
                    f"Cancelled by mutual agreement. No Elo changes.")
        else:
            complete_match(
                match_id, "cancelled",
                pending["player1_elo_before"], pending["player2_elo_before"],
            )
            desc = (f"<@{pending['player1_id']}> vs <@{pending['player2_id']}>\n"
                    f"Cancelled by both players. No Elo changes.")

        # Clean up tracking
        if pending["message_id"]:
            match_votes.pop(int(pending["message_id"]), None)
        cancel_requests.pop(match_id, None)

        await interaction.response.send_message(
            f"Match #{match_id} has been cancelled. No Elo changes applied."
        )

        cancel_embed = discord.Embed(
            title=f"Match #{match_id} Cancelled",
            description=desc,
            color=discord.Color.light_grey(),
        )
        await log_to_match_channel(cancel_embed)

        if pending["thread_id"]:
            try:
                thread = bot.get_channel(int(pending["thread_id"]))
                if thread is None:
                    thread = await bot.fetch_channel(int(pending["thread_id"]))
                await thread.send(f"**Match #{match_id} cancelled.** Thread will now close.")
                await thread.edit(archived=True, locked=True)
            except discord.HTTPException:
                pass
    else:
        if is_doubles:
            team1_ids = {pending["player1_id"], pending["player2_id"]}
            if discord_id in team1_ids:
                await interaction.response.send_message(
                    f"<@{discord_id}> wants to cancel **Match #{match_id}**.\n"
                    f"<@{pending['player3_id']}> or <@{pending['player4_id']}>, type `/cancel` to agree."
                )
            else:
                await interaction.response.send_message(
                    f"<@{discord_id}> wants to cancel **Match #{match_id}**.\n"
                    f"<@{pending['player1_id']}> or <@{pending['player2_id']}>, type `/cancel` to agree."
                )
        else:
            opponent_id = (
                pending["player2_id"] if discord_id == pending["player1_id"] else pending["player1_id"]
            )
            await interaction.response.send_message(
                f"<@{discord_id}> wants to cancel **Match #{match_id}**.\n"
                f"<@{opponent_id}>, type `/cancel` to agree.\n"
                f"If you disagree, the match will be flagged for moderator review."
            )


@tree.command(name="reset_season", description="[Admin] Reset all players' Elo to 1500 and clear win/loss records")
async def reset_season_cmd(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    count = reset_season()
    await interaction.response.send_message(
        f"Season has been reset! {count} player(s) reset to 1500 Elo with 0 wins/losses."
    )


@tree.command(name="reset_singles", description="[Admin] Reset all singles Elo to 1500 and clear win/loss records")
async def reset_singles_cmd(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    count = reset_singles()
    await interaction.response.send_message(
        f"Singles season has been reset! {count} player(s) reset to 1500 Elo with 0 wins/losses."
    )


@tree.command(name="reset_doubles", description="[Admin] Reset all doubles and team Elo and clear records")
async def reset_doubles_cmd(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    count = reset_doubles()
    await interaction.response.send_message(
        f"Doubles season has been reset! {count} player(s) and all team ratings reset. Everyone starts at 1500."
    )


@tree.command(name="set_elo", description="[Admin] Set a player's Elo to a specific value")
@app_commands.describe(player="The player to adjust", elo="The new Elo value")
async def set_elo_cmd(interaction: discord.Interaction, player: discord.Member, elo: int):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    discord_id = str(player.id)
    get_or_create_player(discord_id, player.display_name)

    if set_player_elo(discord_id, elo):
        await interaction.response.send_message(
            f"**{player.display_name}**'s Elo has been set to **{elo}**."
        )
    else:
        await interaction.response.send_message("Failed to update Elo.", ephemeral=True)


@tree.command(name="ban", description="[Admin] Ban a player from matchmaking")
@app_commands.describe(player="The player to ban", reason="Reason for the ban")
async def ban_cmd(interaction: discord.Interaction, player: discord.Member, reason: str | None = None):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    discord_id = str(player.id)

    if ban_player(discord_id, str(interaction.user.id), reason):
        # Remove from queue if they're in it
        if queue.remove_player(discord_id):
            queue_channels.pop(discord_id, None)

        msg = f"**{player.display_name}** has been banned from matchmaking."
        if reason:
            msg += f"\nReason: {reason}"
        await interaction.response.send_message(msg)
    else:
        await interaction.response.send_message(
            f"**{player.display_name}** is already banned.", ephemeral=True
        )


@tree.command(name="unban", description="[Admin] Unban a player from matchmaking")
@app_commands.describe(player="The player to unban")
async def unban_cmd(interaction: discord.Interaction, player: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    discord_id = str(player.id)

    if unban_player(discord_id):
        await interaction.response.send_message(
            f"**{player.display_name}** has been unbanned from matchmaking."
        )
    else:
        await interaction.response.send_message(
            f"**{player.display_name}** is not banned.", ephemeral=True
        )


@tree.command(name="set_cooldown", description="[Admin] Set the rematch cooldown in seconds")
@app_commands.describe(seconds="Cooldown duration in seconds (e.g. 60)")
async def set_cooldown_cmd(interaction: discord.Interaction, seconds: int):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    if seconds < 0:
        await interaction.response.send_message("Cooldown cannot be negative.", ephemeral=True)
        return

    set_setting("rematch_cooldown", str(seconds))
    await interaction.response.send_message(
        f"Rematch cooldown has been set to **{seconds} seconds**."
    )


@tree.command(name="set_queue_timeout", description="[Admin] Set how long a player can be in the queue before being removed for inactivity")
@app_commands.describe(minutes="Queue timeout in minutes (e.g. 60)")
async def set_queue_timeout_cmd(interaction: discord.Interaction, minutes: int):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    if minutes < 1:
        await interaction.response.send_message("Queue timeout must be at least 1 minute.", ephemeral=True)
        return

    set_setting("queue_timeout", str(minutes))
    await interaction.response.send_message(
        f"Queue timeout has been set to **{minutes} minute(s)**. Idle players will be removed after this time."
    )


@tree.command(name="set_vote_timeout", description="[Admin] Set the no-show vote timeout in seconds")
@app_commands.describe(seconds="Vote timeout duration in seconds (e.g. 300 for 5 minutes)")
async def set_vote_timeout_cmd(interaction: discord.Interaction, seconds: int):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    if seconds < 30:
        await interaction.response.send_message("Vote timeout must be at least 30 seconds.", ephemeral=True)
        return

    set_setting("vote_timeout", str(seconds))
    await interaction.response.send_message(
        f"Vote timeout has been set to **{seconds} seconds** ({seconds // 60} min {seconds % 60} sec)."
    )


@tree.command(name="set_match_expire", description="[Admin] Set the match auto-expire duration in minutes")
@app_commands.describe(minutes="Minutes before an unreported match is auto-cancelled (e.g. 30)")
async def set_match_expire_cmd(interaction: discord.Interaction, minutes: int):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    if minutes < 5:
        await interaction.response.send_message("Match expire time must be at least 5 minutes.", ephemeral=True)
        return

    set_setting("match_expire_minutes", str(minutes))
    await interaction.response.send_message(
        f"Match auto-expire has been set to **{minutes} minutes**."
    )


VALID_MATCH_LENGTHS = ["Quick Play", "Extended Play"]


@tree.command(name="set_match_length", description="[Admin] Set the match length for new matches")
@app_commands.describe(length="Match length: Quick Play or Extended Play")
async def set_match_length_cmd(interaction: discord.Interaction, length: str):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    if length not in VALID_MATCH_LENGTHS:
        await interaction.response.send_message(
            f"Invalid match length. Choose **Quick Play** or **Extended Play**.", ephemeral=True
        )
        return

    set_setting("match_length", length)
    await interaction.response.send_message(
        f"Match length has been set to **{length}**."
    )


@set_match_length_cmd.autocomplete("length")
async def match_length_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=length, value=length)
        for length in VALID_MATCH_LENGTHS if current.lower() in length.lower()
    ]


@tree.command(name="enable_court", description="[Admin] Enable a court for the match rotation")
@app_commands.describe(court="The court to enable")
async def enable_court_cmd(interaction: discord.Interaction, court: str):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    if court not in ALL_COURTS:
        await interaction.response.send_message(f"**{court}** is not a valid court.", ephemeral=True)
        return

    enabled = get_enabled_courts()
    if court in enabled:
        await interaction.response.send_message(f"**{court}** is already enabled.", ephemeral=True)
        return

    enabled.append(court)
    set_enabled_courts(enabled)
    await interaction.response.send_message(f"**{court}** has been enabled. ({len(enabled)} courts in rotation)")


@enable_court_cmd.autocomplete("court")
async def enable_court_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    enabled = get_enabled_courts()
    disabled = [c for c in ALL_COURTS if c not in enabled]
    return [
        app_commands.Choice(name=c, value=c)
        for c in disabled if current.lower() in c.lower()
    ][:25]


@tree.command(name="disable_court", description="[Admin] Disable a court from the match rotation")
@app_commands.describe(court="The court to disable")
async def disable_court_cmd(interaction: discord.Interaction, court: str):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    enabled = get_enabled_courts()
    if court not in enabled:
        await interaction.response.send_message(f"**{court}** is not currently enabled.", ephemeral=True)
        return

    if len(enabled) <= 1:
        await interaction.response.send_message("Cannot disable the last court. At least one must be enabled.", ephemeral=True)
        return

    enabled.remove(court)
    set_enabled_courts(enabled)
    await interaction.response.send_message(f"**{court}** has been disabled. ({len(enabled)} courts in rotation)")


@disable_court_cmd.autocomplete("court")
async def disable_court_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    enabled = get_enabled_courts()
    return [
        app_commands.Choice(name=c, value=c)
        for c in enabled if current.lower() in c.lower()
    ][:25]


@tree.command(name="ban_character", description="[Admin] Ban a character from being used in matches")
@app_commands.describe(character="The character to ban")
async def ban_character_cmd(interaction: discord.Interaction, character: str):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    if character not in ALL_CHARACTERS:
        await interaction.response.send_message(f"**{character}** is not a valid character.", ephemeral=True)
        return

    banned = get_banned_characters()
    if character in banned:
        await interaction.response.send_message(f"**{character}** is already banned.", ephemeral=True)
        return

    banned.append(character)
    set_banned_characters(banned)
    await interaction.response.send_message(f"**{character}** has been banned. ({len(banned)} character(s) banned)")


@ban_character_cmd.autocomplete("character")
async def ban_character_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    banned = get_banned_characters()
    available = [c for c in ALL_CHARACTERS if c not in banned]
    return [
        app_commands.Choice(name=c, value=c)
        for c in available if current.lower() in c.lower()
    ][:25]


@tree.command(name="unban_character", description="[Admin] Unban a character so it can be used in matches")
@app_commands.describe(character="The character to unban")
async def unban_character_cmd(interaction: discord.Interaction, character: str):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    banned = get_banned_characters()
    if character not in banned:
        await interaction.response.send_message(f"**{character}** is not currently banned.", ephemeral=True)
        return

    banned.remove(character)
    set_banned_characters(banned)
    if banned:
        await interaction.response.send_message(f"**{character}** has been unbanned. ({len(banned)} character(s) still banned)")
    else:
        await interaction.response.send_message(f"**{character}** has been unbanned. No characters are banned.")


@unban_character_cmd.autocomplete("character")
async def unban_character_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    banned = get_banned_characters()
    return [
        app_commands.Choice(name=c, value=c)
        for c in banned if current.lower() in c.lower()
    ][:25]


@tree.command(name="banned_characters", description="View all currently banned characters")
async def banned_characters_cmd(interaction: discord.Interaction):
    if not is_matchmaking_channel(interaction):
        await interaction.response.send_message(WRONG_CHANNEL_MSG, ephemeral=True)
        return

    banned = get_banned_characters()
    if not banned:
        await interaction.response.send_message("No characters are currently banned.", ephemeral=True)
        return

    description = "\n".join(f"🚫 {c}" for c in banned)
    embed = discord.Embed(
        title="Banned Characters",
        description=description,
        color=discord.Color.red(),
    )
    embed.set_footer(text=f"{len(banned)} character(s) banned")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="list_courts", description="View all courts and their status")
async def list_courts_cmd(interaction: discord.Interaction):
    if not is_matchmaking_channel(interaction):
        await interaction.response.send_message(WRONG_CHANNEL_MSG, ephemeral=True)
        return

    enabled = get_enabled_courts()
    enabled_lines = [f"\u2705 {c}" for c in ALL_COURTS if c in enabled]
    disabled_lines = [f"\u274c {c}" for c in ALL_COURTS if c not in enabled]

    description = "**Enabled:**\n" + "\n".join(enabled_lines)
    if disabled_lines:
        description += "\n\n**Disabled:**\n" + "\n".join(disabled_lines)

    embed = discord.Embed(
        title="Court Rotation",
        description=description,
        color=discord.Color.teal(),
    )
    embed.set_footer(text=f"{len(enabled_lines)} of {len(ALL_COURTS)} courts enabled")
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def find_match_from_context(interaction: discord.Interaction, match_id: int | None) -> dict | None:
    """Find a pending match by ID or by detecting the current thread."""
    if match_id is not None:
        return get_match_by_id(match_id)

    # Try to detect from thread context
    if isinstance(interaction.channel, discord.Thread):
        return get_match_by_thread(str(interaction.channel.id))

    return None


@tree.command(name="resolve", description="[Admin] Declare the winner of a match")
@app_commands.describe(winner="The player to declare as winner", match_id="Match ID (optional if used inside the match thread)")
async def resolve_cmd(interaction: discord.Interaction, winner: discord.Member, match_id: int | None = None):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    match = await find_match_from_context(interaction, match_id)
    if not match:
        await interaction.response.send_message(
            "No pending match found. Provide a match ID or use this command inside a match thread.",
            ephemeral=True,
        )
        return

    winner_id = str(winner.id)
    is_doubles = match["game_mode"] == "doubles"
    all_player_ids = (
        (match["player1_id"], match["player2_id"], match["player3_id"], match["player4_id"])
        if is_doubles else (match["player1_id"], match["player2_id"])
    )
    if winner_id not in all_player_ids:
        await interaction.response.send_message(
            "That player is not part of this match.", ephemeral=True
        )
        return

    # Cancel any pending vote timer
    if match["message_id"]:
        timer = vote_timers.pop(int(match["message_id"]), None)
        if timer:
            timer.cancel()

    await interaction.response.send_message(
        f"**Match #{match['id']}** resolved by admin. Winner: **{winner.display_name}**"
    )

    # Resolve in the match thread if it exists
    thread = None
    if match["thread_id"]:
        try:
            thread = bot.get_channel(int(match["thread_id"]))
            if thread is None:
                thread = await bot.fetch_channel(int(match["thread_id"]))
        except discord.HTTPException:
            pass

    channel = thread or interaction.channel
    msg_id = int(match["message_id"]) if match["message_id"] else 0

    if is_doubles:
        winning_team = 1 if winner_id in (match["player1_id"], match["player2_id"]) else 2
        await resolve_doubles_match(match, winning_team, channel, msg_id)
    else:
        winner_emoji = REACT_P1 if winner_id == match["player1_id"] else REACT_P2
        await resolve_match(match, winner_emoji, channel, msg_id)


@tree.command(name="admin_cancel", description="[Admin] Cancel a match with no Elo change")
@app_commands.describe(match_id="Match ID (optional if used inside the match thread)")
async def admin_cancel_cmd(interaction: discord.Interaction, match_id: int | None = None):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    match = await find_match_from_context(interaction, match_id)
    if not match:
        await interaction.response.send_message(
            "No pending match found. Provide a match ID or use this command inside a match thread.",
            ephemeral=True,
        )
        return

    # Cancel any pending vote timer
    if match["message_id"]:
        timer = vote_timers.pop(int(match["message_id"]), None)
        if timer:
            timer.cancel()
        match_votes.pop(int(match["message_id"]), None)

    cancel_requests.pop(match["id"], None)

    if match["game_mode"] == "doubles":
        complete_doubles_match(
            match["id"], "cancelled",
            match["player1_elo_before"], match["player2_elo_before"],
            match["player3_elo_before"], match["player4_elo_before"],
        )
        desc = (f"<@{match['player1_id']}> & <@{match['player2_id']}> vs "
                f"<@{match['player3_id']}> & <@{match['player4_id']}>\nCancelled by admin. No Elo changes.")
    else:
        complete_match(
            match["id"], "cancelled",
            match["player1_elo_before"], match["player2_elo_before"],
        )
        desc = f"<@{match['player1_id']}> vs <@{match['player2_id']}>\nCancelled by admin. No Elo changes."

    await interaction.response.send_message(
        f"**Match #{match['id']}** has been cancelled by admin. No Elo changes applied."
    )

    cancel_embed = discord.Embed(
        title=f"Match #{match['id']} Cancelled",
        description=desc,
        color=discord.Color.light_grey(),
    )
    await log_to_match_channel(cancel_embed)

    # Archive the thread if it exists
    if match["thread_id"]:
        try:
            thread = bot.get_channel(int(match["thread_id"]))
            if thread is None:
                thread = await bot.fetch_channel(int(match["thread_id"]))
            await thread.send(f"**Match #{match['id']} cancelled by admin.** Thread will now close.")
            await thread.edit(archived=True, locked=True)
        except discord.HTTPException:
            pass


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # Ignore bot's own reactions
    if payload.user_id == bot.user.id:
        return

    emoji = str(payload.emoji)
    user_id = str(payload.user_id)

    # Handle doubles invite reactions (✅ / ❌)
    if emoji in (REACT_ACCEPT, REACT_DECLINE):
        inviter_id = invite_messages.get(payload.message_id)
        if not inviter_id:
            return
        invite = doubles_invites.get(inviter_id)
        if not invite or invite["partner_id"] != user_id:
            return

        channel = bot.get_channel(payload.channel_id)
        if channel is None:
            channel = await bot.fetch_channel(payload.channel_id)

        timer = invite_timers.pop(inviter_id, None)
        if timer:
            timer.cancel()
        doubles_invites.pop(inviter_id, None)
        invite_messages.pop(payload.message_id, None)

        if emoji == REACT_DECLINE:
            await channel.send(f"<@{user_id}> declined the doubles invite from <@{inviter_id}>.")
            return

        # Accept — add both to doubles queue as a team
        inviter = get_or_create_player(inviter_id, (await bot.fetch_user(int(inviter_id))).display_name)
        partner = get_or_create_player(user_id, (await bot.fetch_user(int(user_id))).display_name)
        inviter_r = get_or_create_doubles_rating(inviter_id, inviter["username"])
        partner_r = get_or_create_doubles_rating(user_id, partner["username"])
        team = get_or_create_team(inviter_id, user_id)

        inviter_d = dict(inviter, elo=inviter_r["elo"])
        partner_d = dict(partner, elo=partner_r["elo"])
        doubles_queue.add_team(inviter_d, partner_d, team["elo"])
        queue_channels[inviter_id] = invite["channel_id"]
        queue_channels[user_id] = invite["channel_id"]

        await channel.send(
            f"<@{inviter_id}> <@{user_id}> accepted! **{inviter['username']} & {partner['username']}** joined the doubles queue "
            f"(Team Elo: {team['elo']}). Players in doubles queue: {doubles_queue.queue_size()}"
        )
        await try_create_doubles_match(channel)
        return

    if emoji not in (REACT_P1, REACT_P2):
        return

    match = get_match_by_message(str(payload.message_id))
    if not match:
        return

    if payload.message_id not in match_votes:
        match_votes[payload.message_id] = {}
    votes = match_votes[payload.message_id]

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        channel = await bot.fetch_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)

    for reaction_emoji in (REACT_P1, REACT_P2):
        if reaction_emoji != emoji:
            await message.remove_reaction(reaction_emoji, discord.Object(id=payload.user_id))

    votes[user_id] = emoji

    # --- Doubles voting ---
    if match.get("game_mode") == "doubles":
        voter_team = get_player_team(match, user_id)
        if voter_team is None:
            return

        team1_votes = {did: v for did, v in votes.items() if get_player_team(match, did) == 1}
        team2_votes = {did: v for did, v in votes.items() if get_player_team(match, did) == 2}

        if team1_votes and team2_votes:
            timer = vote_timers.pop(payload.message_id, None)
            if timer:
                timer.cancel()
            t1_vote = next(iter(team1_votes.values()))
            t2_vote = next(iter(team2_votes.values()))
            if t1_vote == t2_vote:
                winning_team = 1 if t1_vote == REACT_P1 else 2
                await resolve_doubles_match(match, winning_team, channel, payload.message_id)
            else:
                await channel.send(
                    f"**Doubles Match #{match['id']} is disputed!** Teams voted for different winners.\n"
                    f"Change your reaction to agree, use `/cancel` to void the match, "
                    f"or contact the moderator team for assistance."
                )
        else:
            if voter_team == 1:
                other_mention = f"<@{match['player3_id']}> <@{match['player4_id']}>"
            else:
                other_mention = f"<@{match['player1_id']}> <@{match['player2_id']}>"
            await channel.send(
                f"{other_mention}, the opposing team has submitted their result. "
                f"You have **{get_vote_timeout() // 60} minute(s)** to react or their result will be accepted."
            )
            old_timer = vote_timers.pop(payload.message_id, None)
            if old_timer:
                old_timer.cancel()
            task = asyncio.create_task(
                doubles_vote_timeout(payload.message_id, payload.channel_id, str(match["id"]))
            )
            vote_timers[payload.message_id] = task
        return

    # --- Singles voting ---
    if user_id not in (match["player1_id"], match["player2_id"]):
        return

    other_id = match["player2_id"] if user_id == match["player1_id"] else match["player1_id"]

    if other_id in votes:
        timer = vote_timers.pop(payload.message_id, None)
        if timer:
            timer.cancel()
        if votes[user_id] == votes[other_id]:
            await resolve_match(match, emoji, channel, payload.message_id)
        else:
            await channel.send(
                f"**Match #{match['id']} is disputed!** "
                f"<@{match['player1_id']}> and <@{match['player2_id']}> selected different winners.\n"
                f"Change your reaction to agree, use `/cancel` to void the match, "
                f"or contact the moderator team for assistance."
            )
    else:
        await channel.send(
            f"<@{other_id}>, your opponent has submitted their result. "
            f"You have **{get_vote_timeout() // 60} minute(s)** to react with the winner's icon or their result will be accepted."
        )
        old_timer = vote_timers.pop(payload.message_id, None)
        if old_timer:
            old_timer.cancel()
        task = asyncio.create_task(
            vote_timeout(payload.message_id, payload.channel_id, str(match["id"]), user_id, other_id, emoji)
        )
        vote_timers[payload.message_id] = task


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    # Ignore bot's own reactions
    if payload.user_id == bot.user.id:
        return

    emoji = str(payload.emoji)
    if emoji not in (REACT_P1, REACT_P2):
        return

    votes = match_votes.get(payload.message_id)
    if votes is None:
        return

    user_id = str(payload.user_id)

    # Only clear if this was their current vote
    if votes.get(user_id) == emoji:
        del votes[user_id]

        # Cancel the timer since the vote was withdrawn
        timer = vote_timers.pop(payload.message_id, None)
        if timer:
            timer.cancel()


@tree.command(name="join_doubles", description="Join the doubles matchmaking queue")
@app_commands.describe(partner="Tag your partner to form a team (leave blank to queue solo)")
async def join_doubles(interaction: discord.Interaction, partner: discord.Member | None = None):
    if not is_matchmaking_channel(interaction):
        await interaction.response.send_message(WRONG_CHANNEL_MSG, ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    username = interaction.user.display_name

    if is_banned(discord_id):
        await interaction.response.send_message("You are banned from matchmaking.", ephemeral=True)
        return

    if queue.is_in_queue(discord_id):
        await interaction.response.send_message(
            "You are in the singles queue. Use `/leave` first.", ephemeral=True
        )
        return

    if doubles_queue.is_in_queue(discord_id):
        await interaction.response.send_message("You are already in the doubles queue.", ephemeral=True)
        return

    if discord_id in doubles_invites:
        await interaction.response.send_message(
            "You have a pending doubles invite. Use `/leave` to cancel it.", ephemeral=True
        )
        return

    for inv in doubles_invites.values():
        if inv["partner_id"] == discord_id:
            await interaction.response.send_message(
                "You have a pending doubles invite to respond to. Accept, decline, or use `/leave` to dismiss it.",
                ephemeral=True,
            )
            return

    pending = get_pending_match(discord_id)
    if pending:
        await interaction.response.send_message(
            f"You have an unfinished match (#{pending['id']}). Finish it or ask an admin to cancel it.",
            ephemeral=True,
        )
        return

    # Solo queue
    if partner is None:
        existing = get_player(discord_id)
        if existing and existing["username"] != username:
            update_player_username(discord_id, username)
        rating = get_or_create_doubles_rating(discord_id, username)
        player_d = dict(get_or_create_player(discord_id, username), elo=rating["elo"])
        doubles_queue.add_solo(player_d)
        queue_channels[discord_id] = interaction.channel_id
        await interaction.response.send_message(
            f"**{username}** joined the doubles queue solo! (Doubles Elo: {rating['elo']}) "
            f"Players in doubles queue: {doubles_queue.queue_size()}"
        )
        await try_create_doubles_match(interaction.channel)
        return

    # Team invite
    partner_id = str(partner.id)
    if partner_id == discord_id:
        await interaction.response.send_message("You cannot invite yourself.", ephemeral=True)
        return

    if is_banned(partner_id):
        await interaction.response.send_message(f"**{partner.display_name}** is banned from matchmaking.", ephemeral=True)
        return

    if queue.is_in_queue(partner_id) or doubles_queue.is_in_queue(partner_id):
        await interaction.response.send_message(
            f"**{partner.display_name}** is already in a queue.", ephemeral=True
        )
        return

    if partner_id in doubles_invites:
        await interaction.response.send_message(
            f"**{partner.display_name}** already has a pending invite out.", ephemeral=True
        )
        return

    for inv in doubles_invites.values():
        if inv["partner_id"] == partner_id:
            await interaction.response.send_message(
                f"**{partner.display_name}** already has a pending invite to respond to.", ephemeral=True
            )
            return

    pending_p = get_pending_match(partner_id)
    if pending_p:
        await interaction.response.send_message(
            f"**{partner.display_name}** has an unfinished match.", ephemeral=True
        )
        return

    timeout_mins = get_invite_timeout() // 60
    invite_embed = discord.Embed(
        title="Doubles Invite!",
        description=(
            f"**{username}** wants to team up with <@{partner_id}> for doubles!\n\n"
            f"React {REACT_ACCEPT} to accept or {REACT_DECLINE} to decline.\n"
            f"This invite expires in {timeout_mins} minute(s)."
        ),
        color=discord.Color.blue(),
    )
    await interaction.response.send_message(content=f"<@{partner_id}>", embed=invite_embed)
    msg = await interaction.original_response()
    await msg.add_reaction(REACT_ACCEPT)
    await msg.add_reaction(REACT_DECLINE)

    doubles_invites[discord_id] = {
        "partner_id": partner_id,
        "message_id": msg.id,
        "channel_id": interaction.channel_id,
    }
    invite_messages[msg.id] = discord_id
    task = asyncio.create_task(invite_timeout(discord_id))
    invite_timers[discord_id] = task


@tree.command(name="leaderboard_doubles", description="View the top players in doubles")
async def leaderboard_doubles_cmd(interaction: discord.Interaction):
    if not is_matchmaking_channel(interaction):
        await interaction.response.send_message(WRONG_CHANNEL_MSG, ephemeral=True)
        return

    top = get_doubles_leaderboard(10)
    if not top:
        await interaction.response.send_message("No doubles matches played yet!", ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    top_ids = {p["discord_id"] for p in top}
    lines = []
    for i, p in enumerate(top, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        marker = " ← You" if p["discord_id"] == discord_id else ""
        lines.append(f"{medal} **{p['username']}** - Elo: {p['elo']} ({p['wins']}W / {p['losses']}L){marker}")

    if discord_id not in top_ids:
        d_rating = get_doubles_rating(discord_id)
        if d_rating:
            rank = get_doubles_player_rank(discord_id)
            if rank:
                lines.append("...")
                lines.append(f"{rank[0]}. **{get_player(discord_id)['username']}** - Elo: {d_rating['elo']} "
                              f"({d_rating['wins']}W / {d_rating['losses']}L) ← You")

    embed = discord.Embed(title="Doubles Leaderboard", description="\n".join(lines), color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="leaderboard_teams", description="View the top teams")
async def leaderboard_teams_cmd(interaction: discord.Interaction):
    if not is_matchmaking_channel(interaction):
        await interaction.response.send_message(WRONG_CHANNEL_MSG, ephemeral=True)
        return

    top = get_team_leaderboard(10)
    if not top:
        await interaction.response.send_message("No team matches played yet!", ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    lines = []
    for i, t in enumerate(top, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} **{t['player1_username']} & {t['player2_username']}** - Elo: {t['elo']} ({t['wins']}W / {t['losses']}L)")

    embed = discord.Embed(title="Team Leaderboard", description="\n".join(lines), color=discord.Color.gold())

    my_teams = get_player_teams(discord_id, 5)
    if my_teams:
        team_lines = []
        for t in my_teams:
            partner_name = t["player2_username"] if t["player1_id"] == discord_id else t["player1_username"]
            rank = get_team_rank(t["player1_id"], t["player2_id"])
            rank_str = f"#{rank[0]}" if rank else "?"
            team_lines.append(f"{rank_str} w/ {partner_name} ({t['elo']})")
        embed.add_field(name="Your Teams", value="\n".join(team_lines), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="stats_team", description="View your team rating with a specific partner")
@app_commands.describe(partner="The player you teamed up with")
async def stats_team_cmd(interaction: discord.Interaction, partner: discord.Member):
    if not is_matchmaking_channel(interaction):
        await interaction.response.send_message(WRONG_CHANNEL_MSG, ephemeral=True)
        return

    discord_id = str(interaction.user.id)
    partner_id = str(partner.id)

    from database import get_team
    team = get_team(discord_id, partner_id)
    if not team:
        await interaction.response.send_message(
            f"No team record found for you and **{partner.display_name}**.", ephemeral=True
        )
        return

    rank = get_team_rank(discord_id, partner_id)
    rank_str = f"#{rank[0]} of {rank[1]}" if rank else "N/A"
    total = team["wins"] + team["losses"]
    wr = f"{team['wins'] / total * 100:.1f}%" if total > 0 else "N/A"
    me = get_player(discord_id)

    embed = discord.Embed(
        title=f"{me['username']} & {partner.display_name} — Team Stats",
        color=discord.Color.purple(),
    )
    embed.add_field(name="Team Rank", value=rank_str, inline=True)
    embed.add_field(name="Team Elo", value=str(team["elo"]), inline=True)
    embed.add_field(name="​", value="​", inline=True)
    embed.add_field(name="Wins", value=str(team["wins"]), inline=True)
    embed.add_field(name="Losses", value=str(team["losses"]), inline=True)
    embed.add_field(name="Win Rate", value=wr, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="set_invite_timeout", description="[Admin] Set how long a doubles invite stays open in seconds")
@app_commands.describe(seconds="Invite timeout in seconds (e.g. 600 for 10 minutes)")
async def set_invite_timeout_cmd(interaction: discord.Interaction, seconds: int):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return
    if seconds < 30:
        await interaction.response.send_message("Invite timeout must be at least 30 seconds.", ephemeral=True)
        return
    set_setting("invite_timeout", str(seconds))
    await interaction.response.send_message(
        f"Doubles invite timeout set to **{seconds} seconds** ({seconds // 60} min {seconds % 60} sec)."
    )


bot.run(TOKEN)
