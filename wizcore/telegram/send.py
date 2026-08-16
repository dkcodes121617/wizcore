"""Telegram — send-only.

**There is no webhook anywhere in this system.** Approvals were removed
deliberately, and with no callback buttons a bot needs no public HTTPS endpoint,
no router, and no one-webhook-per-bot coordination between agents. Telegram
carries exactly three things:

  1. what happened — "published to Facebook + Threads, links below"
  2. what you have to do by hand — X threads and YouTube, formatted for copy-paste
  3. what broke — a failed platform, a tripped guard rail, a stalled lead

The one interactive case that survives (marking a hand-posted item done) is
handled by polling `getUpdates` inside an agent's own scheduled run — see
`poll_commands`. A command that takes up to 30 minutes to register is fine; a
public webhook to save those 30 minutes is not worth the surface area.

## DRY_RUN still sends

This is deliberate and it is not an exception to the kill switch. `DRY_RUN=1`
stops outward actions — publishing, emailing, anything the world sees. Telegram
is how *you* observe a dry run; suppressing it would mean the safe path produces
no evidence, and the rollout procedure is literally "read the Telegram output as
if you were the recipient". Dry-run messages are prefixed so they can never be
mistaken for the real thing.
"""
from __future__ import annotations

import html
import logging
import time

import requests

from wizcore.config import env_bool, env_str
from wizcore.obs.log import current_run_id, traceback_tail

log = logging.getLogger("wizcore.telegram")

_API = "https://api.telegram.org"
_MAX = 4096          # Telegram's hard message limit
_CHUNK = 3900        # leaves room for the prefix and a continuation marker


class TelegramError(RuntimeError):
    pass


def _token() -> str:
    return env_str("TELEGRAM_BOT_TOKEN")


def _split(raw: str) -> list[str]:
    return [c.strip() for c in raw.split(",") if c.strip()]


def _chats(audience: str = "") -> list[str]:
    """Recipients for one message. `TELEGRAM_CHAT_ID` accepts a comma list.

    One value stays one value, so nothing that already works changes. A second
    id means a second person sees the same message at the same time, which is
    what "tell me and my colleague" actually needs — forwarding is not a
    notification, and a bot cannot add itself to somebody's chat.

    `audience` adds the people who should see *only that kind* of message, from
    `TELEGRAM_CHAT_ID_<AUDIENCE>`. Someone helping with the posting should get
    the day's plan, what went out and what needs doing by hand — and should not
    get lead scores, prospect names or stack traces. Opt-in per message is what
    makes that true by default: a new alert added later reaches the owner only,
    because it has to name an audience to reach anyone else.

    Both parties must have STARTED the bot (Telegram refuses to message a user
    who has not). A dead id is skipped with a warning rather than failing the
    send, so one person leaving cannot silence the other.
    """
    ids = _split(env_str("TELEGRAM_CHAT_ID"))
    if audience:
        ids += _split(env_str(f"TELEGRAM_CHAT_ID_{audience.upper()}"))
    # Dedupe, keeping order: the owner listed in both must not get two copies.
    seen: set[str] = set()
    return [c for c in ids if not (c in seen or seen.add(c))]


def _chat() -> str:
    """The first recipient. Kept for callers that genuinely need one id."""
    chats = _chats()
    return chats[0] if chats else ""


def _fan_out(method: str, payload: dict, files: dict | None = None,
             audience: str = "") -> bool:
    """Send one message to every recipient. True if any of them received it.

    Any, not all: a bot blocked by one person must still reach the other. The
    alternative is that one muted chat silences the whole system.
    """
    chats = _chats(audience)
    if not chats:
        log.warning("TELEGRAM_CHAT_ID is not set; message dropped")
        return False
    sent = False
    for chat in chats:
        try:
            _post(method, {**payload, "chat_id": chat}, files)
            sent = True
        except Exception:
            log.warning("telegram %s failed for chat %s", method, chat, exc_info=True)
    return sent


def _topic(name: str) -> str:
    """`TELEGRAM_TOPIC_<NAME>`; blank falls back to the main chat.

    Forum topics are a nicety — one thread per agent so manual tasks do not
    scroll away underneath lead alerts. Everything must work before anyone sets
    them up, so an unset topic is not an error.
    """
    return env_str(f"TELEGRAM_TOPIC_{name.upper()}")


def esc(text: str) -> str:
    """Escape for parse_mode=HTML.

    HTML rather than MarkdownV2 on purpose: MarkdownV2 requires escaping 18
    characters, and these messages carry model-generated copy, URLs and
    tracebacks — arbitrary text. One missed underscore in a traceback would turn
    a failure alert into a 400, losing exactly the message that mattered.
    """
    return html.escape(str(text), quote=False)


def _post(method: str, payload: dict, files: dict | None = None, attempts: int = 4) -> dict:
    token = _token()
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
    url = f"{_API}/bot{token}/{method}"
    last = ""
    for i in range(attempts):
        try:
            resp = requests.post(url, data=payload, files=files, timeout=(10, 30))
        except requests.RequestException as e:
            last = f"network: {e}"
            time.sleep(2 * (i + 1))
            continue
        if resp.status_code == 200:
            return resp.json()
        last = f"HTTP {resp.status_code}: {resp.text[:300]}"
        if resp.status_code == 429:
            # Telegram tells you exactly how long to wait; guessing is how you
            # get rate-limited again immediately.
            wait = 5
            try:
                wait = int(resp.json().get("parameters", {}).get("retry_after", 5))
            except Exception:  # noqa: BLE001
                pass
            time.sleep(min(wait + 1, 60))
            continue
        if resp.status_code >= 500:
            time.sleep(2 * (i + 1))
            continue
        break   # 4xx other than 429 will not fix itself
    raise TelegramError(f"{method} failed: {last}")


def enabled() -> bool:
    return bool(_token() and _chat()) and env_bool("TELEGRAM_ENABLED", True)


def send(text: str, topic: str = "", dry_run: bool = False, silent: bool = False,
         audience: str = "") -> bool:
    """Send a message, chunked to Telegram's limit. Returns False if disabled.

    Never raises on a delivery failure: a notification that takes down the run it
    was reporting on is worse than a missed notification. The failure is logged.
    """
    if not enabled():
        log.info("telegram disabled; message dropped: %s", text[:120])
        return False
    if dry_run:
        text = "🧪 <b>DRY RUN</b> - nothing was published or sent\n\n" + text

    payload_base = {
        "parse_mode": "HTML",
        "link_preview_options": '{"is_disabled": true}',
        "disable_notification": silent,
    }
    thread = _topic(topic) if topic else ""
    if thread:
        payload_base["message_thread_id"] = thread

    ok = False
    for i, chunk in enumerate(_chunks(text)):
        payload = dict(payload_base, text=chunk)
        if i:
            payload["disable_notification"] = True   # only the first pings
        ok = _fan_out("sendMessage", payload, audience=audience) or ok
    return ok


def _chunks(text: str) -> list[str]:
    """Split on line boundaries where possible, never mid-HTML-tag.

    Splitting a message at a fixed offset can cut an <b> from its </b>, and
    Telegram rejects the whole message with a 400 rather than rendering it
    plainly — so the split has to respect line boundaries.
    """
    if len(text) <= _MAX:
        return [text]
    out, current = [], ""
    for line in text.split("\n"):
        # A single line longer than a chunk is rare (a URL, a base64 blob) and
        # has to be hard-split; there is no safe boundary inside it.
        while len(line) > _CHUNK:
            if current:
                out.append(current)
                current = ""
            out.append(line[:_CHUNK])
            line = line[_CHUNK:]
        if len(current) + len(line) + 1 > _CHUNK:
            out.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        out.append(current)
    return out


def send_photo(photo_url: str, caption: str = "", topic: str = "",
               dry_run: bool = False, audience: str = "") -> bool:
    if not enabled():
        return False
    if dry_run:
        caption = "🧪 DRY RUN - not published\n" + caption
    payload = {
        "photo": photo_url,
        "caption": caption[:1024],     # captions cap lower than messages
        "parse_mode": "HTML",
    }
    thread = _topic(topic) if topic else ""
    if thread:
        payload["message_thread_id"] = thread
    return _fan_out("sendPhoto", payload, audience=audience)


def send_album(photo_urls: list[str], caption: str = "", topic: str = "",
               dry_run: bool = False, audience: str = "") -> bool:
    """A carousel preview, as Telegram sees it. Max 10 per album."""
    if not enabled() or not photo_urls:
        return False
    import json as _json

    if dry_run:
        caption = "🧪 DRY RUN - not published\n" + caption
    media = []
    for i, url in enumerate(photo_urls[:10]):
        item = {"type": "photo", "media": url}
        if i == 0 and caption:
            item["caption"] = caption[:1024]
            item["parse_mode"] = "HTML"
        media.append(item)
    payload = {"media": _json.dumps(media)}
    thread = _topic(topic) if topic else ""
    if thread:
        payload["message_thread_id"] = thread
    return _fan_out("sendMediaGroup", payload, audience=audience)


def alert(agent: str, exc: BaseException, context: str = "") -> bool:
    """Unhandled failure → the alert topic, with run_id and a traceback tail.

    A silent failure on a weekly schedule is a month of nothing before anyone
    notices, which is why every agent wraps its entrypoint in this.
    """
    body = (
        f"🔴 <b>{esc(agent)} failed</b>\n"
        f"run_id: <code>{esc(current_run_id())}</code>\n"
    )
    if context:
        body += f"{esc(context)}\n"
    body += f"\n<pre>{esc(traceback_tail(exc))}</pre>"
    return send(body, topic="alerts")


def poll_commands(offset_file: str | None = None, limit: int = 40) -> list[dict]:
    """Drain `getUpdates` for the handful of commands the system accepts.

    This is the whole of the "interactive" surface, and it is pull-based on
    purpose (see the module docstring). Returns a list of
    `{"command", "args", "update_id"}`.

    Long-polling is not used: this runs inside a scheduled container that is
    about to exit, so it takes whatever is queued and moves on.
    """
    if not enabled():
        return []
    offset = 0
    if offset_file:
        try:
            with open(offset_file, encoding="utf-8") as fh:
                offset = int(fh.read().strip() or 0)
        except (OSError, ValueError):
            offset = 0
    try:
        data = _post(
            "getUpdates",
            {"offset": offset, "limit": limit, "timeout": 0,
             "allowed_updates": '["message"]'},
            attempts=2,
        )
    except Exception:  # noqa: BLE001
        log.warning("getUpdates failed", exc_info=True)
        return []

    out: list[dict] = []
    last_id = offset
    for upd in data.get("result", []):
        last_id = max(last_id, int(upd.get("update_id", 0)))
        text = ((upd.get("message") or {}).get("text") or "").strip()
        if not text.startswith("/"):
            continue
        head, _, args = text.partition(" ")
        out.append(
            {
                "command": head.split("@", 1)[0].lstrip("/").lower(),
                "args": args.strip(),
                "update_id": upd.get("update_id"),
            }
        )
    if offset_file and last_id:
        try:
            with open(offset_file, "w", encoding="utf-8") as fh:
                fh.write(str(last_id + 1))
        except OSError:
            log.warning("could not persist getUpdates offset")
    return out


def ping() -> tuple[bool, str]:
    """`getMe`, for a preflight node."""
    try:
        data = _post("getMe", {}, attempts=2)
        return True, "@" + data.get("result", {}).get("username", "?")
    except Exception as e:  # noqa: BLE001
        return False, repr(e)
