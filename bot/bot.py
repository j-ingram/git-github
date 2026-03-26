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
)
from elo import calculate_new_ratings
from matchmaking import MatchmakingQueue, build_match_embed

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)
queue = MatchmakingQueue()


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
            "Use `/report` to report the result first.",
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
        embed = build_match_embed(p1, p2, match_id)
        await interaction.channel.send(
            f"<@{p1['discord_id']}> <@{p2['discord_id']}>", embed=embed
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
        await interaction.response.send_message("The queue is empty.")
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
    await interaction.response.send_message(embed=embed)


@tree.command(name="report", description="Report a match result")
@app_commands.describe(match_id="The match ID", winner="The player who won")
async def report_match(
    interaction: discord.Interaction,
    match_id: int,
    winner: discord.Member,
):
    discord_id = str(interaction.user.id)
    winner_id = str(winner.id)

    pending = get_pending_match(discord_id)
    if not pending or pending["id"] != match_id:
        await interaction.response.send_message(
            "You don't have a pending match with that ID.", ephemeral=True
        )
        return

    if winner_id not in (pending["player1_id"], pending["player2_id"]):
        await interaction.response.send_message(
            "The winner must be one of the match participants.", ephemeral=True
        )
        return

    loser_id = (
        pending["player2_id"]
        if winner_id == pending["player1_id"]
        else pending["player1_id"]
    )

    winner_player = get_player(winner_id)
    loser_player = get_player(loser_id)

    new_winner_elo, new_loser_elo = calculate_new_ratings(
        winner_player["elo"], loser_player["elo"]
    )

    # Determine elo_after values in match column order (player1, player2)
    if winner_id == pending["player1_id"]:
        p1_elo_after, p2_elo_after = new_winner_elo, new_loser_elo
    else:
        p1_elo_after, p2_elo_after = new_loser_elo, new_winner_elo

    complete_match(match_id, winner_id, p1_elo_after, p2_elo_after)
    update_player_stats(winner_id, new_winner_elo, won=True)
    update_player_stats(loser_id, new_loser_elo, won=False)

    winner_delta = new_winner_elo - winner_player["elo"]
    loser_delta = new_loser_elo - loser_player["elo"]

    embed = discord.Embed(
        title=f"Match #{match_id} Result",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Winner",
        value=f"**{winner_player['username']}** {winner_player['elo']} -> {new_winner_elo} (+{winner_delta})",
        inline=False,
    )
    embed.add_field(
        name="Loser",
        value=f"**{loser_player['username']}** {loser_player['elo']} -> {new_loser_elo} ({loser_delta})",
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


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
    await interaction.response.send_message(embed=embed)


@tree.command(name="leaderboard", description="View the top players")
async def leaderboard(interaction: discord.Interaction):
    top = get_leaderboard(10)
    if not top:
        await interaction.response.send_message("No players yet!")
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
    await interaction.response.send_message(embed=embed)


@tree.command(name="cancel", description="Cancel your pending match (both players must agree)")
async def cancel_match(interaction: discord.Interaction):
    discord_id = str(interaction.user.id)
    pending = get_pending_match(discord_id)
    if not pending:
        await interaction.response.send_message(
            "You don't have a pending match.", ephemeral=True
        )
        return

    # Cancel by completing with no winner (set elo_after = elo_before)
    complete_match(
        pending["id"],
        "cancelled",
        pending["player1_elo_before"],
        pending["player2_elo_before"],
    )
    await interaction.response.send_message(
        f"Match #{pending['id']} has been cancelled. No Elo changes applied."
    )


bot.run(TOKEN)
