#!/usr/bin/env bash
#
# Entrypoint for the Bedrock Dedicated Server container.
#
# Runs TWO processes in this one container (kept together deliberately so
# the admin panel can access /data and the server's console directly,
# without needing a second Railway service + a shared volume, which
# Railway does not support safely across services):
#
#   1. bedrock_server   — the actual Minecraft server (UDP 19132)
#   2. panel/app.py      — Flask admin panel (HTTP, port $PORT / 8080)
#
# Responsibilities:
#   1. Make sure /data (the Railway Volume) exists.
#   2. Refresh the server ENGINE (binary + stock packs) from the image into
#      /data on every start — these are program files, not user data, so
#      it's safe (and desirable) to always match this image's BDS_VERSION.
#   3. Apply default SETTINGS (server.properties, permissions.json,
#      allowlist.json) into /data ONLY if they don't already exist — so
#      admin edits and the world are never touched on restart/redeploy.
#   4. Write eula.txt from the EULA env var (must be re-affirmed every run).
#   5. Record this run in version_history.json (for the panel's version list).
#   6. Set up a stdin FIFO + console log file so the panel can send commands
#      (e.g. "list", "stop") to bedrock_server and read its responses.
#   7. Launch the admin panel in the background.
#   8. Run bedrock_server in the foreground so Railway sees its logs and
#      restarts the container if it exits (see railway.json — ALWAYS).

set -euo pipefail

DATA_DIR="/data"
ENGINE_SRC="/app/bds-install"
DEFAULTS_DIR="/app/defaults"
VERSION_FILE="${DATA_DIR}/.bds_version"
VERSION_HISTORY_FILE="${DATA_DIR}/version_history.json"
STARTED_AT_FILE="${DATA_DIR}/.started_at"
STDIN_FIFO="${DATA_DIR}/bds_stdin"
LOG_DIR="${DATA_DIR}/logs"
CONSOLE_LOG="${LOG_DIR}/console.log"

echo "==> [1/8] Ensuring ${DATA_DIR} exists"
mkdir -p "${DATA_DIR}" "${DATA_DIR}/worlds" "${LOG_DIR}"

echo "==> [2/8] Syncing server engine (BDS ${BDS_VERSION}) into ${DATA_DIR}"
ENGINE_ITEMS=(
  "bedrock_server"
  "behavior_packs"
  "resource_packs"
  "definitions"
  "structures"
  "treatments"
  "valid_known_packs.json"
)
for item in "${ENGINE_ITEMS[@]}"; do
  if [ -e "${ENGINE_SRC}/${item}" ]; then
    rm -rf "${DATA_DIR:?}/${item}"
    cp -r "${ENGINE_SRC}/${item}" "${DATA_DIR}/${item}"
  fi
done
find "${ENGINE_SRC}" -maxdepth 1 -name "*.so*" -exec cp -f {} "${DATA_DIR}/" \; 2>/dev/null || true
chmod +x "${DATA_DIR}/bedrock_server"
echo "${BDS_VERSION}" > "${VERSION_FILE}"

echo "==> [3/8] Applying default settings (only if missing)"
if [ ! -f "${DATA_DIR}/server.properties" ]; then
  cp "${DEFAULTS_DIR}/server.properties" "${DATA_DIR}/server.properties"
  echo "    server.properties created from defaults."
else
  echo "    server.properties already exists — leaving it untouched."
fi
[ -f "${DATA_DIR}/permissions.json" ] || echo "[]" > "${DATA_DIR}/permissions.json"
[ -f "${DATA_DIR}/allowlist.json" ]   || echo "[]" > "${DATA_DIR}/allowlist.json"

echo "==> [4/8] Writing eula.txt from the EULA environment variable"
if [ "${EULA:-}" != "TRUE" ] && [ "${EULA:-}" != "true" ]; then
  echo "ERROR: You must accept the Minecraft EULA to run this server."
  echo "Set the Railway environment variable EULA=TRUE (see"
  echo "https://www.minecraft.net/en-us/eula) and redeploy."
  exit 1
fi
{
  echo "# Generated automatically from the EULA env var at container start — do not edit by hand."
  echo "eula=true"
} > "${DATA_DIR}/eula.txt"

echo "==> [5/8] Recording this run in version_history.json"
python3 - "$VERSION_HISTORY_FILE" "$BDS_VERSION" << 'EOPY'
import json, sys
from datetime import datetime, timezone

path, version = sys.argv[1], sys.argv[2]
now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

try:
    with open(path, "r") as f:
        history = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    history = []

found = False
for entry in history:
    if entry.get("version") == version:
        entry["last_seen"] = now
        found = True
        break
if not found:
    history.append({"version": version, "first_seen": now, "last_seen": now})

with open(path, "w") as f:
    json.dump(history, f, ensure_ascii=False, indent=2)
EOPY

date +%s > "${STARTED_AT_FILE}"

echo "==> [6/8] Setting up console FIFO for the admin panel"
[ -p "${STDIN_FIFO}" ] || mkfifo "${STDIN_FIFO}"
: > "${CONSOLE_LOG}"
# Keep a permanent reader/writer open on the FIFO so it never sees EOF
# between commands sent by the panel. FD 3 stays open for the lifetime of
# this script and is what bedrock_server's stdin is attached to below.
exec 3<>"${STDIN_FIFO}"

echo "==> [7/8] Starting admin panel in the background"
PANEL_PID=""
if [ -n "${ADMIN_PASSWORD:-}" ]; then
  BDS_VERSION="${BDS_VERSION}" PORT="${PORT:-8080}" python3 /app/panel/app.py >> "${LOG_DIR}/panel.log" 2>&1 &
  PANEL_PID=$!
  echo "    Panel started (pid ${PANEL_PID}) on port ${PORT:-8080}. Logs: ${LOG_DIR}/panel.log"
else
  echo "    ADMIN_PASSWORD not set — admin panel NOT started (this is intentional; see README)."
fi

cd "${DATA_DIR}"
export LD_LIBRARY_PATH=.

cleanup() {
  echo "==> Caught shutdown signal, stopping child processes..."
  if [ -n "${SERVER_PID:-}" ]; then
    kill -TERM "${SERVER_PID}" 2>/dev/null || true
    wait "${SERVER_PID}" 2>/dev/null || true
  fi
  if [ -n "${PANEL_PID}" ]; then
    kill "${PANEL_PID}" 2>/dev/null || true
  fi
}
trap cleanup TERM INT

echo "==> [8/8] Starting bedrock_server (console log: ${CONSOLE_LOG})"
# stdin comes from FD 3 (the FIFO, kept open by this script — see above);
# stdout/stderr go to Railway logs AND to console.log (for the panel).
# NOTE: this uses process substitution (not a `| tee` pipeline) on purpose
# so that $! below is bedrock_server's own PID, not tee's — otherwise
# TERM/INT from Railway would stop the wrong process and the server
# wouldn't get a chance to save the world before shutdown.
./bedrock_server <&3 > >(tee -a "${CONSOLE_LOG}") 2>&1 &
SERVER_PID=$!
wait "${SERVER_PID}"
EXIT_CODE=$?
echo "==> bedrock_server exited with code ${EXIT_CODE}"
[ -n "${PANEL_PID}" ] && kill "${PANEL_PID}" 2>/dev/null || true
exit "${EXIT_CODE}"
