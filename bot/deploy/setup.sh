#!/bin/bash
# Deployment script for Mario Tennis Matchmaking Bot
# Run as root on a fresh Ubuntu/Debian server (e.g. Linode)

set -e

APP_DIR="/opt/mario-tennis-bot"

echo "=== Installing system dependencies ==="
apt-get update
apt-get install -y python3 python3-venv python3-pip git

echo "=== Creating bot user ==="
if ! id -u botuser &>/dev/null; then
    useradd -r -s /bin/false botuser
fi

echo "=== Setting up application directory ==="
mkdir -p "$APP_DIR"
cd "$APP_DIR"

echo "=== Cloning repository ==="
if [ -d "$APP_DIR/.git" ]; then
    git pull
else
    git clone https://github.com/j-ingram/git-github.git "$APP_DIR"
fi

echo "=== Creating virtual environment ==="
python3 -m venv venv
source venv/bin/activate
pip install -r bot/requirements.txt

echo "=== Setting up .env file ==="
if [ ! -f bot/.env ]; then
    cp bot/.env.example bot/.env
    echo "IMPORTANT: Edit $APP_DIR/bot/.env and add your DISCORD_TOKEN"
fi

echo "=== Setting permissions ==="
chown -R botuser:botuser "$APP_DIR"

echo "=== Installing systemd service ==="
cp bot/deploy/mario-tennis-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable mario-tennis-bot

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. Edit $APP_DIR/bot/.env and set your DISCORD_TOKEN and ADMIN_IDS"
echo "  2. Start the bot: systemctl start mario-tennis-bot"
echo "  3. Check status: systemctl status mario-tennis-bot"
echo "  4. View logs: journalctl -u mario-tennis-bot -f"
