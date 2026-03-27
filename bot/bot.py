import os

import discord
from discord import app_commands
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
)
from elo import calculate_new_ratings
from matchmaking import MatchmakingQueue, build_match_embed, pick_court, REACT_P1, REACT_P2

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

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


@bot.event
async def on_ready():
    init_db()
    await tree.sync()
    print(f"Bot is online as {bot.user}")


@tree.command(name="join", description="Join the matchmaking queue")
async def join_queue(interaction: discord.Interaction):
    discord_id = str(interaction.user.id)
    username = interaction.user.display_name

    # Update username in case it changed
    existing = get_player(discord_id)
    if existing and existing["username"] != username:
        update_player_username(discord_id, username)

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
    await interaction.response.send_message(
        f"**{username}** joined the queue! (Elo: {player['elo']}) "
        f"Players in queue: {queue.queue_size()}"
    )

    # Try to find a match
    result = queue.find_match()
    if result:
        p1, p2, match_id = result
        court = pick_court()
        embed = build_match_embed(p1, p2, match_id, court)

        # Create a private thread for the match
        thread = await interaction.channel.create_thread(
            name=f"Match #{match_id}: {p1['username']} vs {p2['username']}",
            type=discord.ChannelType.private_thread,
        )

        # Send the match embed in the thread and add reaction icons
        match_msg = await thread.send(
            f"<@{p1['discord_id']}> <@{p2['discord_id']}>", embed=embed
        )
        await match_msg.add_reaction(REACT_P1)
        await match_msg.add_reaction(REACT_P2)

        # Store thread and message IDs in the database
        set_match_thread(match_id, str(thread.id), str(match_msg.id))
        match_votes[match_msg.id] = {}

        # Notify in the main channel
        await interaction.channel.send(
            f"**Match #{match_id}** created! "
            f"<@{p1['discord_id']}> vs <@{p2['discord_id']}> \u2014 "
            f"check your private thread."
        )


@tree.command(name="leave", description="Leave the matchmaking queue")
async def leave_queue(interaction: discord.Interaction):
    discord_id = str(interaction.user.id)
    if queue.remove_player(discord_id):
        await interaction.response.send_message(
            f"**{interaction.user.display_name}** left the queue."
        )
    else:
        await interaction.response.send_message(
            "You are not in the queue.", ephemeral=True
        )


@tree.command(name="queue", description="See who is in the matchmaking queue")
async def view_queue(interaction: discord.Interaction):
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
    target = player or interaction.user
    discord_id = str(target.id)
    p = get_or_create_player(discord_id, target.display_name)

    total = p["wins"] + p["losses"]
    win_rate = f"{p['wins'] / total * 100:.1f}%" if total > 0 else "N/A"

    embed = discord.Embed(
        title=f"{p['username']}'s Stats",
        color=discord.Color.purple(),
    )
    embed.add_field(name="Elo", value=str(p["elo"]), inline=True)
    embed.add_field(name="Wins", value=str(p["wins"]), inline=True)
    embed.add_field(name="Losses", value=str(p["losses"]), inline=True)
    embed.add_field(name="Win Rate", value=win_rate, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="leaderboard", description="View the top players")
async def leaderboard(interaction: discord.Interaction):
    top = get_leaderboard(10)
    if not top:
        await interaction.response.send_message(
            "No players yet!", ephemeral=True
        )
        return

    lines = []
    for i, p in enumerate(top, 1):
        medal = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}.get(i, f"{i}.")
        lines.append(
            f"{medal} **{p['username']}** - Elo: {p['elo']} "
            f"({p['wins']}W / {p['losses']}L)"
        )

    embed = discord.Embed(
        title="Leaderboard - Mario Tennis",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="cancel", description="Request to cancel your pending match")
async def cancel_match(interaction: discord.Interaction):
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

    # Check if both players have voted
    if match["player1_id"] in votes and match["player2_id"] in votes:
        p1_vote = votes[match["player1_id"]]
        p2_vote = votes[match["player2_id"]]

        if p1_vote == p2_vote:
            # Both agree on a winner
            winner_id = match["player1_id"] if p1_vote == REACT_P1 else match["player2_id"]
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

            # Clean up
            match_votes.pop(payload.message_id, None)
            cancel_requests.pop(match["id"], None)

            # Archive and lock the thread
            try:
                await channel.send("**Match complete!** This thread will now close.")
                await channel.edit(archived=True, locked=True)
            except discord.HTTPException:
                pass
        else:
            # Dispute \u2014 players disagree
            await channel.send(
                f"**Match #{match['id']} is disputed!** "
                f"<@{match['player1_id']}> and <@{match['player2_id']}> selected different winners.\n"
                f"Change your reaction to agree, use `/cancel` to void the match, "
                f"or contact the moderator team for assistance."
            )


bot.run(TOKEN)
