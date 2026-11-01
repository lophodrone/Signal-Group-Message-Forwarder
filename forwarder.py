#!/usr/bin/env python3
"""
Signal group forwarder.

Listens to a signal-cli-rest-api instance (MODE=json-rpc) over its websocket
and re-posts messages between groups according to a set of routes, including
attachments, prefixed with the sender's name.

Config (environment variables):
  SIGNAL_API_URL   e.g. http://signal-api:8080            (default http://localhost:8080)
  SIGNAL_NUMBER    the bot's registered/linked number, e.g. +15551234567

  Routes - EITHER the simple form:
  SOURCE_GROUP     group to read from  (name, "group.xxx" id, or internal id)
  TARGET_GROUP     group(s) to post into, comma-separated for several
  BIDIRECTIONAL    "true" to also forward from the target group(s) back to the source

  OR the general form (takes precedence if set):
  ROUTES           routes separated by ";" or newlines. "->" is one-way, "<->" is both ways:
                     ROUTES=Group A <-> Group B
                     ROUTES=Announcements -> Team A, Team B; Team A <-> Team B
  PREFIX_GROUP     "true" to show which group a message came from, e.g. "Alice [Group A]: hi"

  FORWARD_DELETES  "true"/"false" - when a message is deleted for everyone in a source group,
                   delete the forwarded copies too (default true)
  DATA_DIR         where the message map database is kept (default /data, or . if missing)
  MAP_RETENTION_DAYS  how long to remember forwarded messages for deletes (default 7)
  PREFIX_SENDER    "true"/"false" - prepend "Name: " to forwarded text (default true;
                   set false to forward only the message content)
  FORWARD_EDITS    "true"/"false" - when a message is edited, edit the forwarded copies too
                   (default true; falls back to a new "(edited)" message if the copy is unknown)
  LOG_LEVEL        INFO / DEBUG (DEBUG logs every raw event from the API, for troubleshooting)

Run `python forwarder.py --list-groups` to see the groups the bot account is in.
"""

import base64
import json
import logging
import os
import sqlite3
import sys
import time

import requests

VERSION = "1.6 (repeated edits)"
log = logging.getLogger("forwarder")

OBJECT_REPLACEMENT = "￼"  # Signal uses this as the placeholder for @mentions


def env_bool(name, default=True):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def find_timestamp(data):
    """Find the sent-message timestamp in a /v2/send response, whatever its shape
    ({"timestamp": ...}, [{"timestamp": ...}], nested results, number or string)."""
    if isinstance(data, dict):
        if "timestamp" in data:
            try:
                return int(data["timestamp"])
            except (TypeError, ValueError):
                pass
        for v in data.values():
            ts = find_timestamp(v)
            if ts:
                return ts
    elif isinstance(data, list):
        for v in data:
            ts = find_timestamp(v)
            if ts:
                return ts
    return None


class SendError(RuntimeError):
    pass


class SignalAPI:
    def __init__(self, base_url, number, session=None):
        self.base = base_url.rstrip("/")
        self.number = number
        self.http = session or requests.Session()

    def list_groups(self):
        r = self.http.get(f"{self.base}/v1/groups/{self.number}", timeout=30)
        r.raise_for_status()
        return r.json()

    def get_attachment(self, attachment_id):
        r = self.http.get(f"{self.base}/v1/attachments/{attachment_id}", timeout=120)
        r.raise_for_status()
        return r.content

    def send(self, recipient, message, base64_attachments=None, link_preview=None,
             edit_timestamp=None):
        body = {"number": self.number, "recipients": [recipient], "message": message}
        if base64_attachments:
            body["base64_attachments"] = base64_attachments
        if link_preview:
            body["link_preview"] = link_preview
        if edit_timestamp:
            body["edit_timestamp"] = int(edit_timestamp)
        r = self.http.post(f"{self.base}/v2/send", json=body, timeout=120)
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {}
        ts = find_timestamp(data)
        if not r.ok:
            # signal-cli can report an error even though the message went out (e.g. one group
            # member has a changed safety number). If the response still has a timestamp,
            # treat it as sent so edits/deletes keep working.
            if ts:
                log.warning("API reported HTTP %s but the message was sent: %s",
                            r.status_code, r.text[:500])
            else:
                raise SendError(f"HTTP {r.status_code}: {r.text[:500]}")
        log.debug("send response: %s", data)
        if not ts:
            log.warning("No timestamp in send response: %s", r.text[:500])
        return ts

    def remote_delete(self, recipient, timestamp):
        r = self.http.delete(f"{self.base}/v1/remote-delete/{self.number}",
                             json={"recipient": recipient, "timestamp": int(timestamp)}, timeout=60)
        if not r.ok:
            raise RuntimeError(f"remote delete failed: HTTP {r.status_code}: {r.text[:300]}")
        log.debug("remote delete response: %s", r.text[:300])

    def ws_url(self):
        url = self.base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{url}/v1/receive/{self.number}"


def resolve_group(api, wanted, groups=None):
    """Return (send_id, internal_id, name) for a group given its name or either id form."""
    groups = groups if groups is not None else api.list_groups()
    for g in groups:
        if wanted in (g.get("id"), g.get("internal_id"), g.get("name")):
            return g["id"], g["internal_id"], g.get("name", "")
    names = ", ".join(repr(g.get("name")) for g in groups) or "(none)"
    raise SystemExit(f"Group {wanted!r} not found. Bot is a member of: {names}")


def render_mentions(text, mentions):
    """Replace Signal's U+FFFC mention placeholders with @Name."""
    if not text or not mentions:
        return text
    # Apply from the end so earlier offsets stay valid.
    for m in sorted(mentions, key=lambda m: m.get("start", 0), reverse=True):
        start, length = m.get("start", 0), m.get("length", 1)
        who = m.get("name") or m.get("number") or m.get("uuid", "someone")
        if text[start:start + length] == OBJECT_REPLACEMENT * length:
            text = text[:start] + f"@{who}" + text[start + length:]
    return text


class MessageStore:
    """Remembers which forwarded messages came from which original, so deletes can follow."""

    def __init__(self, path=":memory:", retention_days=7):
        self.db = sqlite3.connect(path)
        self.retention_ms = int(retention_days * 86400 * 1000)
        self.db.execute("""CREATE TABLE IF NOT EXISTS forwards (
            src_group TEXT, src_ts INTEGER, target TEXT, sent_ts INTEGER, created_ms INTEGER)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS forwards_src ON forwards (src_group, src_ts)")
        self.db.commit()
        self._writes = 0

    def record(self, src_group, src_ts, target, sent_ts):
        now = int(time.time() * 1000)
        self.db.execute("INSERT INTO forwards VALUES (?, ?, ?, ?, ?)",
                        (src_group, int(src_ts), target, int(sent_ts), now))
        self._writes += 1
        if self._writes % 200 == 0:
            self.db.execute("DELETE FROM forwards WHERE created_ms < ?", (now - self.retention_ms,))
        self.db.commit()

    def get(self, src_group, src_ts):
        return self.db.execute("SELECT target, sent_ts FROM forwards WHERE src_group=? AND src_ts=?",
                               (src_group, int(src_ts))).fetchall()

    def forget_copies(self, src_group, copies):
        """Remove every remembered version that points at the given forwarded copies."""
        for target, sent_ts in copies:
            self.db.execute("DELETE FROM forwards WHERE src_group=? AND target=? AND sent_ts=?",
                            (src_group, target, int(sent_ts)))
        self.db.commit()

    def pop(self, src_group, src_ts):
        rows = self.db.execute("SELECT target, sent_ts FROM forwards WHERE src_group=? AND src_ts=?",
                               (src_group, int(src_ts))).fetchall()
        self.db.execute("DELETE FROM forwards WHERE src_group=? AND src_ts=?", (src_group, int(src_ts)))
        self.db.commit()
        return rows


def parse_routes(text):
    """'A <-> B; C -> D, E' -> [(src, dst), ...] using group names/ids as written."""
    pairs = []
    for line in text.replace("\n", ";").split(";"):
        line = line.strip()
        if not line:
            continue
        if "<->" in line:
            left, right, both = *line.split("<->", 1), True
        elif "->" in line:
            left, right, both = *line.split("->", 1), False
        else:
            raise SystemExit(f"Route {line!r} needs '->' or '<->'")
        lefts = [x.strip() for x in left.split(",") if x.strip()]
        rights = [x.strip() for x in right.split(",") if x.strip()]
        if not lefts or not rights:
            raise SystemExit(f"Route {line!r} is missing a group")
        for a in lefts:
            for b in rights:
                pairs.append((a, b))
                if both:
                    pairs.append((b, a))
    return pairs


def build_routes(api, groups, env):
    """Return ({source_internal_id: [target_send_id, ...]}, {any_id: name})."""
    if env.get("ROUTES", "").strip():
        pairs = parse_routes(env["ROUTES"])
    else:
        src, tgt = env.get("SOURCE_GROUP"), env.get("TARGET_GROUP")
        if not src or not tgt:
            raise SystemExit("Set ROUTES, or SOURCE_GROUP and TARGET_GROUP "
                             "(run with --list-groups to find them)")
        both = env_bool_from(env, "BIDIRECTIONAL", False)
        pairs = []
        for t in (x.strip() for x in tgt.split(",")):
            if t:
                pairs.append((src.strip(), t))
                if both:
                    pairs.append((t, src.strip()))
    if not pairs:
        raise SystemExit("No routes configured")

    routes, names = {}, {}
    for a, b in pairs:
        _, a_internal, a_name = resolve_group(api, a, groups)
        b_send, b_internal, b_name = resolve_group(api, b, groups)
        names[a_internal] = a_name
        names[b_send] = b_name
        if a_internal == b_internal:
            raise SystemExit(f"Route {a!r} -> {b!r} points a group at itself")
        lst = routes.setdefault(a_internal, [])
        if b_send not in lst:
            lst.append(b_send)
    return routes, names


def env_bool_from(env, name, default):
    return str(env.get(name, default)).strip().lower() in ("1", "true", "yes", "on")


class Forwarder:
    def __init__(self, api, routes, prefix_sender=True, forward_edits=True,
                 prefix_group=False, group_names=None, forward_deletes=True, store=None):
        """routes: {source_internal_id: [target_send_id, ...]}"""
        self.api = api
        self.forward_deletes = forward_deletes
        self.store = store if store is not None else MessageStore()
        self.routes = {k: list(v) for k, v in routes.items()}
        self.prefix_sender = prefix_sender
        self.forward_edits = forward_edits
        self.prefix_group = prefix_group
        self.group_names = group_names or {}

    def handle(self, payload):
        """Process one websocket payload. Returns True if something was forwarded."""
        env = payload.get("envelope", payload)
        if not isinstance(env, dict):
            return False

        edited = False
        msg = env.get("dataMessage")
        if not msg and self.forward_edits and env.get("editMessage"):
            msg = env["editMessage"].get("dataMessage")
            edited = True
        # Key used to find forwarded copies later. For edits, the original message's timestamp,
        # because a delete-for-everyone refers to the original.
        if edited:
            src_ts = env["editMessage"].get("targetSentTimestamp")
        else:
            src_ts = (msg or {}).get("timestamp") or env.get("timestamp")
        rev_ts = (msg or {}).get("timestamp") or env.get("timestamp")  # this version's timestamp
        if not msg:
            return False  # receipts, typing indicators, sync messages, etc.

        # Never re-forward something the bot itself posted (loop protection).
        if self.api.number and env.get("sourceNumber") == self.api.number:
            return False

        group = (msg.get("groupInfo") or {}).get("groupId")
        targets = self.routes.get(group)
        if not targets:
            return False

        if msg.get("remoteDelete"):
            log.info("Delete-for-everyone received in %r", self.group_names.get(group, group))
            return self.handle_delete(group, msg["remoteDelete"].get("timestamp"))

        if msg.get("reaction"):
            return False  # can't meaningfully mirror these into another group

        text = render_mentions(msg.get("message") or "", msg.get("mentions"))
        attachments = msg.get("attachments") or []
        sticker = msg.get("sticker")
        if not text and not attachments and not sticker:
            return False

        sender = env.get("sourceName") or env.get("sourceNumber") or env.get("source") or "Unknown"
        parts = []
        quote = msg.get("quote")
        if quote and quote.get("text"):
            q = quote["text"].replace("\n", " ")
            parts.append(f"> {q[:120]}{'…' if len(q) > 120 else ''}")
        if text:
            parts.append(text)
        if sticker and not text and not attachments:
            parts.append("[sticker]")
        body = "\n".join(parts)
        # For an edit, find the copies we forwarded earlier so we can edit them in place.
        edit_copies = dict(self.store.get(group, src_ts)) if (edited and src_ts) else {}
        if edited and not edit_copies:
            log.info("Edit received but the original wasn't forwarded by this bot; sending as new message")
            body = f"(edited) {body}"
        label_parts = []
        if self.prefix_sender:
            label_parts.append(sender)
        if self.prefix_group and group in self.group_names:
            label_parts.append(f"[{self.group_names[group]}]")
        if label_parts:
            label = " ".join(label_parts)
            body = f"{label}: {body}" if body else f"{label}:"

        b64 = []
        for a in ([] if edit_copies else attachments):  # an edit can't change attachments
            try:
                data = self.api.get_attachment(a["id"])
            except Exception as e:
                log.warning("Could not fetch attachment %s: %s", a.get("id"), e)
                body += f"\n[attachment could not be forwarded: {a.get('filename') or a.get('contentType')}]"
                continue
            mime = a.get("contentType") or "application/octet-stream"
            fname = a.get("filename")
            prefix = f"data:{mime};filename={fname};base64," if fname else f"data:{mime};base64,"
            b64.append(prefix + base64.b64encode(data).decode())

        preview = self.build_preview(msg.get("previews"), body)
        if "http://" in text or "https://" in text:
            if preview:
                log.info("Including link preview for %s", preview["url"])
            else:
                log.info("Message has a link but the original carried no preview data (previews=%s)",
                         json.dumps(msg.get("previews"))[:300])

        sent = 0
        for target in targets:
            try:
                edit_ts = edit_copies.get(target)
                if edited and edit_copies and not edit_ts:
                    continue  # this target never got the original
                # No automatic retry: an error doesn't always mean the message wasn't delivered,
                # and retrying caused duplicates.
                sent_ts = self.api.send(target, body, b64, preview, edit_timestamp=edit_ts)
                sent += 1
                if not sent_ts and not edit_ts:
                    log.warning("API gave no timestamp for the forwarded message; "
                                "edits/deletes of it can't be mirrored")
                # Remember every version of the message, not just the original: a later edit or
                # delete may point at the previous edit's timestamp rather than the original's.
                # Each source version maps to our matching version, so we mirror whichever the
                # sender's app uses.
                keys = set()
                if edit_ts:
                    our_ts = sent_ts or edit_ts
                    if rev_ts:
                        keys.add(rev_ts)
                else:
                    our_ts = sent_ts
                    keys.update(k for k in (src_ts, rev_ts) if k)
                for k in keys if our_ts else ():
                    try:
                        self.store.record(group, k, target, our_ts)
                    except Exception as e:
                        log.warning("Could not record forwarded message: %s", e)
            except Exception as e:
                # One bad target shouldn't stop delivery to the others.
                log.warning("Failed to forward to %s: %s", target, e)
        if edit_copies:
            log.info("Edited forwarded copies of a message from %s in %d group(s)", sender, sent)
        else:
            log.info("Forwarded message from %s (%d attachment(s)) to %d/%d group(s)",
                     sender, len(b64), sent, len(targets))
        return sent > 0

    def handle_delete(self, group, src_ts):
        if not self.forward_deletes or not src_ts:
            return False
        copies = self.store.pop(group, src_ts)
        if copies:
            self.store.forget_copies(group, copies)  # drop the other remembered versions too
        if not copies:
            log.info("No forwarded copies known for that message (forwarded before the update, "
                     "or the message map isn't persisted)")
            return False
        deleted = 0
        for target, sent_ts in copies:
            try:
                self.api.remote_delete(target, sent_ts)
                deleted += 1
            except Exception as e:
                log.warning("Could not delete forwarded message in %s: %s", target, e)
        log.info("Deleted %d/%d forwarded copies", deleted, len(copies))
        return deleted > 0

    def build_preview(self, previews, body):
        """Turn the incoming message's link preview into the /v2/send link_preview format."""
        for p in previews or []:
            url = p.get("url")
            if not url or url not in body:
                continue  # Signal only shows a preview if its URL is in the message text
            lp = {"url": url, "title": p.get("title") or url}
            if p.get("description"):
                lp["description"] = p["description"]
            image = p.get("image") or {}
            if image.get("id"):
                try:
                    lp["base64_thumbnail"] = base64.b64encode(
                        self.api.get_attachment(image["id"])).decode()
                except Exception as e:
                    log.warning("Could not fetch link preview image: %s", e)
            return lp  # Signal supports one preview per message
        return None

    def run_forever(self):
        import websocket  # websocket-client

        backoff = 1
        while True:
            url = self.api.ws_url()
            try:
                log.info("Connecting to %s", url)
                ws = websocket.create_connection(url, timeout=None)
                log.info("Connected; listening for messages")
                backoff = 1
                while True:
                    raw = ws.recv()
                    if not raw:
                        continue
                    log.debug("event: %s", raw[:4000])
                    try:
                        self.handle(json.loads(raw))
                    except Exception:
                        log.exception("Failed to forward message: %s", raw[:500])
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.warning("Websocket error: %s; reconnecting in %ss", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)


def open_store():
    data_dir = os.environ.get("DATA_DIR") or ("/data" if os.path.isdir("/data") else ".")
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "forwards.sqlite3")
    days = float(os.environ.get("MAP_RETENTION_DAYS", "7"))
    log.info("Message map: %s (kept %g days)", path, days)
    return MessageStore(path, days)


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    number = os.environ.get("SIGNAL_NUMBER")
    if not number:
        sys.exit("SIGNAL_NUMBER is required")
    api = SignalAPI(os.environ.get("SIGNAL_API_URL", "http://localhost:8080"), number)

    if "--list-groups" in sys.argv:
        for g in api.list_groups():
            print(f"{g.get('name')!r}\n    id:          {g.get('id')}\n    internal_id: {g.get('internal_id')}")
        return

    log.info("Signal forwarder %s", VERSION)
    groups = api.list_groups()
    routes, names = build_routes(api, groups, os.environ)
    for src, tgts in routes.items():
        log.info("Route: %r -> %s", names.get(src, src),
                 ", ".join(repr(names.get(t, t)) for t in tgts))

    Forwarder(
        api, routes,
        prefix_sender=env_bool("PREFIX_SENDER", True),
        forward_edits=env_bool("FORWARD_EDITS", True),
        prefix_group=env_bool("PREFIX_GROUP", False),
        group_names=names,
        forward_deletes=env_bool("FORWARD_DELETES", True),
        store=open_store(),
    ).run_forever()


if __name__ == "__main__":
    main()
