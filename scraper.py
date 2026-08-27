"""
scraper.py
----------
Logik untuk ambil maklumat profile X (Twitter) berdasarkan username/URL:
- nama paparan (display name)
- description / bio
- website (link dalam bio)
- avatar / logo (URL gambar resolusi penuh)

X (dulu Twitter) sekarang sangat ketat pasal scraping page utama sebab ia
"single page app" yang di-render guna JavaScript, dan selalunya minta login
untuk lihat profile penuh. Sebab tu kod ni cuba BEBERAPA sumber secara
berturutan (fallback chain), sebab mana-mana satu boleh je kena block/down
bila-bila masa, dan setiap satu ada titik lemah berbeza:

  1. Meta-tag (og:title/og:description/og:image) dari x.com/twitter.com
     terus, guna User-Agent macam bot preview (Telegram/Twitterbot/
     Discordbot dll). X memang benarkan bot jenis ni "intip" og-tags untuk
     tujuan link-preview walaupun browser biasa kena login wall - ini
     sebab preview link X dalam Telegram/WhatsApp selalu jalan walaupun
     page sebenar minta login. TAPI: og:image kadang-kadang banner
     (gambar cover), bukan avatar/logo bulat, dan tiada field "website"
     berasingan (link website di bio X berlainan drpd teks bio).
  2. Twitter "syndication" JSON endpoint (dipakai oleh widget embed rasmi
     Twitter/X sendiri) - bagi avatar + website yang betul (field
     berasingan), tapi endpoint ni sebenarnya endpoint "timeline", jadi
     akaun yang tiada tweet langsung / baru / kena had kadang-kadang tak
     keluar dalam response walaupun akaun tu wujud.
  3. Nitter mirrors (front-end alternatif untuk X) - guna kalau (1) & (2)
     gagal. NOTA: kebanyakan instance nitter awam dah mati/di-block sejak
     X mengetatkan akses, jadi lapisan ni paling tak boleh diharap sekarang
     - anggap ia sebagai last resort sahaja, bukan sumber utama.

Kalau semua gagal, fungsi akan return error yang jelas supaya bot boleh
maklumkan user. ProfileInfo.source akan diisi dengan nama sumber yang
berjaya - bot.py papar ni sekali supaya senang nak debug bila sesuatu akaun
"tak jumpa" walaupun akaun tu wujud.
"""

from __future__ import annotations

import io
import re
import logging
from dataclasses import dataclass
from typing import Optional

import requests
from PIL import Image

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# User-Agent yang meniru bot "link preview" (Telegram/Twitterbot/Discordbot).
# X secara sengaja benarkan bot jenis ni baca og:tags untuk keperluan
# unfurl link, walaupun browser/scraper biasa selalu kena "log in to view
# this profile" wall. Ini sebab preview link X dalam Telegram tetap boleh
# tunjuk nama/bio/gambar walaupun page sebenar minta login.
# Disenaraikan pendek (2, bukan 3+) supaya jumlah percubaan x timeout tak
# lebih had masa fungsi serverless (contoh Vercel Hobby plan ~10s).
BOT_USER_AGENTS = [
    "Mozilla/5.0 (compatible; TelegramBot (like TwitterBot))",
    "Twitterbot/1.0",
]

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}

# Kedua-dua host ni kadang berlainan nasib (satu block, satu tak).
PROFILE_HOSTS = ["https://x.com", "https://twitter.com"]

# Senarai nitter mirror sebagai fallback. Nitter instance selalu naik-turun,
# jadi list ni patut disemak/dikemaskini dari semasa ke semasa.
# Rujuk: https://github.com/zedeus/nitter/wiki/Instances
NITTER_MIRRORS = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]

# Nota deploy Vercel: fungsi serverless ada had masa (Hobby plan lazimnya
# ~10s). fetch_profile() cuba beberapa host/User-Agent secara berturutan,
# jadi timeout per-request kena singkat supaya jumlah keseluruhan tak
# lebih had tu bila beberapa percubaan gagal berturutan.
REQUEST_TIMEOUT = 6


@dataclass
class ProfileInfo:
    username: str
    name: Optional[str] = None
    description: Optional[str] = None
    website: Optional[str] = None
    avatar_url: Optional[str] = None
    source: Optional[str] = None  # untuk debugging - dari mana data ni datang


class ProfileNotFound(Exception):
    """Bila username langsung tak jumpa di mana-mana sumber."""


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def extract_username(text: str) -> Optional[str]:
    """
    Terima input bebas dari user - boleh jadi:
      - https://x.com/username
      - https://twitter.com/username?query=...
      - x.com/username/
      - @username
      - username
    Pulangkan username sahaja (tanpa @), atau None kalau tak jumpa.
    """
    text = text.strip()

    # Cuba pattern URL dulu
    m = re.search(
        r"(?:https?://)?(?:www\.)?(?:x|twitter)\.com/(@?[A-Za-z0-9_]{1,15})"
        r"(?:[/?#].*)?$",
        text,
        re.IGNORECASE,
    )
    if m:
        candidate = m.group(1).lstrip("@")
    else:
        candidate = text.lstrip("@")

    # Elak "tangkap" path yang bukan username sebenar (contoh: x.com/home,
    # x.com/i/..., x.com/search) - ni tak boleh dielak 100% sebab X guna
    # laluan yang sama untuk reserved words, tapi kita check format asas je.
    if _USERNAME_RE.match(candidate):
        return candidate
    return None


def _full_res_avatar(url: str) -> str:
    """
    Avatar dari API/HTML biasanya versi kecil (contoh: ..._normal.jpg,
    saiz 48x48). Tukar ke versi resolusi lebih besar (400x400).
    """
    if not url:
        return url
    return re.sub(r"_normal(?=\.\w+$)", "_400x400", url)


def _field(user: dict, key: str, default=None):
    """
    Baca satu field dari objek user syndication API - tapi X kerap tukar
    "bentuk" response ni (kadang field terus kat top-level, kadang
    dibungkus dalam sub-objek "legacy": {...}, ikut versi backend mana
    yang deploy endpoint ni pada masa tu). Fungsi ni cuba DUA-DUA bentuk
    supaya kod tak "buta" bila X tukar bentuk tanpa notis - ni punca
    biasa kenapa website/bio tiba-tiba tak dikesan walaupun akaun tu
    memang ada.
    """
    if key in user:
        return user[key]
    legacy = user.get("legacy") or {}
    return legacy.get(key, default)


def _resolve_short_url(short_url: str) -> Optional[str]:
    """Ikut redirect link t.co (atau apa-apa short URL) untuk dapat URL sebenar."""
    if not short_url:
        return None
    try:
        resp = requests.head(
            short_url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        return resp.url or short_url
    except Exception:  # noqa: BLE001
        return short_url


def _try_syndication_api(username: str) -> Optional[ProfileInfo]:
    """
    Guna endpoint syndication rasmi yang dipakai oleh widget "Follow Button"
    dan embed timeline X. Tak perlu API key/login.
    """
    url = "https://cdn.syndication.twimg.com/timeline/profile"
    params = {
        "screen_name": username,
        "dnt": "true",
    }
    try:
        resp = requests.get(
            url, params=params, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT
        )
        if resp.status_code != 200:
            logger.info("syndication API status %s untuk %s", resp.status_code, username)
            return None
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.info("syndication API gagal untuk %s: %s", username, exc)
        return None

    users = (data or {}).get("globalObjects", {}).get("users", {})
    match = None
    for user in users.values():
        if str(_field(user, "screen_name", "")).lower() == username.lower():
            match = user
            break
    # NOTA: sengaja TIADA fallback "ambil user pertama" kalau tiada
    # padanan tepat - dulu ada, tapi ni boleh diam-diam pulangkan data
    # akaun yang SALAH (contoh: akaun lain yang muncul dalam timeline
    # sebab retweet), lagi teruk drpd tak jumpa langsung.
    if match is None:
        return None

    website = None
    entities_urls = (
        _field(match, "entities", {}).get("url", {}).get("urls", [])
        if isinstance(_field(match, "entities", {}), dict)
        else []
    )
    if entities_urls:
        website = entities_urls[0].get("expanded_url")
    else:
        # Fallback: field "url" ringkas (link t.co bio) ada tapi
        # "entities.url.urls" tu kosong - cuba ikut redirect terus.
        short_url = _field(match, "url")
        if short_url:
            website = _resolve_short_url(short_url)

    return ProfileInfo(
        username=_field(match, "screen_name", username),
        name=_field(match, "name"),
        description=_field(match, "description"),
        website=website,
        avatar_url=_full_res_avatar(_field(match, "profile_image_url_https", "")),
        source="syndication",
    )


def _try_ogtags_direct(username: str) -> Optional[ProfileInfo]:
    """
    Baca meta og:tags terus dari x.com/twitter.com, guna User-Agent yang
    meniru bot link-preview (rujuk nota BOT_USER_AGENTS kat atas). Ini
    approach yang sama macam Telegram/WhatsApp guna untuk hasilkan
    "card" preview bila korang paste link X.

    Had: og:image kadang banner (bukan avatar bulat), dan tiada field
    "website" berasingan sebab website di bio X bukan sebahagian og:tags.
    """
    for host in PROFILE_HOSTS:
        for ua in BOT_USER_AGENTS:
            headers = {"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9"}
            try:
                resp = requests.get(
                    f"{host}/{username}", headers=headers, timeout=REQUEST_TIMEOUT
                )
                if resp.status_code != 200:
                    logger.info(
                        "og-scrape %s (%s) status %s untuk %s", host, ua, resp.status_code, username
                    )
                    continue
                html = resp.text
            except Exception as exc:  # noqa: BLE001
                logger.info("og-scrape %s (%s) gagal untuk %s: %s", host, ua, username, exc)
                continue

            title_m = _OG_TITLE_RE.search(html)
            desc_m = _OG_DESC_RE.search(html)
            image_m = _OG_IMAGE_RE.search(html)

            if not (title_m or desc_m or image_m):
                continue

            name = None
            if title_m:
                # Format biasa: "Nama (@handle) on X" / "Nama (@handle) / Twitter"
                name = re.split(r"\s*\(@|\s+/\s+", title_m.group(1))[0].strip()

            return ProfileInfo(
                username=username,
                name=name,
                description=desc_m.group(1) if desc_m else None,
                website=None,  # og:tags x.com tak dedah field website berasingan
                avatar_url=image_m.group(1) if image_m else None,
                source=f"og-scrape:{host}",
            )
    return None


_OG_TITLE_RE = re.compile(
    r'<meta property="og:title" content="([^"]*)"', re.IGNORECASE
)
_OG_DESC_RE = re.compile(
    r'<meta property="og:description" content="([^"]*)"', re.IGNORECASE
)
_OG_IMAGE_RE = re.compile(
    r'<meta property="og:image" content="([^"]*)"', re.IGNORECASE
)
_NITTER_WEBSITE_RE = re.compile(
    r'class="profile-website"[^>]*>\s*<a[^>]*href="([^"]+)"', re.IGNORECASE
)


def _try_nitter(username: str) -> Optional[ProfileInfo]:
    for base in NITTER_MIRRORS:
        try:
            resp = requests.get(
                f"{base}/{username}", headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT
            )
            if resp.status_code != 200:
                continue
            html = resp.text
        except Exception as exc:  # noqa: BLE001
            logger.info("Nitter mirror %s gagal untuk %s: %s", base, username, exc)
            continue

        title_m = _OG_TITLE_RE.search(html)
        desc_m = _OG_DESC_RE.search(html)
        image_m = _OG_IMAGE_RE.search(html)
        website_m = _NITTER_WEBSITE_RE.search(html)

        if not (title_m or desc_m or image_m):
            continue

        name = title_m.group(1).split(" / ")[0].strip() if title_m else None

        return ProfileInfo(
            username=username,
            name=name,
            description=desc_m.group(1) if desc_m else None,
            website=website_m.group(1) if website_m else None,
            avatar_url=image_m.group(1) if image_m else None,
            source=f"nitter:{base}",
        )
    return None


def fetch_profile(username: str) -> ProfileInfo:
    """
    Cuba dapatkan maklumat profile dari sumber-sumber yang tersedia,
    ikut turutan keutamaan. Raise ProfileNotFound kalau semua gagal.

    Sumber pertama yang berjaya jadi "asas" (name/description/avatar).
    Kalau asas tu tak bagi field "website" (contoh: og-scrape memang
    tak sokong field ni), kita cuba lengkapkan dari syndication API
    secara berasingan supaya hasil akhir selengkap mungkin.
    """
    result: Optional[ProfileInfo] = None
    for fetcher in (_try_ogtags_direct, _try_syndication_api, _try_nitter):
        try:
            result = fetcher(username)
        except Exception:  # noqa: BLE001
            logger.exception("Fetcher %s crash untuk %s", fetcher.__name__, username)
            result = None
        if result is not None:
            break

    if result is None:
        raise ProfileNotFound(
            f"Tak dapat cari profile @{username}. Mungkin akaun tak wujud, "
            f"private, atau semua sumber (og-scrape, syndication API, nitter) "
            f"sedang di-block/down/rate-limited waktu ni."
        )

    if not result.website and result.source and not result.source.startswith("syndication"):
        try:
            extra = _try_syndication_api(username)
        except Exception:  # noqa: BLE001
            extra = None
        if extra and extra.website:
            result.website = extra.website

    return result


def download_avatar_png(avatar_url: str) -> io.BytesIO:
    """
    Muat turun avatar dan pulangkan sebagai PNG dalam BytesIO,
    tak kira format asal (jpg/webp/png).
    """
    resp = requests.get(avatar_url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
    out = io.BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    out.name = "logo.png"
    return out
