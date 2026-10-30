# Signal group message forwarder

Copies messages between Signal groups: one group into one or more others, or back and forth between groups. Text, attachments, link previews, @mentions, quoted replies and edits are all carried over, and each forwarded message can start with the sender's name (`PREFIX_SENDER=true`) or carry only the content (`PREFIX_SENDER=false`). It runs on [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) in `json-rpc` mode, so new messages arrive over a websocket and are forwarded right away.

Set up steps below

1. **Start the Signal API**
   ```sh
   docker compose up -d signal-api
   ```

2. **Connect a Signal account.** You can use either of these:
   - *Link as a secondary device* (simplest). Open `http://localhost:8080/v1/qrcodelink?device_name=forwarder` and scan the QR code in Signal → Settings → Linked devices. Forwarded messages will then appear as sent by you.
   - *Use a dedicated bot number* (recommended, so forwarded messages don't look like they came from you). Register the number through the API's `/v1/register` and `/v1/register/{number}/verify` endpoints. A captcha may be required; see the signal-cli-rest-api docs.

   The account you use must be a member of both groups.

3. **Configure**
   ```sh
   cp .env.example .env      # set SIGNAL_NUMBER first
   docker compose run --rm forwarder python forwarder.py --list-groups
   ```
   Copy the source and target group names (or ids) into `.env`. To forward to several groups, separate the targets with commas:
   ```
   SOURCE_GROUP=Announcements
   TARGET_GROUP=Team A, Team B, group.xyz123...
   ```
   If a group name itself contains a comma, use its `group.…` id instead.

   **Two-way forwarding.** Add `BIDIRECTIONAL=true` and messages in the target group(s) also flow back to the source. For anything more complex, use `ROUTES`, where `->` means one way and `<->` means both ways:
   ```
   ROUTES=Group A <-> Group B
   ROUTES=Announcements -> Team A, Team B; Team A <-> Team B
   PREFIX_GROUP=true     # "Alice [Group A]: hi", or "[Group A]: hi" with PREFIX_SENDER=false
   ```
   Messages the bot posts are never forwarded again, so two-way routes don't loop. A message is only ever forwarded one hop: with `A -> B` and `B -> C`, a message posted in A reaches B but not C.

4. **Run**
   ```sh
   docker compose up -d --build
   docker compose logs -f forwarder
   ```

## Notes
- **Edits:** editing a message in a source group edits the forwarded copy (it shows as "Edited" in Signal). If the bot doesn't know the original (forwarded before this feature, or older than `MAP_RETENTION_DAYS`), it posts the new text as a separate "(edited)" message instead.
- **Deletes:** when someone uses "Delete for everyone" in a source group, the bot deletes its forwarded copies in the target groups. It keeps a small database in `./data` to know which copies belong to which original, so keep that folder mounted. Messages forwarded before this feature was added, or older than `MAP_RETENTION_DAYS`, can't be deleted this way. Set `FORWARD_DELETES=false` to turn it off.
- Link previews are copied from the original message (title, description, thumbnail). This needs a signal-cli-rest-api version with `link_preview` support on `/v2/send`; on older versions the message is still forwarded, just without the preview.
- Reactions, deletions, typing indicators and read receipts are not forwarded.
- If you linked your own account, the messages *you* send in the source group arrive as sync messages, so they are not forwarded. If you need them forwarded, use a dedicated bot number.
- Only one client should read messages from the account. Don't run another `receive` loop against the same number.
- Without Docker: `pip install -r requirements.txt`, then `SIGNAL_API_URL=http://localhost:8080 SIGNAL_NUMBER=... SOURCE_GROUP=... TARGET_GROUP=... python forwarder.py`.
- Tests: `python -m unittest test_forwarder.py`
