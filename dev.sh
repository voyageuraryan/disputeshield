#!/usr/bin/env bash
# DisputeShield local launcher: Cloudflare quick tunnel + backend (FastAPI) + frontend (Vite).
#
#   ./dev.sh              start everything (Ctrl+C stops everything)
#   ./dev.sh --reload     same, backend auto-reloads on code edits
#   ./dev.sh --quiet      don't stream backend log lines into this terminal
#   ./dev.sh stop         stop everything started by this script
#   ./dev.sh status       what's running, current tunnel URL, PayPal webhook target
#   ./dev.sh url          print the PayPal Return URL you need right now
#   ./dev.sh logs [backend|frontend|tunnel]   follow a log (default: backend)
#
# Tunnel URL handling (quick-tunnel URLs change on every cloudflared start):
#   1. backend/.env AUTH_REDIRECT_URI is rewritten automatically
#   2. the PayPal webhook is re-pointed automatically (register_webhook --write-env)
#   3. the PayPal app's Return URL is the ONLY manual step: the script prints it in a
#      banner, sends a desktop notification and writes it to .run/PAYPAL_RETURN_URL.txt
# If cloudflared dies or the public URL stops answering, the tunnel is restarted, steps 1-2
# are redone, the backend is restarted and you're told the new Return URL.
# Secrets in backend/.env are never printed; only the AUTH_REDIRECT_URI line is edited.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
RUN="$ROOT/.run"
ENV_FILE="$BACKEND/.env"
PY="$BACKEND/.venv/bin/python"
URL_FILE="$RUN/tunnel_url"
RETURN_FILE="$RUN/PAYPAL_RETURN_URL.txt"
HISTORY="$RUN/tunnel_history.log"
API_PORT=8000
WEB_PORT=5173
CALLBACK_PATH="/api/auth/paypal/callback"
WEBHOOK_PATH="/webhooks/paypal"
PROCS=(tunnel backend frontend)

RELOAD=""
QUIET=0
CMD="start"
for a in "$@"; do
  case "$a" in
    --reload) RELOAD="--reload" ;;
    --quiet) QUIET=1 ;;
    start|stop|status|url|logs) CMD="$a" ;;
    backend|frontend|tunnel) LOG_TARGET="$a" ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $a (try --help)"; exit 2 ;;
  esac
done

mkdir -p "$RUN"

# ---------- output ----------
if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'; else B= G= Y= R= C= N=; fi
ts()   { date +%H:%M:%S; }
info() { echo "${C}[$(ts)]${N} $*"; }
ok()   { echo "${G}[$(ts)] ✔${N} $*"; }
warn() { echo "${Y}[$(ts)] !${N} $*"; }
fail() { echo "${R}[$(ts)] ✘${N} $*" >&2; }
notify() { command -v notify-send >/dev/null && notify-send -u critical -a DisputeShield "$1" "$2" 2>/dev/null || true; }

# ---------- process helpers ----------
pid_of()   { [ -f "$RUN/$1.pid" ] && cat "$RUN/$1.pid"; }
alive()    { local p; p=$(pid_of "$1") && [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }
port_busy(){ ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }

start_proc() {  # name dir cmd...  -> own session/process group, output to .run/<name>.log
  local name=$1 dir=$2; shift 2
  ( cd "$dir" && exec setsid "$@" ) >"$RUN/$name.log" 2>&1 < /dev/null &
  echo $! > "$RUN/$name.pid"
}

stop_proc() {
  local name=$1 p i
  p=$(pid_of "$name") || return 0
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
    kill -TERM -- "-$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null
    for i in $(seq 1 25); do kill -0 "$p" 2>/dev/null || break; sleep 0.2; done
    kill -KILL -- "-$p" 2>/dev/null || true
  fi
  rm -f "$RUN/$name.pid"
}

wait_http() {  # url seconds
  local i
  for i in $(seq 1 "$2"); do curl -fsS -m 3 -o /dev/null "$1" 2>/dev/null && return 0; sleep 1; done
  return 1
}

# ---------- tunnel URL handling ----------
env_url() { grep -m1 '^AUTH_REDIRECT_URI=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | sed "s|$CALLBACK_PATH\$||"; }

start_tunnel() {  # prints the URL on stdout
  start_proc tunnel "$ROOT" cloudflared tunnel --no-autoupdate --url "http://localhost:$API_PORT"
  local i url
  for i in $(seq 1 60); do
    url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$RUN/tunnel.log" 2>/dev/null | grep -v '^https://api\.' | tail -1)
    [ -n "$url" ] && { echo "$url"; return 0; }
    alive tunnel || break
    sleep 1
  done
  return 1
}

set_env_redirect() {
  local cb="$1$CALLBACK_PATH"
  if grep -q '^AUTH_REDIRECT_URI=' "$ENV_FILE"; then
    sed -i "s|^AUTH_REDIRECT_URI=.*|AUTH_REDIRECT_URI=$cb|" "$ENV_FILE"
  else
    printf '\nAUTH_REDIRECT_URI=%s\n' "$cb" >> "$ENV_FILE"
  fi
}

repoint_webhook() {
  local out
  if out=$(cd "$BACKEND" && "$PY" -m app.scripts.register_webhook --url "$1$WEBHOOK_PATH" --write-env 2>&1); then
    ok "PayPal webhook → $1$WEBHOOK_PATH ($(echo "$out" | head -1 | cut -d: -f1))"
  else
    warn "Could not re-point the PayPal webhook (disputes still arrive via the poller, ~1-3 min):"
    echo "$out" | tail -3 | sed 's/^/      /'
  fi
}

apply_url() {  # url -> updates .env + webhook, announces the Return URL
  local url=$1 prev
  prev=$(env_url)
  set_env_redirect "$url"
  repoint_webhook "$url"
  echo "$url" > "$URL_FILE"
  echo "$url$CALLBACK_PATH" > "$RETURN_FILE"
  echo "$(date -Is) $url" >> "$HISTORY"
  if [ "$prev" != "$url" ]; then
    echo
    echo "${Y}${B}╔══════════════════════════════════════════════════════════════════════════════╗${N}"
    echo "${Y}${B}  TUNNEL URL CHANGED: update the PayPal Return URL (the only manual step)${N}"
    echo "${Y}${B}╚══════════════════════════════════════════════════════════════════════════════╝${N}"
    echo "  developer.paypal.com → Apps & Credentials → Sandbox → your app → Log in with PayPal"
    echo "  → Advanced settings → Return URL → replace it with:"
    echo
    echo "      ${B}$url$CALLBACK_PATH${N}"
    echo
    echo "  → Save.  (Also saved in .run/PAYPAL_RETURN_URL.txt; ./dev.sh url prints it again.)"
    echo "  Already done for you: backend/.env AUTH_REDIRECT_URI, PayPal webhook URL."
    echo
    notify "PayPal Return URL changed" "Set it to: $url$CALLBACK_PATH"
  else
    ok "Tunnel URL unchanged ($url): nothing to update at PayPal"
  fi
}

# ---------- commands ----------
cmd_stop() {
  local n lp
  lp=$(cat "$RUN/launcher.pid" 2>/dev/null)
  if [ -n "$lp" ] && [ "$lp" != "$$" ] && kill -0 "$lp" 2>/dev/null; then
    kill -TERM "$lp"   # the running ./dev.sh stops its own children (and its watchdog)
    for n in $(seq 1 50); do kill -0 "$lp" 2>/dev/null || break; sleep 0.2; done
  fi
  [ "$lp" = "$$" ] && rm -f "$RUN/launcher.pid"
  for n in frontend backend tunnel follow; do
    if alive "$n"; then stop_proc "$n"; [ "$n" != follow ] && ok "stopped $n"; else rm -f "$RUN/$n.pid"; fi
  done
}

cmd_status() {
  local n
  for n in "${PROCS[@]}"; do
    if alive "$n"; then ok "$n running (pid $(pid_of "$n"))"; else warn "$n not running"; fi
  done
  local url; url=$(cat "$URL_FILE" 2>/dev/null)
  if [ -n "$url" ]; then
    info "tunnel URL:        $url"
    info "PayPal Return URL: $url$CALLBACK_PATH"
    if curl -fsS -m 5 -o /dev/null "$url/health"; then ok "public $url/health answers"; else warn "public $url/health does not answer"; fi
  fi
  info "backend/.env AUTH_REDIRECT_URI host: $(env_url)"
  info "PayPal webhooks:"
  (cd "$BACKEND" && "$PY" -m app.scripts.register_webhook 2>&1 | sed 's/^/      /')
}

preflight() {
  command -v cloudflared >/dev/null || { fail "cloudflared not installed (Arch: sudo pacman -S cloudflared)"; exit 1; }
  command -v npm >/dev/null || { fail "npm not installed"; exit 1; }
  if [ ! -x "$PY" ]; then
    info "Creating backend/.venv (first run)…"
    python3.11 -m venv "$BACKEND/.venv" 2>/dev/null || python3 -m venv "$BACKEND/.venv" || exit 1
    "$PY" -m pip install -q -r "$BACKEND/requirements.txt" || { fail "pip install failed"; exit 1; }
  fi
  if [ ! -d "$FRONTEND/node_modules" ]; then
    info "Installing frontend packages (first run)…"
    (cd "$FRONTEND" && npm ci --no-audit --no-fund) || { fail "npm ci failed"; exit 1; }
  fi
  if [ ! -f "$ENV_FILE" ]; then
    cp "$BACKEND/.env.example" "$ENV_FILE"
    fail "Created backend/.env from .env.example: fill in the PayPal + LLM keys, then run ./dev.sh again"
    exit 1
  fi
  local n
  local lp; lp=$(cat "$RUN/launcher.pid" 2>/dev/null)
  [ -n "$lp" ] && kill -0 "$lp" 2>/dev/null && { fail "./dev.sh is already running (pid $lp). Run ./dev.sh stop first."; exit 1; }
  for n in "${PROCS[@]}"; do
    alive "$n" && { fail "$n is already running (pid $(pid_of "$n")). Run ./dev.sh stop first."; exit 1; }
  done
  for p in $API_PORT $WEB_PORT; do
    if port_busy "$p"; then
      fail "Port $p is in use by something else:"; ss -ltnpH "sport = :$p" | sed 's/^/      /'
      exit 1
    fi
  done
}

start_backend() {
  start_proc backend "$BACKEND" "$PY" -m uvicorn app.main:app --port "$API_PORT" $RELOAD
  if wait_http "http://localhost:$API_PORT/health" 60; then ok "backend  http://localhost:$API_PORT"
  else fail "backend did not start; last log lines:"; tail -20 "$RUN/backend.log"; return 1; fi
}

start_follow() {
  [ "$QUIET" = 1 ] && return
  # like start_proc, but the output stays in this terminal
  ( exec setsid bash -c "tail -n0 -F '$RUN/backend.log' 2>/dev/null | sed -u 's/^/  ${C}[api]${N} /'" ) < /dev/null &
  echo $! > "$RUN/follow.pid"
}

new_tunnel() {  # (re)start the tunnel and wire up its URL
  local url
  stop_proc tunnel
  info "Starting Cloudflare tunnel → localhost:$API_PORT…"
  url=$(start_tunnel) || { fail "cloudflared gave no URL; last log lines:"; tail -15 "$RUN/tunnel.log"; return 1; }
  ok "tunnel   $url"
  apply_url "$url"
}

summary() {
  local url; url=$(cat "$URL_FILE")
  echo
  echo "${G}${B}DisputeShield is running${N}"
  echo "  Dashboard (merchant)  http://localhost:$WEB_PORT"
  echo "  Store (buyers)        http://localhost:$WEB_PORT/store"
  echo "  API                   http://localhost:$API_PORT/health"
  echo "  Public tunnel         $url"
  echo "  PayPal Return URL     $url$CALLBACK_PATH"
  echo "  Logs                  .run/{backend,frontend,tunnel}.log   (./dev.sh logs frontend)"
  echo "  Stop                  Ctrl+C here, or ./dev.sh stop"
  echo
}

watchdog() {  # keeps everything up; restarts the tunnel (and re-wires the URL) if it dies or goes stale
  local misses=0 tick=0 restarts=0 url
  while true; do
    sleep 5 & wait $!
    tick=$((tick + 1))
    if ! alive tunnel; then
      warn "cloudflared stopped; starting a new tunnel (the URL will change)"
      new_tunnel && { stop_proc backend; start_backend; } ; misses=0; restarts=$((restarts + 1))
    fi
    if ! alive backend; then
      warn "backend exited; last log lines:"; tail -15 "$RUN/backend.log"
      start_backend; restarts=$((restarts + 1))
    fi
    if ! alive frontend; then
      warn "frontend exited; restarting"; start_proc frontend "$FRONTEND" npm run dev -- --port "$WEB_PORT" --strictPort
      restarts=$((restarts + 1))
    fi
    if [ "$restarts" -gt 5 ]; then fail "Too many restarts; giving up. Check .run/*.log"; return 1; fi
    if [ $((tick % 12)) -eq 0 ]; then  # every ~60 s: is the public URL still reachable?
      restarts=0
      url=$(cat "$URL_FILE" 2>/dev/null)
      if curl -fsS -m 8 -o /dev/null "$url/health"; then misses=0
      else
        misses=$((misses + 1)); warn "public tunnel not answering ($misses/3)"
        if [ "$misses" -ge 3 ]; then
          warn "tunnel looks dead; replacing it"
          new_tunnel && { stop_proc backend; start_backend; }; misses=0
        fi
      fi
    fi
  done
}

cmd_start() {
  preflight
  echo $$ > "$RUN/launcher.pid"
  trap 'echo; info "Stopping…"; cmd_stop; exit 0' INT TERM
  trap 'cmd_stop' EXIT
  new_tunnel || { cmd_stop; exit 1; }
  start_backend || { cmd_stop; exit 1; }
  start_proc frontend "$FRONTEND" npm run dev -- --port "$WEB_PORT" --strictPort
  if wait_http "http://localhost:$WEB_PORT" 60; then ok "frontend http://localhost:$WEB_PORT"
  else fail "frontend did not start; last log lines:"; tail -20 "$RUN/frontend.log"; cmd_stop; exit 1; fi
  local url; url=$(cat "$URL_FILE")
  if wait_http "$url/health" 45; then ok "public tunnel reaches the backend ($url/health)"
  else warn "public $url/health not answering yet (new tunnels can take a minute; watching it)"; fi
  summary
  start_follow
  watchdog
}

case "$CMD" in
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  status) cmd_status ;;
  url)    u=$(cat "$URL_FILE" 2>/dev/null) || { fail "no tunnel URL yet: run ./dev.sh"; exit 1; }
          echo "$u$CALLBACK_PATH" ;;
  logs)   exec tail -n 50 -F "$RUN/${LOG_TARGET:-backend}.log" ;;
esac
