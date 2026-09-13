"""
api/index.py
------------
X Profile Lookup Telegram bot - WEBHOOK version for Vercel (serverless).

Why webhook, not polling (like the original bot.py):
    Vercel (and other serverless platforms) only runs code when a
    REQUEST comes in, then the process "dies". Polling mode
    (run_polling()) needs a process that stays alive CONTINUOUSLY to
    keep asking Telegram "any new messages?" - not compatible with
    serverless.

    Webhook mode, on the other hand, has TELEGRAM itself "push" (POST)
    every new message straight to one URL whenever someone messages the
    bot - this matches 100% how serverless works (the function runs when
    there's a request, replies, done). No continuous process needed, and
    (usually) completely free on Vercel.

IMPORTANT ROUTING NOTE: this file deliberately has NO vercel.json - it
uses Vercel's zero-config routing (a file under api/ is automatically
reachable at /api/index). The handler below is also deliberately a
"catch-all" (accepts any path) so it doesn't matter what path Vercel
actually assigns - this avoids a routing issue that happened before
(Flask received a different path than expected when using a custom
vercel.json).

Setup (see README.md for the full version):
    1. Deploy this folder to Vercel (import the repo / vercel CLI).
    2. Set the BOT_TOKEN environment variable (and WEBHOOK_SECRET -
       optional but recommended) in the Vercel project settings.
    3. Register the webhook ONCE after deploying:
       curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<project>.vercel.app/api/index&secret_token=<WEBHOOK_SECRET>"
"""

import html
import io
import json
import logging
import os
from typing import Optional

from flask import Flask, jsonify, request

from scraper import (
    ProfileInfo,
    ProfileNotFound,
    download_avatar_png,
    extract_username,
    fetch_profile,
    probe_domain_candidates,
    slugify,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
# Optional but recommended: a random value you choose yourself,
# registered once via setWebhook (the secret_token parameter). Telegram
# will send this value back in the "X-Telegram-Bot-Api-Secret-Token"
# header every time it calls this webhook - we check it matches so no
# one else can "spam" this endpoint with fake payloads if the URL leaks.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# Optional - makes this a PRIVATE bot. Comma-separated Telegram numeric
# user IDs, e.g. "111111111,222222222". When set, only these users can
# use the bot - everyone else gets a short message showing THEIR OWN id
# so they can send it to the owner to request access (self-service, no
# need to dig it out of Telegram settings). When left empty (default),
# the bot is public - anyone can use it, exactly like before this
# feature existed.
ALLOWED_USER_IDS = os.environ.get("ALLOWED_USER_IDS", "")
_ALLOWED_USER_ID_SET = {
    int(uid.strip()) for uid in ALLOWED_USER_IDS.split(",") if uid.strip().isdigit()
}


def _is_allowed(user_id) -> bool:
    # No whitelist configured at all -> public bot, everyone allowed.
    if not _ALLOWED_USER_ID_SET:
        return True
    return user_id in _ALLOWED_USER_ID_SET


TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Use 'requests' directly (not the python-telegram-bot library) - lighter
# weight and easier to run in a short-lived, sync serverless function
# like this, avoiding python-telegram-bot's asyncio complexity in a sync
# WSGI context.
import requests  # noqa: E402


def tg_send_message(chat_id, text, parse_mode="HTML", reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=8)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to sendMessage to chat_id=%s", chat_id)


def tg_send_photo(chat_id, photo_bytes: io.BytesIO, caption, parse_mode="HTML", reply_markup=None):
    photo_bytes.seek(0)
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": parse_mode}
    if reply_markup:
        # sendPhoto is sent as multipart/form-data (because of the file),
        # so reply_markup has to be JSON-encoded into a plain string field
        # here, unlike sendMessage's JSON body above where a nested dict
        # is fine as-is.
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(
            f"{TELEGRAM_API}/sendPhoto",
            data=data,
            files={"photo": ("logo.png", photo_bytes, "image/png")},
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to sendPhoto to chat_id=%s", chat_id)


def tg_answer_callback_query(callback_query_id, text=None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json=payload, timeout=8)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to answerCallbackQuery id=%s", callback_query_id)


def tg_edit_message_reply_markup(chat_id, message_id, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": reply_markup or {"inline_keyboard": []},
    }
    try:
        requests.post(f"{TELEGRAM_API}/editMessageReplyMarkup", json=payload, timeout=8)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to editMessageReplyMarkup chat_id=%s message_id=%s", chat_id, message_id)


# Max length Telegram allows for a button's callback_data, in bytes. Kept
# well under that (see _domain_guess_callback_data below) so the
# username + a slugified display name both fit comfortably.
_CALLBACK_DATA_MAX_BYTES = 64


def _domain_guess_callback_data(username: str, name: Optional[str]) -> str:
    name_slug = (slugify(name) or "")[:30]
    data = f"dg:{username}:{name_slug}"
    # Simple truncation is fine here - worst case we lose a few characters
    # of the name slug, which just narrows the guess slightly rather than
    # breaking anything.
    return data[:_CALLBACK_DATA_MAX_BYTES]


def _domain_guess_button(username: str, name: Optional[str]) -> dict:
    return {
        "inline_keyboard": [[
            {
                "text": "🔍 Force Website",
                "callback_data": _domain_guess_callback_data(username, name),
            }
        ]]
    }


def _html_escape(text: str) -> str:
    return html.escape(text, quote=False)


def build_caption(profile: ProfileInfo, username: str) -> str:
    name = profile.name or f"@{username}"
    # All values (including description) are wrapped in a <code> tag -
    # in Telegram apps, monospace/code-styled text like this can be
    # TAPPED to copy directly, no manual select-all needed.
    description_line = (
        f"<code>{_html_escape(profile.description)}</code>"
        if profile.description
        else "<i>(no bio)</i>"
    )
    website_line = (
        f"<code>{_html_escape(profile.website)}</code>"
        if profile.website
        else "<i>(no website detected)</i>"
    )
    profile_link = f"https://x.com/{profile.username}"

    # Order as requested: Desc -> Link X -> Username -> Website ->
    # (the logo is sent as the photo itself; this caption becomes the
    # text below that photo).
    lines = [
        f"<b><code>{_html_escape(name)}</code></b>",
        "",
        f"📝 Desc: {description_line}",
        "",
        f"🔗 Link X: <code>{_html_escape(profile_link)}</code>",
        "",
        f"👤 Username: <code>@{_html_escape(profile.username)}</code>",
        "",
        f"🌐 Website: {website_line}",
    ]
    return "\n".join(lines)


def handle_text_message(chat_id: int, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return

    if text.startswith("/start"):
        tg_send_message(
            chat_id,
            "Hi! Just send an X (Twitter) profile link or username, "
            "e.g. https://x.com/openai or @openai\n\n"
            "I'll reply with the name, description, website, and logo (PNG).",
        )
        return

    username = extract_username(text)
    if not username:
        tg_send_message(
            chat_id,
            "Couldn't detect an X username in that message. Try sending it as "
            "https://x.com/username, @username, or just the username.",
        )
        return

    try:
        profile = fetch_profile(username)
    except ProfileNotFound as exc:
        tg_send_message(chat_id, str(exc))
        return
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error during fetch_profile(%s)", username)
        tg_send_message(chat_id, "An unexpected error occurred while fetching this profile. Please try again shortly.")
        return

    caption = build_caption(profile, username)

    # Only offer the "force-check a domain" button when no website was
    # found any other way - if one was already found (X's own data, or
    # even the bio-text fallback), there's nothing to guess.
    reply_markup = None
    if not profile.website:
        reply_markup = _domain_guess_button(profile.username, profile.name)

    if profile.avatar_url:
        try:
            png_bytes = download_avatar_png(profile.avatar_url)
            tg_send_photo(chat_id, png_bytes, caption, reply_markup=reply_markup)
            return
        except Exception:  # noqa: BLE001
            logger.exception("Failed to download/send avatar for %s", username)

    tg_send_message(
        chat_id,
        caption + "\n\n<i>(couldn't download the logo)</i>",
        reply_markup=reply_markup,
    )


def handle_callback_query(callback_query: dict) -> None:
    """
    Handles a tap on the "Force-check a domain" button (see
    _domain_guess_button above). Guessing a domain is deliberately NOT
    part of the automatic fetch_profile() chain - it's a genuine guess
    (probing common TLDs for the account's name), not data read from X,
    and testing showed real false-positive risk (parked/for-sale domains,
    or an unrelated company that just happens to share the name). Keeping
    it behind an explicit button means it only ever runs when someone
    consciously asks for it, and the result is always labeled as a guess.
    """
    callback_id = callback_query.get("id")
    data = callback_query.get("data", "") or ""
    message = callback_query.get("message", {}) or {}
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    if not data.startswith("dg:"):
        tg_answer_callback_query(callback_id)
        return

    parts = data.split(":", 2)
    username = parts[1] if len(parts) > 1 else ""
    name_slug = parts[2] if len(parts) > 2 else ""

    # Ack immediately (Telegram shows a small loading spinner on the
    # button until this is called) and remove the button right away so
    # it can't be tapped again while the probe is still running.
    tg_answer_callback_query(callback_id, text="Checking domains...")
    if chat_id is not None and message_id is not None:
        tg_edit_message_reply_markup(chat_id, message_id, reply_markup=None)

    if not username:
        return

    try:
        candidates = probe_domain_candidates(username, name_slug or None)
    except Exception:  # noqa: BLE001
        logger.exception("probe_domain_candidates crashed for %s", username)
        candidates = []

    if chat_id is None:
        return

    tg_send_message(chat_id, _build_domain_check_report(username, candidates))


_STATUS_ICON = {"live": "✅", "parked": "⚠️", "dead": "❌"}


def _build_domain_check_report(username: str, candidates: list) -> str:
    if not candidates:
        return (
            f"🔍 Domain check for <code>@{_html_escape(username)}</code>: "
            f"<i>couldn't build any domain candidates.</i>"
        )

    lines = [f"🔍 Domain check for <code>@{_html_escape(username)}</code>:", ""]
    for domain, status, url in candidates:
        icon = _STATUS_ICON.get(status, "❌")
        line = f"{icon} <code>{_html_escape(domain)}</code>"
        if status == "live":
            line += f" — {_html_escape(url)}"
        elif status == "parked":
            line += " — parked/for sale"
        lines.append(line)

    lines += [
        "",
        "<i>Check one of these yourself before trusting it.</i>",
    ]
    return "\n".join(lines)


@app.route("/", defaults={"_path": ""}, methods=["GET", "POST"])
@app.route("/<path:_path>", methods=["GET", "POST"])
def catch_all(_path):
    if request.method == "GET":
        return "X Profile Telegram bot webhook is alive.", 200

    if WEBHOOK_SECRET:
        incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if incoming != WEBHOOK_SECRET:
            logger.warning("Webhook secret mismatch - request rejected.")
            return jsonify(ok=False), 403

    update = request.get_json(silent=True) or {}

    callback_query = update.get("callback_query")
    if callback_query:
        caller_id = (callback_query.get("from") or {}).get("id")
        if not _is_allowed(caller_id):
            # Don't run the domain check for a non-whitelisted user, but
            # still answer the callback so their button doesn't just spin
            # forever - a short toast is enough here (no need to show
            # their id again, they'd have already seen it from /start or
            # a text message).
            tg_answer_callback_query(
                callback_query.get("id"), text="This bot is private."
            )
            return jsonify(ok=True)
        try:
            handle_callback_query(callback_query)
        except Exception:  # noqa: BLE001
            logger.exception("Uncaught error while processing callback_query")
        return jsonify(ok=True)

    message = update.get("message") or update.get("edited_message")
    if not message:
        # Other update types (e.g. channel_post) - just ignore them.
        return jsonify(ok=True)

    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")

    if chat_id is None:
        return jsonify(ok=True)

    sender_id = (message.get("from") or {}).get("id")
    # Diagnostic log - shows how many IDs were parsed from
    # ALLOWED_USER_IDS (never the values themselves) and whether this
    # specific sender was allowed. If whitelist_count is 0 when you
    # expect it not to be, the env var isn't reaching this function at
    # runtime (config/redeploy issue) - that's the first thing to check,
    # same as the earlier X_AUTH_TOKEN/X_CT0 diagnostic.
    logger.info(
        "whitelist check: whitelist_count=%s sender_id=%s allowed=%s",
        len(_ALLOWED_USER_ID_SET), sender_id, _is_allowed(sender_id),
    )
    if not _is_allowed(sender_id):
        tg_send_message(
            chat_id,
            "🔒 This bot is private.\n\n"
            f"Your Telegram ID is <code>{sender_id}</code> - send it to the "
            "bot owner to request access.",
        )
        return jsonify(ok=True)

    try:
        handle_text_message(chat_id, text)
    except Exception:  # noqa: BLE001
        logger.exception("Uncaught error while processing message from chat_id=%s", chat_id)
        tg_send_message(chat_id, "Sorry, an unexpected error occurred. Please try sending it again shortly.")

    # Always reply 200 to Telegram so it doesn't retry / think the webhook is down.
    return jsonify(ok=True)
