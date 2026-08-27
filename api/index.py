"""
api/index.py
------------
Bot Telegram X Profile Lookup - versi WEBHOOK untuk Vercel (serverless).

Kenapa webhook, bukan polling (macam bot.py versi asal):
    Vercel (dan platform serverless lain) hanya jalankan kod bila ada
    REQUEST masuk, lepas tu proses tu "mati". Bot mod polling
    (run_polling()) perlukan proses yang idup BERTERUSAN untuk asyik
    tanya Telegram "ada mesej baru tak?" - tak serasi dengan serverless.

    Mod webhook plak, TELEGRAM sendiri yang "push" (POST) setiap mesej
    baru terus ke satu URL bila-bila masa ada orang mesej bot - ni
    padan 100% dengan cara serverless berfungsi (function run bila ada
    request, reply, habis). Tiada proses berterusan diperlukan, dan
    (lazimnya) percuma sepenuhnya di Vercel.

NOTA PENTING pasal routing: fail ni sengaja TIADA vercel.json - guna
zero-config routing Vercel (fail dalam api/ automatik reachable di
/api/index). Handler kat bawah pun sengaja "catch-all" (terima sebarang
path) supaya tak kisah macam mana Vercel assign path sebenar - ni elak
isu routing yang pernah jadi sebelum ni (Flask dapat path lain drpd yang
disangka bila guna vercel.json custom).

Setup (rujuk README.md untuk penuh):
    1. Deploy folder ni ke Vercel (import repo / vercel CLI).
    2. Set environment variable BOT_TOKEN (dan WEBHOOK_SECRET - optional
       tapi disyorkan) dalam Vercel project settings.
    3. Daftar webhook SEKALI je lepas deploy:
       curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<project>.vercel.app/api/index&secret_token=<WEBHOOK_SECRET>"
"""

import html
import io
import logging
import os

from flask import Flask, jsonify, request

from scraper import ProfileInfo, ProfileNotFound, download_avatar_png, extract_username, fetch_profile

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
# Optional tapi disyorkan: nilai rawak korang pilih sendiri, didaftarkan
# sekali masa setWebhook (parameter secret_token). Telegram akan hantar
# balik nilai ni dalam header "X-Telegram-Bot-Api-Secret-Token" setiap
# kali dia panggil webhook ni - kita check ia sepadan supaya orang lain
# tak boleh "spam" endpoint ni dengan payload palsu kalau URL ni bocor.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Guna 'requests' terus (bukan library python-telegram-bot) - lagi ringan
# & senang jalan dalam function serverless yang sync/pendek hayat macam
# ni, elak kerumitan asyncio python-telegram-bot dalam konteks WSGI sync.
import requests  # noqa: E402


def tg_send_message(chat_id, text, parse_mode="HTML"):
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=8,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Gagal sendMessage ke chat_id=%s", chat_id)


def tg_send_photo(chat_id, photo_bytes: io.BytesIO, caption, parse_mode="HTML"):
    photo_bytes.seek(0)
    try:
        requests.post(
            f"{TELEGRAM_API}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": parse_mode},
            files={"photo": ("logo.png", photo_bytes, "image/png")},
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Gagal sendPhoto ke chat_id=%s", chat_id)


def _html_escape(text: str) -> str:
    return html.escape(text, quote=False)


def build_caption(profile: ProfileInfo, username: str) -> str:
    name = profile.name or f"@{username}"
    # Semua nilai (termasuk description) di-wrap dalam tag <code> - dalam
    # app Telegram, teks bergaya monospace/code macam ni boleh terus
    # di-TAP untuk copy terus, tak payah select-all manual.
    description_line = (
        f"<code>{_html_escape(profile.description)}</code>"
        if profile.description
        else "<i>(tiada bio)</i>"
    )
    website_line = (
        f"<code>{_html_escape(profile.website)}</code>"
        if profile.website
        else "<i>(tiada website dikesan)</i>"
    )
    source_line = _html_escape(profile.source or "tidak diketahui")
    profile_link = f"https://x.com/{profile.username}"

    # Susunan ikut apa yang diminta: Desc -> Link X -> Username -> Website
    # -> (logo dihantar sebagai gambar itu sendiri, caption ni jadi teks
    # bawah gambar tu).
    lines = [
        f"<b>{_html_escape(name)}</b>",
        "",
        f"📝 Desc: {description_line}",
        "",
        f"🔗 Link X: <code>{_html_escape(profile_link)}</code>",
        "",
        f"👤 Username: <code>@{_html_escape(profile.username)}</code>",
        "",
        f"🌐 Website: {website_line}",
        "",
        f"<i>sumber: {source_line}</i>",
    ]
    return "\n".join(lines)


def handle_text_message(chat_id: int, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return

    if text.startswith("/start"):
        tg_send_message(
            chat_id,
            "Hai! Hantar je link profile X (Twitter) atau username, "
            "contoh: https://x.com/openai atau @openai\n\n"
            "Saya akan bagi balik nama, description, website, dan logo (PNG).",
        )
        return

    username = extract_username(text)
    if not username:
        tg_send_message(
            chat_id,
            "Tak dapat kesan username X dari mesej tu. Cuba hantar dalam bentuk "
            "https://x.com/username, @username, atau username je.",
        )
        return

    try:
        profile = fetch_profile(username)
    except ProfileNotFound as exc:
        tg_send_message(chat_id, str(exc))
        return
    except Exception:  # noqa: BLE001
        logger.exception("Ralat tak dijangka semasa fetch_profile(%s)", username)
        tg_send_message(chat_id, "Ada ralat tak dijangka semasa cuba dapatkan profile ni. Cuba lagi sekejap.")
        return

    caption = build_caption(profile, username)

    if profile.avatar_url:
        try:
            png_bytes = download_avatar_png(profile.avatar_url)
            tg_send_photo(chat_id, png_bytes, caption)
            return
        except Exception:  # noqa: BLE001
            logger.exception("Gagal muat turun/hantar avatar untuk %s", username)

    tg_send_message(chat_id, caption + "\n\n<i>(logo tak dapat dimuat turun)</i>")


@app.route("/", defaults={"_path": ""}, methods=["GET", "POST"])
@app.route("/<path:_path>", methods=["GET", "POST"])
def catch_all(_path):
    if request.method == "GET":
        return "X Profile Telegram bot webhook is alive.", 200

    if WEBHOOK_SECRET:
        incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if incoming != WEBHOOK_SECRET:
            logger.warning("Webhook secret tak sepadan - request ditolak.")
            return jsonify(ok=False), 403

    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message")
    if not message:
        # Update jenis lain (contoh: callback_query, channel_post) - abaikan sahaja.
        return jsonify(ok=True)

    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")

    if chat_id is None:
        return jsonify(ok=True)

    try:
        handle_text_message(chat_id, text)
    except Exception:  # noqa: BLE001
        logger.exception("Ralat tak ditangkap semasa proses mesej dari chat_id=%s", chat_id)
        tg_send_message(chat_id, "Maaf, ada ralat tak dijangka. Cuba hantar semula sekejap lagi.")

    # Sentiasa balas 200 kat Telegram supaya ia tak retry/anggap webhook down.
    return jsonify(ok=True)
