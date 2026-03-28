# Mario Tennis Matchmaking Bot

A Discord bot that handles player matchmaking for Mario Tennis using an Elo rating system.

## Features

- **Queue-based matchmaking** — Players join a queue and are matched with the closest-rated opponent
- **Elo rating system** — Chess-style Elo ratings (K-factor: 32, default rating: 1000)
- **Private match threads** — Each match gets its own private thread for isolated communication
- **Reaction-based reporting** — Players react with the winner's icon (🍄 or ⭐) to report results
- **Dispute handling** — Conflicting votes flag a dispute; admins can `/resolve` or `/admin_cancel`
- **Rematch cooldown** — 60-second cooldown prevents the same pair from being matched repeatedly, starting from when they join the queue
- **Auto-matching** — Background task checks the queue every 10 seconds and pairs players once cooldowns expire
- **Random court selection** — A random court is assigned to each match (Grass, Hard, Clay, Wood, Brick, Carpet, Sand, Forest)
- **Match cancellation** — Both players must agree to cancel a match (no Elo change)
- **No-show protection** — When one player votes, the other has 5 minutes to respond or the result is accepted automatically
- **Match instructions** — Private threads include step-by-step instructions for setting up the game and the no-show rule
- **Admin commands** — Owner/admin-only commands for season resets, Elo adjustments, player bans, and dispute resolution
- **Leaderboard** — See the top-ranked players

## Commands

### Player Commands

| Command | Description | Visibility |
|---------|-------------|------------|
| `/join` | Join the matchmaking queue | Public |
| `/leave` | Leave the queue | Public |
| `/queue` | See who is currently in the queue | Ephemeral |
| `/cancel` | Cancel your pending match (both players must agree) | Public |
| `/stats [player]` | View your or another player's stats | Ephemeral |
| `/leaderboard` | View the top 10 players | Ephemeral |

### Admin Commands

| Command | Description | Visibility |
|---------|-------------|------------|
| `/reset_season` | Reset all players' Elo to 1000 and clear win/loss records | Public |
| `/set_elo @player <elo>` | Set a player's Elo to a specific value | Public |
| `/ban @player [reason]` | Ban a player from joining the matchmaking queue | Public |
| `/unban @player` | Unban a player from matchmaking | Public |
| `/resolve @winner [match_id]` | Declare the winner of a disputed match | Public |
| `/admin_cancel [match_id]` | Cancel a match with no Elo change | Public |

`/resolve` and `/admin_cancel` auto-detect the match when used inside a match thread. When used from any other channel, provide the match ID.

Admin commands require your Discord user ID to be listed in `ADMIN_IDS` in the `.env` file.

## Setup

### 1. Create a Discord Bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a new application
3. Go to **Bot** tab and create a bot
4. Copy the bot token
5. Go to **OAuth2 > URL Generator**, select `bot` and `applications.commands` scopes
6. Select permissions: **Send Messages**, **Embed Links**, **Mention Everyone**, **Manage Messages**, **Create Private Threads**, **Send Messages in Threads**
7. Use the generated URL to invite the bot to your server

### 2. Configure the .env File

```
DISCORD_TOKEN=your-bot-token-here
ADMIN_IDS=208693717563998209
```

- `DISCORD_TOKEN` — Your bot token from the Discord Developer Portal
- `ADMIN_IDS` — Comma-separated list of Discord user IDs that can use admin commands (e.g. `208693717563998209,123456789`)

To find your Discord user ID: enable **Developer Mode** in Discord Settings > Advanced, then right-click your username and select **Copy User ID**.

### 3. Local Development (Windows)

```bash
cd bot
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and add your DISCORD_TOKEN and ADMIN_IDS
python bot.py
```

### 4. Local Development (macOS/Linux)

```bash
cd bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and add your DISCORD_TOKEN and ADMIN_IDS
python bot.py
```

### 5. Deploy to a VPS (Linode/DigitalOcean/etc.)

```bash
# SSH into your server and run as root:
curl -O https://raw.githubusercontent.com/j-ingram/git-github/claude/discord-matchmaking-bot-CHsYt/bot/deploy/setup.sh
chmod +x setup.sh
sudo ./setup.sh

# Then edit the .env file with your token and admin IDs:
sudo nano /opt/mario-tennis-bot/bot/.env

# Start the bot:
sudo systemctl start mario-tennis-bot
```

#### Useful VPS Commands

```bash
sudo systemctl status mario-tennis-bot    # Check bot status
sudo systemctl restart mario-tennis-bot   # Restart the bot
sudo journalctl -u mario-tennis-bot -f    # View live logs
```

## How Matchmaking Works

1. Players use `/join` to enter the queue
2. When 2+ players are in the queue, the bot matches the two with the closest Elo
3. If the best match is a recent rematch, the bot waits 60 seconds (from queue join time) before pairing them, giving priority to fresh matchups
4. A background task checks the queue every 10 seconds for newly valid matches
5. A private thread is created with match instructions, game settings, and the no-show rule
6. Players arrange a private match in Mario Tennis using the settings shown (court, ball speed, mode, match length)
7. After playing, both players react on the match embed with the winner's icon (🍄 or ⭐)
8. If both players agree, the match is resolved and Elo is updated
9. If players disagree, the match is flagged as disputed — an admin can use `/resolve` or `/admin_cancel`
10. If only one player votes, the other has 5 minutes to respond or the result is accepted by default

## Elo System

- Starting Elo: **1000**
- K-factor: **32**
- Beating a higher-rated player gains more Elo; beating a lower-rated player gains less

## Database

The bot uses SQLite (`matchmaking.db`) for persistent storage. The database is created automatically on first run.

**Note:** If you update the bot and the database schema has changed (new tables or columns), you need to delete `matchmaking.db` so it can be recreated. This only applies to schema changes, not regular code updates.
