#!/usr/bin/env bash
# Browser hub: one always-on Chrome on the canonical profile, shared by every agent over CDP.
# Every clipping login lives in this one window. Never log in anywhere else.
#
#   hub.sh run                     launchd entry point (execs Chrome)
#   hub.sh start | stop | restart | status
#   hub.sh attach <session>        attach a playwright-cli session in its OWN new tab
#   hub.sh detach <session>        close that tab + session (hub stays up)
#   hub.sh url <session>           the session's real URL (the CLI's "Page URL" line lies)
#   hub.sh guard <session>         exit 3 + desktop alert if the session's tab is a login page
#   hub.sh lock <site> [owner] [wait_s]   one writer per site; waits up to wait_s (default 900)
#   hub.sh unlock <site>
set -uo pipefail
PORT=9333
PROFILE="$HOME/.playwright/clipper-profile"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ ! -x "$CHROME" ]; then   # Linux/WSL: first Chrome-family binary on PATH
  CHROME="$(command -v google-chrome-stable || command -v google-chrome || command -v chromium || command -v chromium-browser || echo "$CHROME")"
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$HERE/hub.json"
LOCKS="$HOME/.playwright/hub-locks"
LABEL="clipper.browserhub"
STALE_S=2700
LOGIN_RE='accounts\.google\.com/(v3/)?signin|ServiceLogin|/login|/signin|/sign-in|/sign_in|/oauth|checkpoint|two_factor|/2fa|/sso|/auth/'

alive() { curl -s -m 2 "http://127.0.0.1:$PORT/json/version" >/dev/null; }
pw() { playwright-cli -s="$1" "${@:2}"; }
session_url() {
  local u
  for _ in 1 2 3 4 5; do
    u="$(pw "$1" eval "location.href" 2>/dev/null | awk '/^### Result/{getline; gsub(/"/,""); print; exit}')"
    [ -n "$u" ] && { echo "$u"; return; }
    sleep 2
  done
}

notify() { bash "$HERE/../notify.sh" "Browser hub" "$1"; }
mtime() { if [ "$(uname -s)" = Darwin ]; then stat -f %m "$1"; else stat -c %Y "$1"; fi; }
bg_start() {
  launchctl kickstart "gui/$(id -u)/$LABEL" 2>/dev/null && return
  systemctl --user start clipper-browserhub.service 2>/dev/null && return
  nohup "$0" run >/dev/null 2>&1 &
}

case "${1:-}" in
  run)
    exec "$CHROME" --user-data-dir="$PROFILE" --remote-debugging-port=$PORT --remote-debugging-address=127.0.0.1 \
      --use-mock-keychain --password-store=basic --no-first-run --no-default-browser-check \
      --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding \
      --disable-hang-monitor --disable-ipc-flooding-protection --disable-extensions \
      --disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion \
      about:blank ;;
  restart)
    "$0" stop >/dev/null
    for _ in $(seq 1 20); do pgrep -f "user-data-dir=$PROFILE" >/dev/null || break; sleep 0.5; done
    launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || bg_start
    for _ in $(seq 1 30); do alive && { echo "hub restarted on :$PORT"; exit 0; }; sleep 0.5; done
    echo "hub failed to restart"; exit 1 ;;
  start)
    alive && { echo "hub up on :$PORT"; exit 0; }
    if pgrep -f "user-data-dir=$PROFILE" >/dev/null; then
      echo "profile held by a non-hub Chrome. Close it first: pgrep -fl clipper-profile"; exit 1
    fi
    bg_start
    for _ in $(seq 1 30); do alive && { echo "hub up on :$PORT"; exit 0; }; sleep 0.5; done
    echo "hub failed to start"; exit 1 ;;
  stop)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; systemctl --user stop clipper-browserhub.service 2>/dev/null
    python3 -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p: p.chromium.connect_over_cdp('http://127.0.0.1:$PORT').new_browser_cdp_session().send('Browser.close')" 2>/dev/null
    echo "hub stopped. Start again with: $0 start" ;;
  status)
    alive && echo "hub: UP :$PORT" || echo "hub: DOWN"
    alive && curl -s "http://127.0.0.1:$PORT/json/list" | python3 -c "import json,sys;t=[x for x in json.load(sys.stdin) if x['type']=='page'];print(f'tabs: {len(t)}');[print('  ',x['url'][:90]) for x in t]" 2>/dev/null
    echo "locks:"; ls "$LOCKS" 2>/dev/null | while read -r l; do echo "   $l: $(cat "$LOCKS/$l/owner" 2>/dev/null)"; done ;;
  attach)
    S="${2:?session name}"
    "$0" start >/dev/null || exit 1
    "$0" lock _attach "$S" 120 >/dev/null || { echo "attach queue timeout"; exit 1; }
    pw "$S" open --config="$CONFIG" >/dev/null 2>&1 || { "$0" unlock _attach >/dev/null; echo "attach failed"; exit 1; }
    pw "$S" tab-new >/dev/null 2>&1
    "$0" unlock _attach >/dev/null
    echo "attached $S in its own tab. Navigate with: playwright-cli -s=$S goto <url>" ;;
  detach)
    S="${2:?session name}"
    pw "$S" tab-close >/dev/null 2>&1; pw "$S" close >/dev/null 2>&1; echo "detached $S" ;;
  url)
    session_url "${2:?session name}" ;;
  guard)
    S="${2:?session name}"; U="$(session_url "$S")"
    if [ -z "$U" ]; then echo "guard: no url for $S"; exit 2; fi
    if echo "$U" | grep -qE "$LOGIN_RE"; then
      HOST="$(echo "$U" | awk -F/ '{print $3}')"; mkdir -p "$LOCKS"; STAMP="$LOCKS/.ping-$HOST"
      if [ ! -f "$STAMP" ] || [ $(( $(date +%s) - $(mtime "$STAMP") )) -gt 600 ]; then
        notify "Login needed on $HOST ($S is waiting). Log in in the hub Chrome window."
        touch "$STAMP"
      fi
      echo "LOGIN WALL: $HOST. Do not snapshot. Ask the operator to log in by hand."; exit 3
    fi
    echo "ok: $U" ;;
  lock)
    SITE="${2:?site}"; OWNER="${3:-$$}"; WAIT="${4:-900}"; D="$LOCKS/$SITE"; mkdir -p "$LOCKS"
    [ "$SITE" = "_attach" ] && STALE_S=60
    for _ in $(seq 1 "$WAIT"); do
      if mkdir "$D" 2>/dev/null; then echo "$OWNER $(date '+%F %T')" > "$D/owner"; echo "locked $SITE"; exit 0; fi
      if [ $(( $(date +%s) - $(mtime "$D") )) -gt $STALE_S ]; then
        echo "stale lock on $SITE ($(cat "$D/owner" 2>/dev/null)), taking it"; rm -rf "$D"; continue
      fi
      sleep 1
    done
    echo "timeout: $SITE held by $(cat "$D/owner" 2>/dev/null)"; exit 1 ;;
  unlock)
    rm -rf "$LOCKS/${2:?site}"; echo "unlocked $2" ;;
  *) sed -n 2,13p "$0"; exit 1 ;;
esac
