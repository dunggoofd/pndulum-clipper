# Pndulum clipper

Records live streams, finds the moments, cuts 9:16 clips with hook + captions + cover, posts through Blotato, submits to Content Rewards campaigns, tracks views and payouts. The operator reviews every clip before it posts.

## Three rules
1. **One machine, one set of accounts.** This clipper runs on this computer only, with the operator's own accounts. Never Dung's accounts. Never on two machines. Double submits read as botting and Reach bans across every campaign.
2. **Campaign rules before cutting.** Every streamer gets `rules/<streamer>.md` before their first clip. Read it before changing anything for that streamer.
3. **The operator joins campaigns and types passwords. You never do.** Never join a campaign, type a password or 2FA code, create an account, or read `.env` values aloud. If a page needs a login, stop and ask.

## Never switch on
- `autopost` and `governor` (they post without review). Only after Dung signs off; `install_units.py` refuses without `--signed-off`.
- Never re-submit a "failed" post before checking the Content Rewards submissions list. A double submit is a ban risk.
- Never edit or delete a posted clip until it is paid.

## Working rules
- Read-only first: explain, then change. Plan mode for anything bigger than one step.
- End every job with proof (a log line, a file, a screenshot of the Clip Desk).
- Keys live in `.env`, edited by hand. Close `.env` before prompting.
- All clipping logins and browsing happen in the browser hub (`browser-hub/hub.sh`, port 9333). Never another browser profile.
- Check with Dung before adding a streamer. Dung's roster is off limits.

## Map
| File | Does | Runs |
|---|---|---|
| `rec.py <streamer>` | 60s chunks + chat while live, stops under `min_free_gb` | always on, one per streamer |
| `highlight.py` | peaks, transcript, Claude judge, cut, hook, captions, cover | every 5 min |
| `cover.py` | cover from a real face frame | per cut |
| `dash.py` | Clip Desk http://127.0.0.1:7779 | always on |
| `gate.py` | campaign rules check before post | per post |
| `publish.py` | Blotato post | on Post |
| `submit.py` | Content Rewards submit via hub, checks submissions first | after post |
| `sentinel.py` | every paid post submitted inside its window | 15 min |
| `views.py` | view counts + Content Rewards sync | 3h |
| `allocator.py` | real $/1k per campaign | hourly |
| `scout.py` | new campaigns, never joins | 3h |
| `stream_clips.py`, `radar.py` | viewer clips as a signal | 2h |
| `install_units.py` | background jobs for this user | by hand |
| `selfcheck.py` | machine check | by hand |

Data lives under `root:` in `config.yaml` (default `~/clipper`). Logs in `logs/`.
