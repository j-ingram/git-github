# Mario Tennis Matchmaking Bot

A Discord bot that handles player matchmaking for Mario Tennis using an Elo rating system.

## Features

- **Queue-based matchmaking** - Players join a queue and are matched with the closest-rated opponent
- **Elo rating system** - Chess-style Elo ratings (K-factor: 32, default rating: 1000)
- **Match tracking** - Full match history with Elo changes
- **Leaderboard** - See the top-ranked players

## Commands

| Command | Description |
|---------|-------------|
| `/join` | Join the matchmaking queue (visible to all) |
| `/leave` | Leave the queue (visible to all) |
| `/queue` | See who is currently in the queue (ephemeral) |
| `/cancel` | Cancel your pending match (no Elo change) |
| `/stats [player]` | View your or another player's stats (ephemeral) |
| `/leaderboard` | View the top 10 players (ephemeral) |

## Setup

### 1. Create a Discord Bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a new application
3. Go to **Bot** tab and create a bot
4. Copy the bot token
5. Go to **OAuth2 > URL Generator**, select `bot` and `applications.commands` scopes
6. Select permissions: Send Messages, Embed Links, Mention Everyone
7. Use the generated URL to invite the bot to your server

### 2. Local Development

```bash
cd bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and add your DISCORD_TOKEN
python bot.py
```

### 3. Deploy to a VPS (Linode/DigitalOcean/etc.)

```bash
# SSH into your server and run as root:
curl -O https://raw.githubusercontent.com/j-ingram/git-github/claude/discord-matchmaking-bot-CHsYt/bot/deploy/setup.sh
chmod +x setup.sh
sudo ./setup.sh

# Then edit the .env file with your token:
sudo nano /opt/mario-tennis-bot/bot/.env

# Start the bot:
sudo systemctl start mario-tennis-bot
```

## How Matchmaking Works

1. Players use `/join` to enter the queue
2. When 2+ players are in the queue, the bot matches the two with the closest Elo
3. A private thread is created for the matched players
4. After playing, both players react on the match embed with the winner's icon
5. If both players agree, the match is resolved and Elo is updated
6. If players disagree, the match is flagged as disputed

## Elo System

- Starting Elo: **1000**
- K-factor: **32**
- Beating a higher-rated player gains more Elo; beating a lower-rated player gains less
