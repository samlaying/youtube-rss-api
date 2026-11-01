#!/usr/bin/env python3
"""HTTP API for the self-built YouTube feed system.

Runs behind nginx at /yt/api/ on 127.0.0.1. Every request needs the shared
token: ?token=..., X-API-Token: ... or Authorization: Bearer ...

Endpoints
  GET    /yt/api/health
  GET    /yt/api/channels[?videos=1]
  POST   /yt/api/channels            single object or array (batch)
  GET    /yt/api/channels/<key>      key = slug | @handle | channel id | name
  PATCH  /yt/api/channels/<key>
  PATCH  /yt/api/channels            array of {key, ...fields} (batch)
  DELETE /yt/api/channels/<key>
  DELETE /yt/api/channels?keys=a,b   or body {"keys": [...]} (batch)
  GET    /yt/api/channels/<key>/videos[?live=1&limit=15]
  GET    /yt/api/updates?since=ISO&channel=<slug>&limit=50
  GET    /yt/api/resolve?q=<name>    search YouTube for a channel URL
  POST   /yt/api/check               {"channels": [...]} optional; refreshes and
                                     reports newly seen videos
"""

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate as gen  # noqa: E402

TOKEN = os.environ.get("YT_API_TOKEN", "")
if not TOKEN:
    sys.exit("YT_API_TOKEN is required (set it in the environment or a systemd unit)")
HOST = os.environ.get("YT_API_HOST", "127.0.0.1")
PORT = int(os.environ.get("YT_API_PORT", "8099"))
LOCK = threading.Lock()
STARTED = time.time()


def utcnow():
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------- lookups


def load_channels():
    return gen.load_json(gen.CHANNELS_PATH, [])


def find_channel(channels, key):
    key = unquote(key or "").strip()
    if not key:
        return None
    low = key.lower()
    bare = low.lstrip("@")
    for channel in channels:
        if (
            channel["slug"].lower() == low
            or channel["cid"] == key
            or channel["handle"].lstrip("@").lower() == bare
        ):
            return channel
    for channel in channels:
        if channel["name"].lower() == low:
            return channel
    matches = [channel for channel in channels if low in channel["name"].lower()]
    return matches[0] if len(matches) == 1 else None


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "channel"


def unique_slug(channels, base):
    used = {channel["slug"] for channel in channels}
    slug, n = base, 2
    while slug in used:
        slug = f"{base}-{n}"
        n += 1
    return slug


def video_public(channel, item):
    return {
        "channel": channel["slug"],
        "id": item["id"],
        "title": item["title"],
        "link": item["link"],
        "thumbnail": item.get("thumb", ""),
        "first_seen": item["date"].isoformat(),
        "meta": item.get("meta", []),
    }


def channel_public(channel, with_videos=False):
    items = gen.load_cache(channel["slug"])
    payload = {
        "slug": channel["slug"],
        "name": channel["name"],
        "handle": channel["handle"],
        "cid": channel["cid"],
        "youtube_url": f"https://www.youtube.com/{channel['handle']}",
        "feed_url": f"{gen.PUBLIC_BASE}/{channel['slug']}.xml",
        "items": len(items),
        "latest": video_public(channel, items[0]) if items else None,
    }
    if with_videos:
        payload["videos"] = [video_public(channel, item) for item in items]
    return payload


# ---------------------------------------------------------------- mutations


def add_one(spec, channels):
    """Resolve and append one channel spec. Returns (status, channel|error)."""
    if not isinstance(spec, dict):
        return "error", "channel spec must be an object"

    reference = spec.get("cid") or spec.get("url") or spec.get("handle")
    resolved_from = None
    if not reference:
        name = (spec.get("name") or "").strip()
        if not name:
            return "error", "need cid, url, handle or name"
        candidates = gen.search_channels(name, limit=1)
        if not candidates:
            return "error", f"no YouTube channel found for {name!r}"
        reference = candidates[0]["url"]
        resolved_from = candidates[0]

    try:
        cid, handle, name = gen.channel_meta(reference)
    except Exception as exc:  # noqa: BLE001
        return "error", f"resolve failed: {exc}"

    for existing in channels:
        if existing["cid"] == cid:
            return "exists", existing

    channel = {
        "slug": unique_slug(channels, slugify(spec.get("slug") or spec.get("handle") or name)),
        "name": (spec.get("name") or name or handle or cid).strip(),
        "handle": (spec.get("handle") or handle or "").strip(),
        "cid": cid,
    }
    if not channel["handle"]:
        channel["handle"] = f"/channel/{cid}"
    channels.append(channel)
    result = dict(channel)
    if resolved_from:
        result["resolved_from"] = resolved_from
    return "created", result


def update_one(key, patch, channels):
    channel = find_channel(channels, key)
    if not channel:
        return "missing", f"no channel matches {key!r}"
    if not isinstance(patch, dict):
        return "error", "patch must be an object"

    old_slug = channel["slug"]
    touched = False
    for field in ("name", "slug"):
        if field in patch and patch[field]:
            value = str(patch[field]).strip()
            if field == "slug":
                value = slugify(value)
                others = [c for c in channels if c is not channel]
                if any(c["slug"] == value for c in others):
                    return "error", f"slug {value!r} already in use"
            channel[field] = value
            touched = True

    reference = patch.get("url") or patch.get("handle") or patch.get("cid")
    if reference:
        try:
            cid, handle, name = gen.channel_meta(reference)
        except Exception as exc:  # noqa: BLE001
            return "error", f"resolve failed: {exc}"
        if cid != channel["cid"]:
            clash = next((c for c in channels if c["cid"] == cid and c is not channel), None)
            if clash:
                return "error", f"channel id already tracked as {clash['slug']!r}"
        channel["cid"] = cid
        channel["handle"] = (patch.get("handle") or handle or channel["handle"]).strip()
        if not patch.get("name"):
            channel["name"] = name or channel["name"]
        touched = True

    if channel["slug"] != old_slug:
        move_artifacts(old_slug, channel["slug"])

    return ("updated", channel) if touched else ("unchanged", channel)


def move_artifacts(old_slug, new_slug):
    """Keep feed and cache attached to the channel when its slug changes."""
    for directory, ext in ((gen.OUT_DIR, ".xml"), (gen.CACHE_DIR, ".json")):
        source = os.path.join(directory, f"{old_slug}{ext}")
        target = os.path.join(directory, f"{new_slug}{ext}")
        if os.path.exists(source):
            os.replace(source, target)


def delete_one(key, channels):
    channel = find_channel(channels, key)
    if not channel:
        return "missing", f"no channel matches {key!r}"
    channels.remove(channel)
    for path in (
        os.path.join(gen.OUT_DIR, f"{channel['slug']}.xml"),
        os.path.join(gen.CACHE_DIR, f"{channel['slug']}.json"),
    ):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    return "deleted", channel


def prune_state(state, channels):
    keep = {channel["slug"] for channel in channels}
    return {video_id: entry for video_id, entry in state.items() if entry.get("c") in keep}


# ------------------------------------------------------------------- actions


def run_check(keys=None):
    channels = load_channels()
    targets = channels
    missing = []
    if keys:
        targets, seen = [], set()
        for key in keys:
            channel = find_channel(channels, key)
            if not channel:
                missing.append(key)
            elif channel["slug"] not in seen:
                seen.add(channel["slug"])
                targets.append(channel)

    state = gen.load_json(gen.STATE_PATH, {})
    moment = utcnow()
    report = []
    for index, channel in enumerate(targets):
        if index:
            time.sleep(1.5)
        try:
            items, new_ids = gen.refresh_channel(channel, state, moment)
            gen.save_json(gen.STATE_PATH, state)
            report.append(
                {
                    "channel": channel["slug"],
                    "ok": True,
                    "checked": len(items),
                    "new": [video_public(channel, i) for i in items if i["id"] in new_ids],
                }
            )
        except Exception as exc:  # noqa: BLE001
            report.append({"channel": channel["slug"], "ok": False, "error": str(exc)})

    gen.write_aggregate(channels)
    return {"checked": len(report), "report": report, "unknown": missing}


def collect_updates(since_iso=None, slug=None, limit=50):
    channels = load_channels()
    since = None
    if since_iso:
        since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    merged = []
    for channel in channels:
        if slug and channel["slug"] != slug:
            continue
        for item in gen.load_cache(channel["slug"]):
            if since and item["date"] < since:
                continue
            merged.append(video_public(channel, item))
    merged.sort(key=lambda entry: entry["first_seen"], reverse=True)
    return merged[:limit]


# -------------------------------------------------------------------- server


class Handler(BaseHTTPRequestHandler):
    server_version = "yt-feeds-api/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        gen.log(f"api {self.address_string()} {fmt % args}")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    # -- plumbing

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise ValueError("body is not valid JSON")

    def _authorized(self, query):
        supplied = (query.get("token") or [None])[0] or self.headers.get("X-API-Token") or ""
        if not supplied:
            header = self.headers.get("Authorization") or ""
            if header.lower().startswith("bearer "):
                supplied = header[7:].strip()
        return supplied == TOKEN

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path

        if not path.startswith("/yt/api"):
            return self._send(404, {"ok": False, "error": "not found"})
        parts = [unquote(part) for part in path[len("/yt/api"):].strip("/").split("/") if part]

        if not self._authorized(query):
            return self._send(401, {"ok": False, "error": "invalid or missing token"})

        try:
            return self._route(method, parts, query)
        except ValueError as exc:
            return self._send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            gen.log(f"api error {method} {path}: {exc}")
            return self._send(500, {"ok": False, "error": str(exc)})

    # -- routes

    def _route(self, method, parts, query):
        if not parts or parts[0] == "health":
            if method != "GET":
                return self._send(405, {"ok": False, "error": "method not allowed"})
            channels = load_channels()
            return self._send(
                200,
                {
                    "ok": True,
                    "service": "yt-feeds",
                    "channels": len(channels),
                    "uptime_seconds": round(time.time() - STARTED),
                },
            )

        if parts[0] == "resolve":
            if method != "GET":
                return self._send(405, {"ok": False, "error": "method not allowed"})
            q = (query.get("q") or [""])[0].strip()
            if not q:
                raise ValueError("missing q")
            limit = int((query.get("limit") or ["8"])[0])
            found = gen.search_channels(q, limit=limit)
            channels = load_channels()
            tracked_by_cid = {channel["cid"]: channel["slug"] for channel in channels}
            for item in found:
                item["tracked_slug"] = tracked_by_cid.get(item["cid"])
            return self._send(
                200,
                {
                    "ok": True,
                    "query": q,
                    "results": found,
                    "already_tracked": sum(1 for item in found if item["tracked_slug"]),
                },
            )

        if parts[0] == "check":
            if method != "POST":
                return self._send(405, {"ok": False, "error": "method not allowed"})
            body = self._body() or {}
            keys = body.get("channels")
            if isinstance(keys, str):
                keys = [keys]
            with LOCK:
                result = run_check(keys or None)
            return self._send(200, {"ok": all(r["ok"] for r in result["report"]), **result})

        if parts[0] == "updates":
            if method != "GET":
                return self._send(405, {"ok": False, "error": "method not allowed"})
            since = (query.get("since") or [None])[0]
            slug = (query.get("channel") or [None])[0]
            limit = int((query.get("limit") or ["50"])[0])
            updates = collect_updates(since, slug, limit)
            return self._send(
                200, {"ok": True, "since": since, "count": len(updates), "updates": updates}
            )

        if parts[0] != "channels":
            return self._send(404, {"ok": False, "error": "unknown endpoint"})

        # /channels...
        if len(parts) == 1:
            if method == "GET":
                with_videos = (query.get("videos") or ["0"])[0] not in ("0", "", "false")
                return self._send(
                    200,
                    {
                        "ok": True,
                        "count": len(load_channels()),
                        "channels": [channel_public(c, with_videos) for c in load_channels()],
                    },
                )
            if method == "POST":
                return self._add(self._body())
            if method == "PATCH":
                return self._batch_update(self._body())
            if method == "DELETE":
                return self._batch_delete(query)
            return self._send(405, {"ok": False, "error": "method not allowed"})

        key = parts[1]
        if len(parts) == 3 and parts[2] == "videos":
            if method != "GET":
                return self._send(405, {"ok": False, "error": "method not allowed"})
            return self._videos(key, query)
        if len(parts) > 2:
            return self._send(404, {"ok": False, "error": "unknown endpoint"})

        if method == "GET":
            channel = find_channel(load_channels(), key)
            if not channel:
                return self._send(404, {"ok": False, "error": f"no channel matches {key!r}"})
            return self._send(200, {"ok": True, "channel": channel_public(channel, True)})
        if method == "PATCH":
            return self._update(key, self._body())
        if method == "DELETE":
            return self._delete(key)
        return self._send(405, {"ok": False, "error": "method not allowed"})

    # -- handlers

    def _add(self, body):
        specs = body if isinstance(body, list) else (body or {}).get("channels")
        if specs is None:
            specs = [body] if isinstance(body, dict) else None
        if not specs:
            raise ValueError("expected a channel object, an array, or {channels: [...]}")

        with LOCK:
            channels = load_channels()
            results = []
            for spec in specs:
                status, payload = add_one(spec, channels)
                results.append(
                    {
                        "status": status,
                        "channel": payload if status in ("created", "exists") else None,
                        "error": payload if status == "error" else None,
                    }
                )
            gen.save_json(gen.CHANNELS_PATH, channels)

        ok = all(item["status"] != "error" for item in results)
        return self._send(
            200 if ok else 207,
            {"ok": ok, "count": len(results), "results": results},
        )

    def _update(self, key, body):
        with LOCK:
            channels = load_channels()
            status, payload = update_one(key, body, channels)
            if status not in ("updated", "unchanged"):
                code = 404 if status == "missing" else 400
                return self._send(code, {"ok": False, "error": payload})
            gen.save_json(gen.CHANNELS_PATH, channels)
            gen.write_aggregate(channels)
        return self._send(
            200, {"ok": True, "status": status, "channel": channel_public(payload)}
        )

    def _batch_update(self, body):
        items = body if isinstance(body, list) else (body or {}).get("channels")
        if not isinstance(items, list) or not items:
            raise ValueError("expected an array of {key, ...fields}")
        results = []
        with LOCK:
            channels = load_channels()
            for item in items:
                key = item.get("key") or item.get("slug") or item.get("cid")
                status, payload = update_one(key, item, channels)
                results.append(
                    {
                        "key": key,
                        "status": status,
                        "channel": payload if status in ("updated", "unchanged") else None,
                        "error": payload if status not in ("updated", "unchanged") else None,
                    }
                )
            gen.save_json(gen.CHANNELS_PATH, channels)
            gen.write_aggregate(channels)
        ok = all(item["status"] in ("updated", "unchanged") for item in results)
        return self._send(200 if ok else 207, {"ok": ok, "results": results})

    def _delete(self, key):
        with LOCK:
            channels = load_channels()
            status, payload = delete_one(key, channels)
            if status != "deleted":
                return self._send(404, {"ok": False, "error": payload})
            gen.save_json(gen.CHANNELS_PATH, channels)
            state = prune_state(gen.load_json(gen.STATE_PATH, {}), channels)
            gen.save_json(gen.STATE_PATH, state)
            gen.write_aggregate(channels)
        return self._send(
            200,
            {
                "ok": True,
                "status": "deleted",
                "channel": {"slug": payload["slug"], "name": payload["name"]},
            },
        )

    def _batch_delete(self, query):
        keys = query.get("keys") or []
        if len(keys) == 1 and "," in keys[0]:
            keys = [part.strip() for part in keys[0].split(",") if part.strip()]
        if not keys:
            body = self._body() or {}
            keys = body.get("keys") or []
            if isinstance(keys, str):
                keys = [keys]
        if not keys:
            raise ValueError("expected ?keys=a,b or {\"keys\": [...]}")

        results = []
        with LOCK:
            channels = load_channels()
            for key in keys:
                status, payload = delete_one(key, channels)
                results.append(
                    {
                        "key": key,
                        "status": status,
                        "slug": payload["slug"] if status == "deleted" else None,
                        "error": payload if status != "deleted" else None,
                    }
                )
            gen.save_json(gen.CHANNELS_PATH, channels)
            state = prune_state(gen.load_json(gen.STATE_PATH, {}), channels)
            gen.save_json(gen.STATE_PATH, state)
            gen.write_aggregate(channels)
        ok = all(item["status"] == "deleted" for item in results)
        return self._send(200 if ok else 207, {"ok": ok, "results": results})

    def _videos(self, key, query):
        channel = find_channel(load_channels(), key)
        if not channel:
            return self._send(404, {"ok": False, "error": f"no channel matches {key!r}"})
        live = (query.get("live") or ["0"])[0] not in ("0", "", "false")
        limit = int((query.get("limit") or [str(gen.MAX_ITEMS)])[0])

        items = None
        if live:
            with LOCK:
                state = gen.load_json(gen.STATE_PATH, {})
                try:
                    items, _ = gen.refresh_channel(channel, state, utcnow())
                    gen.save_json(gen.STATE_PATH, state)
                    gen.write_aggregate(load_channels())
                except Exception as exc:  # noqa: BLE001
                    return self._send(502, {"ok": False, "error": f"fetch failed: {exc}"})
        if items is None:
            items = gen.load_cache(channel["slug"])
        return self._send(
            200,
            {
                "ok": True,
                "channel": channel["slug"],
                "live": live,
                "count": len(items),
                "videos": [video_public(channel, item) for item in items[:limit]],
            },
        )


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    gen.log(f"yt-feeds api listening on {HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
