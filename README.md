# youtube-rss-api

Self-hosted RSS feeds and a small JSON API for YouTube channels.

YouTube retired its public Atom endpoint: `https://www.youtube.com/feeds/videos.xml?channel_id=...`
now returns 404 for every channel, and the usual mirrors (Piped, Invidious) are unreliable or
blocked. This project rebuilds the feed from the channel page's server-rendered `ytInitialData`,
so you get normal RSS subscriptions without an API key, a headless browser, or a third-party service.

Standard library only (Python 3.8+). No pip install.

## Features

- RSS 2.0 feed per channel, plus one aggregate feed across all channels
- JSON API to search channels, check for new uploads, and add/update/delete channels in bulk
- Token authentication on every endpoint
- Hourly refresh via cron; on-demand refresh through the API
- Per-channel cache so read endpoints answer instantly

## How it works

1. Fetch `https://www.youtube.com/channel/<channel_id>/videos?hl=en`.
2. Parse `ytInitialData` and read the `lockupViewModel` nodes (video id, title, views, relative age).
3. Emit RSS. YouTube does not expose exact publish dates on that page, so each video's
   **first-seen timestamp** is stored in `state.json` and used as `pubDate`; the relative label
   ("3d ago") only seeds the very first run.

## Quick start

```bash
git clone https://github.com/<you>/youtube-rss-api.git
cd youtube-rss-api

# 1. edit channels.json (slug / name / handle / cid)
python3 generate.py          # writes feeds/*.xml and cache/*.json

# 2. serve the feeds with any web server, e.g.:
python3 -m http.server 8000 --directory feeds

# 3. optional: run the JSON API
export YT_API_TOKEN="$(openssl rand -hex 16)"
export YT_FEEDS_PUBLIC_BASE="http://localhost:8000"
python3 api.py               # listens on 127.0.0.1:8099
```

Get a channel id from a handle:

```bash
curl -sL "https://www.youtube.com/@Fireship" | grep -o '"externalId":"UC[^"]*"' | head -1
```

or let the API find it for you with `/resolve`.

## Configuration

`channels.json` is the channel list and is rewritten by the API:

```json
[
  {"slug": "fireship", "name": "Fireship", "handle": "@Fireship", "cid": "UCsBjURrPoezykLs9EqgamOA"}
]
```

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `YT_FEEDS_BASE` | script directory | both | Data directory (`channels.json`, `state.json`, `cache/`, `feeds/`) |
| `YT_FEEDS_PUBLIC_BASE` | `http://localhost/yt` | both | Public URL prefix written into feed self-links and API `feed_url` |
| `YT_API_TOKEN` | — (required) | `api.py` | Shared secret for every API request |
| `YT_API_HOST` | `127.0.0.1` | `api.py` | Bind address |
| `YT_API_PORT` | `8099` | `api.py` | Bind port |

## HTTP API

Base path in the examples below is `/yt/api` because that is how nginx is wired in `deploy/`;
`api.py` itself serves the same routes under `/yt/api` on its own port.

Authentication: `?token=<token>`, `X-API-Token: <token>`, or `Authorization: Bearer <token>`.
Missing or wrong token returns `401`.

`<key>` accepts a slug, `@handle`, channel id (`UC...`), or a channel name (exact, or a unique substring).

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Service status and channel count |
| GET | `/channels` | List channels with their latest video; `?videos=1` includes all cached videos |
| GET | `/channels/<key>` | One channel with its cached videos |
| GET | `/channels/<key>/videos` | Cached videos; `?live=1` fetches now, `?limit=N` |
| POST | `/channels` | Add one channel, or an array (batch) |
| PATCH | `/channels/<key>` | Update `name` / `slug` / `handle` / `url` / `cid` |
| PATCH | `/channels` | Batch update: `[{"key": "...", ...fields}]` |
| DELETE | `/channels/<key>` | Delete one channel |
| DELETE | `/channels?keys=a,b` | Batch delete, or body `{"keys": [...]}` |
| GET | `/updates` | Recent videos; `?since=ISO&channel=<slug>&limit=N` |
| GET | `/resolve?q=<name>` | Search YouTube for a channel (name, handle, id, URL, subscribers) |
| POST | `/check` | Refresh now and report newly seen videos; body `{"channels": [...]}` optional |

Batch writes return `200` when everything succeeded and `207` when some items failed; each item
carries a `status` (`created`, `exists`, `updated`, `deleted`, `missing`, `error`) and an `error` field.

Adding a channel accepts `url`, `handle`, `cid`, or `name`. With `name` alone the API searches
YouTube, takes the first hit, and echoes it back as `resolved_from` so you can verify the match.

### Examples

```bash
T=<token>; B=http://127.0.0.1:8099/yt/api

# find someone's channel URL
curl -s "$B/resolve?q=mckay+wrigley&token=$T"

# latest videos, fetched live
curl -s "$B/channels/mckay-wrigley/videos?live=1&limit=5&token=$T"

# add by URL
curl -s -X POST -H "X-API-Token: $T" \
  -d '{"url":"https://www.youtube.com/@mkbhd"}' "$B/channels"

# batch add
curl -s -X POST -H "X-API-Token: $T" \
  -d '[{"handle":"@veritasium"},{"name":"Fireship"}]' "$B/channels"

# batch rename / re-slug
curl -s -X PATCH -H "X-API-Token: $T" \
  -d '[{"key":"mkbhd","name":"MKBHD"},{"key":"veritasium","slug":"veritasium-science"}]' "$B/channels"

# check for new uploads
curl -s -X POST -H "X-API-Token: $T" -d '{"channels":["fireship"]}' "$B/check"

# everything new in the last 24h
curl -s "$B/updates?since=$(date -u -d '1 day ago' +%Y-%m-%dT%H:%M:%SZ)&token=$T"

# batch delete
curl -s -X DELETE -H "X-API-Token: $T" "$B/channels?keys=veritasium-science,mkbhd"
```

`check` fetches every targeted channel page; roughly 20 seconds for 12 channels.

## Deployment

`deploy/` contains ready-to-adapt files:

- `yt-feeds-api.service` — systemd unit for `api.py` (bound to 127.0.0.1)
- `cron.d-yt-feeds` — hourly refresh
- `nginx-yt.conf` — static feed route with a token check, plus reverse proxy for `/yt/api/`
- `logrotate-yt-feeds` — log rotation for the cron output

The nginx snippet repeats the token so feeds can be subscribed to in a reader without HTTP auth.
Keep it in sync with `YT_API_TOKEN`, or drop the `if ($arg_token ...)` line if the feeds are public.

## Caveats

- `pubDate` is first-seen time, not the real publish time; the first run approximates it from
  relative labels ("3d ago") and can be hours off.
- Only the newest ~15 videos per channel are kept.
- Parsing depends on channel pages shipping `lockupViewModel`. If YouTube changes the markup,
  `parse_videos()` returns nothing; the generator retries three times and then leaves the
  previous feed file untouched rather than emptying it.
- No Shorts/live filtering: whatever appears on the /videos tab is included.

## Security

- `YT_API_TOKEN` is read from the environment; nothing is hardcoded.
- Never commit `state.json`, `cache/`, or `feeds/` — `.gitignore` already excludes them.
- The API binds to `127.0.0.1` by default; expose it through a reverse proxy, not directly.
