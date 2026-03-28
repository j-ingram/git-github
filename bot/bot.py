import asyncio
import os

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from database import (
    init_db,
    get_or_create_player,
    get_leaderboard,
    get_pending_match,
    complete_match,
    update_player_stats,
    get_player,
    update_player_username,
    set_match_thread,
    get_match_by_message,
    get_match_by_thread,
    get_match_by_id,
    reset_season,
    set_player_elo,
    ban_player,
    unban_player,
    is_banned,
    get_player_rank,
    set_setting,
    get_setting,
)
from elo import calculate_new_ratings
from matchmaking import MatchmakingQueue, build_match_embed, pick_court, REACT_P1, REACT_P2

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_IDS = set(os.getenv("ADMIN_IDS", "").split(","))
MATCH_LOG_CHANNEL = os.getenv("MATCH_LOG_CHANNEL")
MATCHMAKING_CHANNEL = os.getenv("MATCHMAKING_CHANNEL")


def is_admin(interaction: discord.Interaction) -> bool:
    return str(interaction.user.id) in ADMIN_IDS


def is_matchmaking_channel(interaction: discord.Interaction) -> bool:
    if not MATCHMAKING_CHANNEL:
        return True  # No restriction if not configured
    return str(interaction.channel_id) == MATCHMAKING_CHANNEL


WRONG_CHANNEL_MSG = f"This command can only be used in <#{MATCHMAKING_CHANNEL}>." if MATCHMAKING_CHANNEL else ""


intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)
queue = MatchmakingQueue()

# Track votes per match message: {message_id: {discord_id: emoji}}
match_votes: dict[int, dict[str, str]] = {}

# Track cancel requests per match: {match_id: set of discord_ids who requested cancel}
cancel_requests: dict[int, set[str]] = {}

# Track which channel each queued player joined from: {discord_id: channel_id}
queue_channels: dict[str, int] = {}

# Track vote timeout tasks: {message_id: asyncio.Task}
vote_timers: dict[int, asyncio.Task] = {}

VOTE_TIMEOUT = 300  # 5 minutes for the other player to vote


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


async def resolve_match(match: dict, winner_emoji: str, channel: discord.abc.Messageable, message_id: int):
    """Resolve a match based on the winning emoji. Updates Elo, sends result, and closes thread."""
    winner_id = match["player1_id"] if winner_emoji == REACT_P1 else match["player2_id"]
    loser_id = match["player2_id"] if winner_id == match["player1_id"] else match["player1_id"]

    winner_player = get_player(winner_id)
    loser_player = get_player(loser_id)

    new_winner_elo, new_loser_elo = calculate_new_ratings(
        winner_player["elo"], loser_player["elo"]
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
    await asyncio.sleep(VOTE_TIMEOUT)

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
            f"<@{other_id}> did not respond within 5 minutes. "
            f"<@{voter_id}>'s result has been accepted."
        )
        await resolve_match(match, voter_emoji, channel, message_id)


@bot.event
async def on_ready():
    init_db()
    await tree.sync()
    if not check_queue_matches.is_running():
        check_queue_matches.start()
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

    await thread.send(
        f"**How to play:**\n"
        f"1. One player creates a **private match** in Mario Tennis and shares the room code here\n"
        f"2. The other player joins using the room code\n"
        f"3. Play with the settings shown above: **{court}** court, **High** ball speed, **Classic** mode, **Quick Play**\n"
        f"4. After the match, both players react above with the winner's icon ({REACT_P1} or {REACT_P2})\n\n"
        f"**No-show rule:** If your opponent does not respond in this thread within 5 minutes, "
        f"report yourself as the winner by reacting above. If they don't dispute within 5 minutes, "
        f"the result will be accepted automatically."
    )

    set_match_thread(match_id, str(thread.id), str(match_msg.id))
    match_votes[match_msg.id] = {}

    await channel.send(
        f"**Match #{match_id}** created! "
        f"<@{p1['discord_id']}> vs <@{p2['discord_id']}> \u2014 "
        f"check your private thread."
    )
    return True


@tasks.loop(seconds=10)
async def check_queue_matches():
    """Periodically check the queue for matches that became valid after cooldowns expired."""
    if queue.queue_size() < 2:
        return

    # Collect unique channels from queued players
    channels_seen = set()
    for discord_id in list(queue.queue.keys()):
        ch_id = queue_channels.get(discord_id)
        if ch_id:
            channels_seen.add(ch_id)

    # Use the first available channel to create the match thread
    for ch_id in channels_seen:
        channel = bot.get_channel(ch_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(ch_id)
            except discord.HTTPException:
                continue
        # Keep creating matches until no more valid pairs exist
        while await try_create_match(channel):
            pass
        break


@check_queue_matches.before_loop
async def before_check_queue():
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
            "You are already in the queue!", ephemeral=True
        )
        return

    pending = get_pending_match(discord_id)
    if pending:
        await interaction.response.send_message(
            f"You have an unfinished match (#{pending['id']}). "
            "Finish your current match first.",
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
    if queue.remove_player(discord_id):
        queue_channels.pop(discord_id, None)
        await interaction.response.send_message(
            f"**{interaction.user.display_name}** left the queue."
        )
    else:
        await interaction.response.send_message(
            "You are not in the queue.", ephemeral=True
        )


@tree.command(name="queue", description="See who is in the matchmaking queue")
async def view_queue(interaction: discord.Interaction):
    if not is_matchmaking_channel(interaction):
        await interaction.response.send_message(WRONG_CHANNEL_MSG, ephemeral=True)
        return

    if queue.queue_size() == 0:
        await interaction.response.send_message(
            "The queue is empty.", ephemeral=True
        )
        return

    lines = []
    for i, p in enumerate(
        sorted(queue.queue.values(), key=lambda x: x["elo"], reverse=True), 1
    ):
        lines.append(f"{i}. **{p['username']}** (Elo: {p['elo']})")

    embed = discord.Embed(
        title="Matchmaking Queue",
        description="\n".join(lines),
        color=discord.Color.blue(),
    )
    embed.set_footer(text=f"{queue.queue_size()} player(s) waiting")
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

    embed = discord.Embed(
        title=f"{p['username']}'s Stats",
        color=discord.Color.purple(),
    )
    embed.add_field(name="Rank", value=f"#{rank} of {total_players}", inline=True)
    embed.add_field(name="Elo", value=str(p["elo"]), inline=True)
    embed.add_field(name="Wins", value=str(p["wins"]), inline=True)
    embed.add_field(name="Losses", value=str(p["losses"]), inline=True)
    embed.add_field(name="Win Rate", value=win_rate, inline=True)
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
    opponent_id = (
        pending["player2_id"]
        if discord_id == pending["player1_id"]
        else pending["player1_id"]
    )

    if match_id not in cancel_requests:
        cancel_requests[match_id] = set()

    cancel_requests[match_id].add(discord_id)

    if len(cancel_requests[match_id]) >= 2:
        # Both players agreed to cancel
        complete_match(
            match_id,
            "cancelled",
            pending["player1_elo_before"],
            pending["player2_elo_before"],
        )

        # Clean up tracking
        if pending["message_id"]:
            match_votes.pop(int(pending["message_id"]), None)
        cancel_requests.pop(match_id, None)

        await interaction.response.send_message(
            f"Match #{match_id} has been cancelled by both players. No Elo changes applied."
        )

        # Log cancellation to match history channel
        cancel_embed = discord.Embed(
            title=f"Match #{match_id} Cancelled",
            description=f"<@{pending['player1_id']}> vs <@{pending['player2_id']}>\nCancelled by both players. No Elo changes.",
            color=discord.Color.light_grey(),
        )
        await log_to_match_channel(cancel_embed)

        # Archive the thread if it exists
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
        # First player to request cancel \u2014 notify opponent
        await interaction.response.send_message(
            f"<@{discord_id}> wants to cancel **Match #{match_id}**.\n"
            f"<@{opponent_id}>, type `/cancel` to agree.\n"
            f"If you disagree, the match will be flagged for moderator review."
        )


@tree.command(name="reset_season", description="[Admin] Reset all players' Elo to 1000 and clear win/loss records")
async def reset_season_cmd(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    count = reset_season()
    await interaction.response.send_message(
        f"Season has been reset! {count} player(s) reset to 1000 Elo with 0 wins/losses."
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
    if winner_id not in (match["player1_id"], match["player2_id"]):
        await interaction.response.send_message(
            "That player is not part of this match.", ephemeral=True
        )
        return

    # Determine winner emoji to reuse resolve_match
    winner_emoji = REACT_P1 if winner_id == match["player1_id"] else REACT_P2

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
    await resolve_match(match, winner_emoji, channel, int(match["message_id"]) if match["message_id"] else 0)


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

    complete_match(
        match["id"],
        "cancelled",
        match["player1_elo_before"],
        match["player2_elo_before"],
    )

    await interaction.response.send_message(
        f"**Match #{match['id']}** has been cancelled by admin. No Elo changes applied."
    )

    # Log cancellation to match history channel
    cancel_embed = discord.Embed(
        title=f"Match #{match['id']} Cancelled",
        description=f"<@{match['player1_id']}> vs <@{match['player2_id']}>\nCancelled by admin. No Elo changes.",
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
    if emoji not in (REACT_P1, REACT_P2):
        return

    # Look up the match by message ID
    match = get_match_by_message(str(payload.message_id))
    if not match:
        return

    user_id = str(payload.user_id)

    # Only match participants can vote
    if user_id not in (match["player1_id"], match["player2_id"]):
        return

    # Initialize vote tracking for this message if needed
    if payload.message_id not in match_votes:
        match_votes[payload.message_id] = {}

    votes = match_votes[payload.message_id]

    # Remove any previous reaction by this user from the message
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        channel = await bot.fetch_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)

    for reaction_emoji in (REACT_P1, REACT_P2):
        if reaction_emoji != emoji:
            await message.remove_reaction(reaction_emoji, discord.Object(id=payload.user_id))

    # Record this player's vote
    votes[user_id] = emoji

    # Determine the other player
    other_id = match["player2_id"] if user_id == match["player1_id"] else match["player1_id"]

    # Check if both players have voted
    if other_id in votes:
        # Cancel any pending timeout since both players have voted
        timer = vote_timers.pop(payload.message_id, None)
        if timer:
            timer.cancel()

        if votes[user_id] == votes[other_id]:
            # Both agree on a winner
            await resolve_match(match, emoji, channel, payload.message_id)
        else:
            # Dispute — players disagree
            await channel.send(
                f"**Match #{match['id']} is disputed!** "
                f"<@{match['player1_id']}> and <@{match['player2_id']}> selected different winners.\n"
                f"Change your reaction to agree, use `/cancel` to void the match, "
                f"or contact the moderator team for assistance."
            )
    else:
        # First vote — ping the other player and start 5-minute timer
        await channel.send(
            f"<@{other_id}>, your opponent has submitted their result. "
            f"You have **5 minutes** to react with the winner's icon or their result will be accepted."
        )

        # Cancel any existing timer for this message (in case of re-vote)
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


bot.run(TOKEN)
