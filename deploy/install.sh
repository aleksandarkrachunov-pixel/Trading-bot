#!/usr/bin/env bash
# One-time setup on an always-on Linux machine (Ubuntu/Debian VPS, Raspberry Pi, home server).
#
#   git clone -b claude/jolly-franklin-a9obyc https://github.com/aleksandarkrachunov-pixel/Trading-bot.git
#   cd Trading-bot && ./deploy/install.sh
#
# Installs Docker if needed, asks for your API keys (creates .env), creates config.yaml
# from the Trading 212 example, and starts the bot. Safe to run again: it keeps
# existing .env, config.yaml and state/.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo "== Installing Docker"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"

if [ ! -f .env ]; then
  echo "== Trading 212 API key (Practice account: Settings -> API (Beta) -> Generate API key)"
  read -rsp "  API key: " key; echo
  read -rsp "  API secret: " secret; echo
  echo "== Telegram alerts (optional, press Enter to skip)"
  read -rsp "  Bot token from @BotFather: " tg_token; echo
  read -rp  "  Chat id: " tg_chat
  umask 077
  printf 'T212_API_KEY=%s\nT212_API_SECRET=%s\nTELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' \
    "$key" "$secret" "$tg_token" "$tg_chat" > .env
  echo "Saved .env"
else
  echo "== Keeping existing .env"
fi

if [ ! -f config.yaml ]; then
  cp config.trading212.example.yaml config.yaml
  echo "Created config.yaml (Trading 212 DEMO, stock scanner on)"
else
  echo "== Keeping existing config.yaml"
fi

mkdir -p state logs
if ls state/*.json >/dev/null 2>&1; then
  echo "== Found saved state (the bot will resume its position):"
  ls state/*.json
fi

echo "== Checking the Trading 212 connection"
$DOCKER compose build -q
$DOCKER compose run --rm bot t212-check

echo "== Starting the bot"
$DOCKER compose up -d
echo
echo "Running. Useful commands:"
echo "  $DOCKER compose logs -f            # watch it"
echo "  $DOCKER compose ps                 # status (healthy = state updated in the last 15 min)"
echo "  $DOCKER compose run --rm bot status"
echo "  $DOCKER compose stop               # stop, keeping the position"
echo "  git pull && $DOCKER compose up -d --build   # update"
