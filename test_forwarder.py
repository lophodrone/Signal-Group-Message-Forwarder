import base64
import unittest

from forwarder import Forwarder, MessageStore, render_mentions, resolve_group, SignalAPI, parse_routes, build_routes

SRC, TGT = "SRCinternal==", "group.TGTsendid"


class FakeAPI:
    number = "+1999"

    def __init__(self):
        self.sent = []
        self.previews = []
        self.deleted = []
        self.edits = []
        self._ts = 1000
        self.files = {"att1": b"\x89PNGfake"}

    def get_attachment(self, aid):
        return self.files[aid]

    def send(self, recipient, message, b64=None, link_preview=None, edit_timestamp=None):
        self.sent.append((recipient, message, b64 or []))
        self.previews.append(link_preview)
        self.edits.append(edit_timestamp)
        self._ts += 1
        return self._ts

    def remote_delete(self, recipient, ts):
        self.deleted.append((recipient, ts))


def env(msg, name="Alice", group=SRC):
    msg = dict(msg, groupInfo={"groupId": group, "type": "DELIVER"})
    msg.setdefault("timestamp", 555)
    return {"envelope": {"sourceName": name, "sourceNumber": "+1555", "dataMessage": msg}, "account": "+1999"}


class T(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.f = Forwarder(self.api, {SRC: [TGT]})

    def test_text(self):
        self.assertTrue(self.f.handle(env({"message": "hi all"})))
        self.assertEqual(self.api.sent, [(TGT, "Alice: hi all", [])])

    def test_other_group_ignored(self):
        self.assertFalse(self.f.handle(env({"message": "x"}, group="OTHER")))
        self.assertEqual(self.api.sent, [])

    def test_receipts_and_typing_ignored(self):
        self.assertFalse(self.f.handle({"envelope": {"receiptMessage": {}}}))
        self.assertFalse(self.f.handle({"envelope": {"typingMessage": {}}}))
        self.assertFalse(self.f.handle({"envelope": {"syncMessage": {}}}))

    def test_reaction_ignored(self):
        self.assertFalse(self.f.handle(env({"reaction": {"emoji": "👍"}})))

    def test_attachment(self):
        self.f.handle(env({"message": None, "attachments": [
            {"id": "att1", "contentType": "image/png", "filename": "cat.png"}]}))
        rcpt, body, b64 = self.api.sent[0]
        self.assertEqual(body, "Alice:")
        self.assertTrue(b64[0].startswith("data:image/png;filename=cat.png;base64,"))
        self.assertEqual(base64.b64decode(b64[0].split(",", 1)[1]), b"\x89PNGfake")

    def test_mentions(self):
        self.assertEqual(
            render_mentions("hey ￼ and ￼!", [
                {"start": 4, "length": 1, "name": "Bob"},
                {"start": 10, "length": 1, "number": "+1222"}]),
            "hey @Bob and @+1222!")

    def test_quote_and_edit(self):
        self.f.handle({"envelope": {"sourceName": "Al", "editMessage": {"dataMessage": {
            "message": "fixed", "quote": {"text": "orig"},
            "groupInfo": {"groupId": SRC}}}}})
        self.assertEqual(self.api.sent[0][1], "Al: (edited) > orig\nfixed")  # original unknown -> fallback

    def test_no_prefix(self):
        Forwarder(self.api, {SRC: [TGT]}, prefix_sender=False).handle(env({"message": "plain"}))
        self.assertEqual(self.api.sent[0][1], "plain")

    def test_multiple_targets(self):
        f = Forwarder(self.api, {SRC: ["group.A", "group.B"]})
        f.handle(env({"message": "fan out", "attachments": [
            {"id": "att1", "contentType": "image/png"}]}))
        self.assertEqual([r for r, _, _ in self.api.sent], ["group.A", "group.B"])
        self.assertTrue(all(len(b) == 1 for _, _, b in self.api.sent))

    def test_one_failing_target_doesnt_block_others(self):
        orig = self.api.send
        def flaky(r, m, b=None, lp=None, edit_timestamp=None):
            if r == "group.A":
                raise RuntimeError("boom")
            orig(r, m, b, lp)
        self.api.send = flaky
        self.assertTrue(Forwarder(self.api, {SRC: ["group.A", "group.B"]}).handle(env({"message": "x"})))
        self.assertEqual([r for r, _, _ in self.api.sent], ["group.B"])

    def test_bidirectional_and_loop_protection(self):
        f = Forwarder(self.api, {"Aint": ["group.B"], "Bint": ["group.A"]},
                      prefix_group=True, group_names={"Aint": "Group A", "Bint": "Group B"})
        f.handle(env({"message": "from A"}, name="Alice", group="Aint"))
        f.handle(env({"message": "from B"}, name="Bob", group="Bint"))
        self.assertEqual(self.api.sent, [
            ("group.B", "Alice [Group A]: from A", []),
            ("group.A", "Bob [Group B]: from B", [])])
        # The bot's own post showing up in a group must not be forwarded again.
        echo = env({"message": "Alice: from A"}, group="Bint")
        echo["envelope"]["sourceNumber"] = FakeAPI.number
        self.assertFalse(f.handle(echo))
        # Nor sync messages (how a linked account sees its own sends).
        self.assertFalse(f.handle({"envelope": {"sourceNumber": "+1999", "syncMessage": {
            "sentMessage": {"message": "x", "groupInfo": {"groupId": "Bint"}}}}}))
        self.assertEqual(len(self.api.sent), 2)

    def test_parse_routes(self):
        self.assertEqual(parse_routes("A <-> B"), [("A", "B"), ("B", "A")])
        self.assertEqual(parse_routes("Ann -> T1, T2; X<->Y"),
                         [("Ann", "T1"), ("Ann", "T2"), ("X", "Y"), ("Y", "X")])
        with self.assertRaises(SystemExit):
            parse_routes("A B")

    def test_build_routes(self):
        groups = [{"name": n, "id": f"group.{n}", "internal_id": f"{n}=="} for n in ("A", "B", "C")]
        r, _ = build_routes(None, groups, {"ROUTES": "A <-> B\nA -> C"})
        self.assertEqual(r, {"A==": ["group.B", "group.C"], "B==": ["group.A"]})
        r, _ = build_routes(None, groups, {"SOURCE_GROUP": "A", "TARGET_GROUP": "B, C", "BIDIRECTIONAL": "true"})
        self.assertEqual(r, {"A==": ["group.B", "group.C"], "B==": ["group.A"], "C==": ["group.A"]})
        r, _ = build_routes(None, groups, {"SOURCE_GROUP": "A", "TARGET_GROUP": "B"})
        self.assertEqual(r, {"A==": ["group.B"]})
        with self.assertRaises(SystemExit):
            build_routes(None, groups, {"ROUTES": "A <-> A"})

    def test_content_only(self):
        f = Forwarder(self.api, {SRC: [TGT]}, prefix_sender=False)
        f.handle(env({"message": "just this"}))
        f.handle(env({"message": None, "attachments": [{"id": "att1", "contentType": "image/png"}]}))
        self.assertEqual([m for _, m, _ in self.api.sent], ["just this", ""])
        self.assertEqual(len(self.api.sent[1][2]), 1)

    def test_group_label_without_name(self):
        Forwarder(self.api, {SRC: [TGT]}, prefix_sender=False, prefix_group=True,
                  group_names={SRC: "Group A"}).handle(env({"message": "hi"}))
        self.assertEqual(self.api.sent[0][1], "[Group A]: hi")

    def test_link_preview_forwarded(self):
        self.api.files["thumb"] = b"jpegbytes"
        self.f.handle(env({"message": "look https://ex.com/a", "previews": [{
            "url": "https://ex.com/a", "title": "Ex", "description": "Desc",
            "image": {"id": "thumb", "contentType": "image/jpeg"}}]}))
        self.assertEqual(self.api.previews[0], {
            "url": "https://ex.com/a", "title": "Ex", "description": "Desc",
            "base64_thumbnail": base64.b64encode(b"jpegbytes").decode()})

    def test_link_preview_without_image_or_url_in_text(self):
        self.f.handle(env({"message": "https://ex.com", "previews": [{"url": "https://ex.com", "title": "T"}]}))
        self.assertEqual(self.api.previews[0], {"url": "https://ex.com", "title": "T"})
        self.f.handle(env({"message": "no link", "previews": [{"url": "https://ex.com", "title": "T"}]}))
        self.assertIsNone(self.api.previews[1])

    def test_error_never_causes_duplicate_send(self):
        calls = []
        def failing(r, m, b=None, lp=None, edit_timestamp=None):
            calls.append(lp)
            raise RuntimeError("HTTP 400: something")
        self.api.send = failing
        self.f.handle(env({"message": "https://ex.com", "previews": [{"url": "https://ex.com", "title": "T"}]}))
        self.assertEqual(len(calls), 1)

    def test_partial_failure_response_still_records_timestamp(self):
        class Resp:
            def __init__(self, code, body): self.status_code, self._b = code, body
            ok = property(lambda self: self.status_code < 400)
            content = property(lambda self: b"x")
            text = property(lambda self: str(self._b))
            def json(self): return self._b
        class Sess:
            def __init__(self, resp): self.resp = resp
            def post(self, *a, **k): return self.resp
        api = SignalAPI("http://h", "+1", Sess(Resp(400, {"timestamp": "1730000000000", "error": "untrusted"})))
        self.assertEqual(api.send("group.x", "hi"), 1730000000000)
        api = SignalAPI("http://h", "+1", Sess(Resp(400, {"error": "boom"})))
        with self.assertRaises(RuntimeError):
            api.send("group.x", "hi")
        api = SignalAPI("http://h", "+1", Sess(Resp(201, {"timestamp": "42"})))
        self.assertEqual(api.send("group.x", "hi"), 42)
        # The shape seen on a real server: a list.
        api = SignalAPI("http://h", "+1", Sess(Resp(201, [{"timestamp": "1730261836000"}])))
        self.assertEqual(api.send("group.x", "hi"), 1730261836000)
        api = SignalAPI("http://h", "+1", Sess(Resp(201, [1730261836001])))
        self.assertIsNone(api.send("group.x", "hi"))  # unknown shape: no crash, just no timestamp
        api = SignalAPI("http://h", "+1", Sess(Resp(201, {"results": [{"timestamp": 7}]})))
        self.assertEqual(api.send("group.x", "hi"), 7)

    def test_delete_follows_to_all_targets(self):
        f = Forwarder(self.api, {SRC: ["group.A", "group.B"]})
        f.handle(env({"message": "oops", "timestamp": 777}))
        f.handle(env({"message": "keep", "timestamp": 778}))
        self.assertTrue(f.handle(env({"remoteDelete": {"timestamp": 777}})))
        self.assertEqual(self.api.deleted, [("group.A", 1001), ("group.B", 1002)])
        # Deleting again (or an unknown message) does nothing.
        self.assertFalse(f.handle(env({"remoteDelete": {"timestamp": 777}})))
        self.assertFalse(f.handle(env({"remoteDelete": {"timestamp": 123}})))

    def edit(self, f, target_ts, text, name="Alice"):
        return f.handle({"envelope": {"sourceName": name, "editMessage": {"targetSentTimestamp": target_ts,
            "dataMessage": {"message": text, "timestamp": target_ts + 50, "groupInfo": {"groupId": SRC}}}}})

    def test_edit_in_place(self):
        f = Forwarder(self.api, {SRC: ["group.A", "group.B"]}, prefix_sender=False)
        f.handle(env({"message": "v1", "timestamp": 900}))        # -> 1001 in A, 1002 in B
        self.assertTrue(self.edit(f, 900, "v2"))
        self.assertEqual(self.api.sent[2:], [("group.A", "v2", []), ("group.B", "v2", [])])
        self.assertEqual(self.api.edits[2:], [1001, 1002])
        self.assertTrue(self.edit(f, 900, "v3"))                  # a second edit targets the original too
        self.assertEqual(self.api.edits[4:], [1001, 1002])
        # Deleting the edited message deletes the one forwarded copy per group.
        f.handle(env({"remoteDelete": {"timestamp": 900}}))
        self.assertEqual(self.api.deleted, [("group.A", 1001), ("group.B", 1002)])

    def test_edit_of_unknown_message_falls_back(self):
        f = Forwarder(self.api, {SRC: [TGT]}, prefix_sender=False)
        self.edit(f, 12345, "changed")
        self.assertEqual(self.api.sent[0][1], "(edited) changed")
        self.assertEqual(self.api.edits[0], None)

    def test_deletes_can_be_disabled(self):
        f = Forwarder(self.api, {SRC: [TGT]}, forward_deletes=False)
        f.handle(env({"message": "x", "timestamp": 1}))
        self.assertFalse(f.handle(env({"remoteDelete": {"timestamp": 1}})))
        self.assertEqual(self.api.deleted, [])

    def test_store_persists(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "m.sqlite3")
        MessageStore(path).record("g", 1, "group.T", 42)
        self.assertEqual(MessageStore(path).pop("g", 1), [("group.T", 42)])

    def test_resolve_group(self):
        groups = [{"name": "Family", "id": "group.abc", "internal_id": "abc=="}]
        for key in ("Family", "group.abc", "abc=="):
            self.assertEqual(resolve_group(None, key, groups), ("group.abc", "abc==", "Family"))
        with self.assertRaises(SystemExit):
            resolve_group(None, "Nope", groups)

    def test_ws_url(self):
        self.assertEqual(SignalAPI("http://h:8080/", "+1").ws_url(), "ws://h:8080/v1/receive/+1")


if __name__ == "__main__":
    unittest.main()
