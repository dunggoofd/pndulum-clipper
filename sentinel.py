"""Submit sentinel: every paid post must be in Content Rewards inside its window, and reviewed statuses must be read.

Runs after views.py has synced the ledger from Content Rewards (views.py calls submit.sync_cli). Alerts once per
condition per post (state kept in ~/gta-clipper/sentinel.json):
  failed     auto-submit failed and the window is still open: alert right away with minutes left
  retry      still not submitted RETRY_AT_MIN after posting: retries submit.py once, alerts if that fails
  last call  still not submitted LAST_CALL_MIN before the window closes: alert to submit by hand
  deleted    Content Rewards lists it as deleted (reviewer removed it): alert with the campaign, so the reason gets read
  rejected   status rejected: alert
  window     submitted after the window closed: alert, likely unpaid

python3 sentinel.py [--dry]     (called from views.py)
python3 sentinel.py --fast      launchd every 2 min: only when a post under 40 min is unsubmitted, syncs Content Rewards first
"""
import json, subprocess, sys, time

import gate
from views import CFG, HERE, ROOT, read_ledger

STATE = ROOT / "sentinel.json"
RETRY_AT_MIN = 10
LAST_CALL_MIN = 10
BAD = {"deleted", "rejected", "denied"}


def state():
    return json.loads(STATE.read_text()) if STATE.exists() else {"seen": {}}


def main():
    dry = "--dry" in sys.argv
    st = state()
    now = time.time()
    alerts, retries = [], []
    for f, c in read_ledger()["clips"].items():
        who = f.split("/")[-1].split("-")[0]
        s = CFG["streamers"].get(who, {})
        if not s.get("campaign_url") or gate.money(who):
            continue
        window = (s.get("submit_window_min") or 30) * 60
        for p, post in c.get("posts", {}).items():
            key = f"{f}|{p}"
            posted = post.get("posted") or now
            age = now - posted
            seen = st["seen"].setdefault(key, {})
            if post.get("submitted"):
                cr = (post.get("cr_status") or "").lower()
                if cr in BAD and "bad" not in seen:
                    seen["bad"] = now
                    alerts.append(f"{who} {p}: Content Rewards says {cr} ({post.get('cr_campaign')}). Read the reason: {post['url']}")
                sub_at = post.get("submitted_at")
                if sub_at and sub_at - posted > window and "window" not in seen:
                    seen["window"] = now
                    alerts.append(f"{who} {p}: submitted {int((sub_at - posted) / 60)} min after posting, window is {window // 60}. Probably unpaid.")
                continue
            pub = c.get("publish", {}).get(p, {})
            if age > 24 * 3600:
                seen.setdefault("late", now)   # history from before the sentinel existed: never alert, never retry
                continue
            if age >= window:
                seen.setdefault("late", now)   # window shut: a ping now is useless, submit.py would be refused
                continue
            left = int((window - age) / 60)
            if pub.get("state") == "submit-failed" and "failed" not in seen:
                seen["failed"] = now
                alerts.append(f"{who} {p}: auto-submit FAILED, {left} min left to submit by hand. {pub.get('error') or ''} {post['url']}")
            if age >= RETRY_AT_MIN * 60 and "retry" not in seen and pub.get("state") != "submitting":
                seen["retry"] = now
                retries.append((f, p, who, int(age / 60), left))
            elif age >= window - LAST_CALL_MIN * 60 and "last" not in seen:
                seen["last"] = now
                alerts.append(f"{who} {p}: LAST CALL, not in Content Rewards, {left} min left. Submit by hand: {post['url']}")
    for f, p, who, mins, left in retries:
        print(f"retry submit {who} {p} at {mins} min", flush=True)
        if dry:
            continue
        r = subprocess.run([sys.executable, str(HERE / "submit.py"), f, p, "--force"], cwd=HERE, capture_output=True, text=True, timeout=600)
        if "submitted" not in r.stdout:
            alerts.append(f"{who} {p}: auto-submit retry failed at {mins} min, {left} min left to submit by hand: {(r.stderr or r.stdout).strip()[-200:]}")
    if alerts:
        msg = "Submit sentinel\n" + "\n".join(f"- {a}" for a in alerts)
        print(msg, flush=True)
        if not dry:
            try:
                import scout
                scout.notify(msg)
            except Exception as e:
                print("notify failed", e, flush=True)
    if not dry:
        STATE.write_text(json.dumps(st, indent=1))


def fresh_unsubmitted():
    now = time.time()
    return any(not post.get("submitted") and now - (post.get("posted") or 0) < 40 * 60
               for c in read_ledger()["clips"].values() for post in c.get("posts", {}).values())


if __name__ == "__main__":
    if "--fast" in sys.argv:
        if not fresh_unsubmitted():
            sys.exit(0)
        import submit
        submit.sync_cli()
    main()
