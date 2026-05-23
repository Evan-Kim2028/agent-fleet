#!/usr/bin/env bash
# Pull agent-fleet, wire the Hermes plugin, install into the gateway venv, restart.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_VENV="${HERMES_VENV:-$HERMES_HOME/hermes-agent/venv}"
PLUGIN_LINK="$HERMES_HOME/plugins/cursor-fleet"
PLUGIN_SRC="$REPO_ROOT/integrations/hermes"

NO_RESTART=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-restart) NO_RESTART=1; shift ;;
    -h|--help)
      cat <<EOF
Usage: $(basename "$0") [--no-restart]

  git pull in agent-fleet
  pip install -e into the Hermes gateway venv
  symlink ~/.hermes/plugins/cursor-fleet -> integrations/hermes
  hermes gateway restart

Env:
  HERMES_HOME   default: ~/.hermes
  HERMES_VENV   default: \$HERMES_HOME/hermes-agent/venv
EOF
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

echo "==> Pull latest ($REPO_ROOT)"
git -C "$REPO_ROOT" pull --ff-only

echo "==> Install agent-fleet into Hermes venv"
PYTHON="$HERMES_VENV/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "Hermes venv not found at $HERMES_VENV" >&2
  echo "Set HERMES_VENV or install Hermes first." >&2
  exit 1
fi
if [[ -x "$HERMES_VENV/bin/pip" ]]; then
  "$HERMES_VENV/bin/pip" install -e "$REPO_ROOT[dev]" -q
elif command -v uv >/dev/null 2>&1; then
  uv pip install -e "$REPO_ROOT[dev]" --python "$PYTHON" -q
else
  "$PYTHON" -m pip install -e "$REPO_ROOT[dev]" -q
fi

echo "==> Link Hermes plugin"
mkdir -p "$HERMES_HOME/plugins"
if [[ -e "$PLUGIN_LINK" && ! -L "$PLUGIN_LINK" ]]; then
  echo "Removing stale plugin dir (replacing with symlink)"
  rm -rf "$PLUGIN_LINK"
fi
ln -sfn "$PLUGIN_SRC" "$PLUGIN_LINK"
echo "    $PLUGIN_LINK -> $PLUGIN_SRC"

if [[ "$NO_RESTART" -eq 1 ]]; then
  echo "==> Skipping gateway restart (--no-restart)"
  exit 0
fi

echo "==> Restart Hermes gateway"
systemctl --user reset-failed hermes-gateway.service 2>/dev/null || true
if systemctl --user is-active --quiet hermes-gateway.service; then
  echo "    stopping existing gateway (SIGKILL if drain hangs)"
  systemctl --user kill -s SIGKILL hermes-gateway.service 2>/dev/null || true
  sleep 2
fi
systemctl --user start hermes-gateway.service
sleep 3
systemctl --user status hermes-gateway.service --no-pager | sed -n '1,12p'

echo "==> Done"
