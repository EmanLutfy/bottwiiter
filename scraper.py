"""
scraper.py
----------
Logic for fetching X (Twitter) profile info from a username/URL:
- display name
- description / bio
- website (link in bio)
- avatar / logo (full-resolution image URL)

X (formerly Twitter) is now very strict about scraping its main page,
since it's a JavaScript-rendered "single page app" and usually requires
login to view a full profile. That's why this code tries SEVERAL sources
in sequence (a fallback chain) - any one of them can get blocked/go down
at any time, and each has a different weak point:

  1. Meta tags (og:title/og:description/og:image) fetched directly from
     x.com/twitter.com, using a User-Agent that mimics a link-preview bot
     (Telegram/Twitterbot/Discordbot etc). X deliberately allows these
     bots to read og:tags for link-unfurling purposes, even though a
     regular browser/scraper hits a "log in to view this profile" wall -
     this is why X link previews in Telegram/WhatsApp always work even
     though the real page demands login. BUT: og:image is sometimes the
     banner (cover image), not the round avatar, and there's no separate
     "website" field (the website link in an X bio is different from the
     bio text itself).
  2. Twitter's "syndication" JSON endpoint (used by the official
     Twitter/X "Follow Button" and timeline embed widgets) - gives the
     correct avatar + website (a separate field), but this endpoint is
     actually a "timeline" endpoint, so an account with no tweets at
     all / a new account / a rate-limited account sometimes doesn't show
     up in the response even though the account exists.
  3. Nitter mirrors (an alternative front-end for X) - used if (1) and
     (2) fail. NOTE: most public nitter instances have died/been blocked
     since X tightened access, so this layer is the least reliable right
     now - treat it as a last resort only, not a primary source.

If everything fails, the function raises a clear error so the bot can
inform the user. ProfileInfo.source is filled with the name of whichever
source succeeded - bot.py displays this too, to make it easy to debug
when some account is "not found" even though it exists.
"""

from __future__ import annotations

import io
import json
import os
import re
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

import requests
from PIL import Image

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# User-Agent that mimics a "link preview" bot (Telegram/Twitterbot/
# Discordbot). X deliberately allows this kind of bot to read og:tags
# for link-unfurling purposes, even though a regular browser/scraper
# always hits a "log in to view this profile" wall. This is why an X
# link preview in Telegram still shows name/bio/image even though the
# real page demands login.
# Kept short (2, not 3+) so the number of attempts x timeout doesn't
# exceed a serverless function's time limit (e.g. Vercel Hobby plan ~10s).
BOT_USER_AGENTS = [
    "Mozilla/5.0 (compatible; TelegramBot (like TwitterBot))",
    "Twitterbot/1.0",
]

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}

# These two hosts sometimes have different luck (one blocked, one not).
PROFILE_HOSTS = ["https://x.com", "https://twitter.com"]

# List of nitter mirrors used as a fallback. Nitter instances constantly
# go up and down, so this list should be reviewed/updated periodically.
# See: https://github.com/zedeus/nitter/wiki/Instances
NITTER_MIRRORS = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]

# Vercel deployment note: serverless functions have a time limit (Hobby
# plan is typically ~10s). fetch_profile() tries several hosts/User-Agents
# in sequence, so the per-request timeout needs to be short so the total
# doesn't exceed that limit when several attempts fail in a row.
REQUEST_TIMEOUT = 6


@dataclass
class ProfileInfo:
    username: str
    name: Optional[str] = None
    description: Optional[str] = None
    website: Optional[str] = None
    avatar_url: Optional[str] = None
    source: Optional[str] = None  # for debugging - where this data came from


class ProfileNotFound(Exception):
    """Raised when the username simply can't be found from any source."""


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def extract_username(text: str) -> Optional[str]:
    """
    Accepts free-form input from the user - can be:
      - https://x.com/username
      - https://twitter.com/username?query=...
      - x.com/username/
      - @username
      - username
    Returns the username only (without @), or None if not found.
    """
    text = text.strip()

    # Try the URL pattern first
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

    # Avoid "catching" a path that isn't a real username (e.g. x.com/home,
    # x.com/i/..., x.com/search) - this can't be fully avoided since X
    # uses the same path scheme for reserved words, so we just check the
    # basic format.
    if _USERNAME_RE.match(candidate):
        return candidate
    return None


def _full_res_avatar(url: str) -> str:
    """
    The avatar from the API/HTML is usually a small version (e.g.
    ..._normal.jpg, 48x48). Swap it for a larger resolution (400x400).
    """
    if not url:
        return url
    return re.sub(r"_normal(?=\.\w+$)", "_400x400", url)


def _field(user: dict, key: str, default=None):
    """
    Read one field from a syndication API user object - but X frequently
    changes the "shape" of this response (sometimes a field sits at the
    top level, sometimes it's wrapped in a "legacy": {...} sub-object,
    depending on which backend version is serving this endpoint at the
    time). This function tries BOTH shapes so the code isn't "blind" when
    X changes shape without notice - this is a common reason why
    website/bio suddenly stops being detected even though the account
    genuinely has one.
    """
    if key in user:
        return user[key]
    legacy = user.get("legacy") or {}
    return legacy.get(key, default)


def _resolve_short_url(short_url: str) -> Optional[str]:
    """Follow a t.co (or any other short URL) redirect to get the real URL."""
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
    Uses the official syndication endpoint used by the "Follow Button"
    widget and X's timeline embed. No API key/login required.
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
            logger.info("syndication API status %s for %s", resp.status_code, username)
            return None
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.info("syndication API failed for %s: %s", username, exc)
        return None

    users = (data or {}).get("globalObjects", {}).get("users", {})
    match = None
    for user in users.values():
        if str(_field(user, "screen_name", "")).lower() == username.lower():
            match = user
            break
    # NOTE: deliberately NO "take the first user" fallback when there's
    # no exact match - there used to be one, but it could silently return
    # data for the WRONG account (e.g. some other account that showed up
    # in the timeline because of a retweet), which is worse than not
    # finding anything at all.
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
        # Fallback: the short "url" field (bio t.co link) is present but
        # "entities.url.urls" is empty - try following the redirect directly.
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
    Reads og:tags meta directly from x.com/twitter.com, using a User-Agent
    that mimics a link-preview bot (see the BOT_USER_AGENTS note above).
    This is the same approach Telegram/WhatsApp use to produce a "card"
    preview when you paste an X link.

    Limitation: og:image is sometimes the banner (not a round avatar),
    and there's no separate "website" field since the website in an X
    bio isn't part of og:tags.
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
                        "og-scrape %s (%s) status %s for %s", host, ua, resp.status_code, username
                    )
                    continue
                html = resp.text
            except Exception as exc:  # noqa: BLE001
                logger.info("og-scrape %s (%s) failed for %s: %s", host, ua, username, exc)
                continue

            title_m = _OG_TITLE_RE.search(html)
            desc_m = _OG_DESC_RE.search(html)
            image_m = _OG_IMAGE_RE.search(html)

            if not (title_m or desc_m or image_m):
                continue

            name = None
            if title_m:
                # Common format: "Name (@handle) on X" / "Name (@handle) / Twitter"
                name = re.split(r"\s*\(@|\s+/\s+", title_m.group(1))[0].strip()

            return ProfileInfo(
                username=username,
                name=name,
                description=desc_m.group(1) if desc_m else None,
                website=None,  # x.com's og:tags don't expose a separate website field
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
            logger.info("Nitter mirror %s failed for %s: %s", base, username, exc)
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


# Official X API (OPTIONAL, pay-per-use - see README). Only used when the
# X_BEARER_TOKEN env var is set; otherwise this function just returns
# None and the code falls back to the free fallbacks (link found in bio).
# This is currently the MOST reliable source for the separate "Website"
# field, since the syndication API is dead and nitter can't be relied on
# either - X itself guarantees this data is correct.
X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")


def _try_x_official_api(username: str) -> Optional[ProfileInfo]:
    if not X_BEARER_TOKEN:
        return None

    url = f"https://api.twitter.com/2/users/by/username/{username}"
    params = {"user.fields": "description,profile_image_url,url,entities"}
    headers = {**DEFAULT_HEADERS, "Authorization": f"Bearer {X_BEARER_TOKEN}"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.info("official X API status %s for %s", resp.status_code, username)
            return None
        data = (resp.json() or {}).get("data")
    except Exception as exc:  # noqa: BLE001
        logger.info("official X API failed for %s: %s", username, exc)
        return None

    if not data:
        return None

    website = None
    entities_urls = data.get("entities", {}).get("url", {}).get("urls", [])
    if entities_urls:
        website = entities_urls[0].get("expanded_url")
    elif data.get("url"):
        website = _resolve_short_url(data["url"])

    return ProfileInfo(
        username=data.get("username", username),
        name=data.get("name"),
        description=data.get("description"),
        website=website,
        avatar_url=_full_res_avatar(data.get("profile_image_url", "")),
        source="x-api-official",
    )


# Cookie-based access to X's own internal GraphQL API (OPTIONAL). This is
# currently the ONLY genuinely free option that reliably includes the
# separate "Website" field - it's the exact same API X's own web app
# calls to render a profile page, so whatever X shows in the browser is
# what this returns. It requires a *logged-in* X account's session
# cookies (auth_token + ct0), NOT an official/paid API key.
#
# IMPORTANT: use a secondary/"burner" X account for this, never your main
# personal account. Automated access like this is against X's Terms of
# Service, and the account whose cookies are used here carries some risk
# of being rate-limited or suspended. The bot itself is unaffected either
# way - only that one X account is at risk.
#
# How to get these two values:
#   1. Log into x.com in a normal browser, using the burner account.
#   2. Open DevTools (F12) -> Application/Storage tab -> Cookies ->
#      https://x.com.
#   3. Copy the value of the "auth_token" cookie into X_AUTH_TOKEN.
#   4. Copy the value of the "ct0" cookie into X_CT0.
#   5. Set both as environment variables. If either is missing, this
#      fetcher just no-ops (returns None) and the rest of the fallback
#      chain behaves exactly as before - zero behavior change by default.
#
# These cookies expire after a while (the exact lifetime varies) - if
# this fetcher works for a period and then quietly stops, the cookie has
# most likely expired and needs to be refreshed the same way.
#
# Fragility warning: X changes the GraphQL "queryId" and the required
# "features" flags for this endpoint from time to time without notice -
# if this stops working even with fresh cookies, that's the first thing
# to check/update (search "UserByScreenName queryId" for a current value
# from an actively maintained open-source X/Twitter scraper).
X_AUTH_TOKEN = os.environ.get("X_AUTH_TOKEN", "")
X_CT0 = os.environ.get("X_CT0", "")

# The "public" bearer token baked into X's own web app JavaScript bundle -
# not a developer/paid API key. This exact value has been publicly known
# and reused by open-source X/Twitter scrapers for years.
_GRAPHQL_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# Query ID for the "UserByScreenName" GraphQL operation.
_USER_BY_SCREEN_NAME_QUERY_ID = "sLVLhk0bGj3MVFEKTdax1w"

# X's GraphQL API requires a large block of boolean "feature flags" to be
# sent with every request, or it rejects the request outright. This list
# can go stale when X adds/removes flags - if requests start failing with
# a "cannot be null" style error, that's usually a missing/renamed flag.
_GRAPHQL_FEATURES = {
    "hidden_profile_subscriptions_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}


def _try_x_graphql_cookie(username: str) -> Optional[ProfileInfo]:
    if not (X_AUTH_TOKEN and X_CT0):
        return None

    url = f"https://x.com/i/api/graphql/{_USER_BY_SCREEN_NAME_QUERY_ID}/UserByScreenName"
    variables = {
        "screen_name": username,
        "withSafetyModeUserFields": True,
        "withHighlightedLabel": True,
    }
    params = {
        "variables": json.dumps(variables),
        "features": json.dumps(_GRAPHQL_FEATURES),
    }
    headers = {
        **DEFAULT_HEADERS,
        "Authorization": _GRAPHQL_BEARER,
        "x-csrf-token": X_CT0,
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": "en",
    }
    cookies = {"auth_token": X_AUTH_TOKEN, "ct0": X_CT0}

    try:
        resp = requests.get(
            url, params=params, headers=headers, cookies=cookies, timeout=REQUEST_TIMEOUT
        )
        if resp.status_code != 200:
            logger.info("GraphQL cookie API status %s for %s", resp.status_code, username)
            return None
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.info("GraphQL cookie API failed for %s: %s", username, exc)
        return None

    # Defensive parsing: X has reshuffled this response schema more than
    # once - some fields sit directly under "legacy", others have moved
    # into nested objects like "core" on newer schema versions. Any
    # unexpected shape here just falls through to None (safe no-op)
    # rather than raising, same philosophy as the rest of this module.
    try:
        result = data["data"]["user"]["result"]
        legacy = result.get("legacy", {}) or {}
        core = result.get("core", {}) or {}

        def _pick(*dicts_and_keys):
            for d, k in dicts_and_keys:
                if d and d.get(k):
                    return d.get(k)
            return None

        name = _pick((core, "name"), (legacy, "name"))
        screen_name = _pick((core, "screen_name"), (legacy, "screen_name")) or username
        description = legacy.get("description")

        website = None
        entities_urls = (legacy.get("entities", {}) or {}).get("url", {}).get("urls", [])
        if entities_urls:
            website = entities_urls[0].get("expanded_url")
        elif legacy.get("url"):
            website = _resolve_short_url(legacy["url"])

        avatar = (
            (result.get("avatar", {}) or {}).get("image_url")
            or legacy.get("profile_image_url_https")
        )
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected GraphQL cookie API response shape for %s", username)
        return None

    if not (name or description or website or avatar):
        return None

    return ProfileInfo(
        username=screen_name,
        name=name,
        description=description,
        website=website,
        avatar_url=_full_res_avatar(avatar or ""),
        source="x-graphql-cookie",
    )


def fetch_profile(username: str) -> ProfileInfo:
    """
    Tries to get profile info from the available sources, in priority
    order. Raises ProfileNotFound if everything fails.

    Whichever source succeeds first becomes the "base" (name/description/
    avatar). If that base doesn't provide a "website" field (e.g.
    og-scrape simply doesn't support this field), we try to fill it in
    separately from the syndication API so the final result is as
    complete as possible.
    """
    result: Optional[ProfileInfo] = None

    # If configured, try the cookie-based GraphQL fetcher FIRST - it's
    # the only source that reliably includes the separate "Website" field
    # AND is free (see the X_AUTH_TOKEN/X_CT0 comment above), and it can
    # supply name/description/avatar too, in a single request. If not
    # configured, this is a cheap no-op (one env var check) and the rest
    # of the chain behaves exactly as it did before this fetcher existed.
    graphql_cookie_tried = bool(X_AUTH_TOKEN and X_CT0)
    # Diagnostic log - only prints LENGTHS (never the actual secret
    # values), so it's safe to leave on and check in Vercel logs. If both
    # lengths show 0, the env vars aren't reaching this function at
    # runtime (config/redeploy issue on the platform side, not a code
    # bug) - that's the first thing to fix before anything else here
    # matters.
    logger.info(
        "graphql cookie config check: X_AUTH_TOKEN len=%s, X_CT0 len=%s, configured=%s",
        len(X_AUTH_TOKEN), len(X_CT0), graphql_cookie_tried,
    )
    if graphql_cookie_tried:
        try:
            result = _try_x_graphql_cookie(username)
        except Exception:  # noqa: BLE001
            logger.exception("GraphQL cookie fetcher crashed for %s", username)
            result = None

    if result is None:
        for fetcher in (_try_ogtags_direct, _try_syndication_api, _try_nitter):
            try:
                result = fetcher(username)
            except Exception:  # noqa: BLE001
                logger.exception("Fetcher %s crashed for %s", fetcher.__name__, username)
                result = None
            if result is not None:
                break

    if result is None:
        raise ProfileNotFound(
            f"Couldn't find profile @{username}. The account might not exist, "
            f"be private, or every source (og-scrape, syndication API, nitter"
            f"{', GraphQL cookie API' if graphql_cookie_tried else ''}) "
            f"might currently be blocked/down/rate-limited."
        )

    if not result.website:
        already_tried_syndication = result.source == "syndication"
        already_tried_nitter = bool(result.source and result.source.startswith("nitter"))
        candidates = []
        # The free cookie-based GraphQL fetcher is tried first (if not
        # already tried as the base above), then the official paid X API
        # (if X_BEARER_TOKEN is set) - both are far more reliable for the
        # separate "Website" field than the sources below.
        if not graphql_cookie_tried:
            candidates.append(_try_x_graphql_cookie)
        candidates.append(_try_x_official_api)
        if not already_tried_syndication:
            candidates.append(_try_syndication_api)
        if not already_tried_nitter:
            candidates.append(_try_nitter)

        for extra_fetcher in candidates:
            try:
                extra = extra_fetcher(username)
            except Exception:  # noqa: BLE001
                extra = None
            if extra and extra.website:
                result.website = extra.website
                logger.info(
                    "Website filled in from %s for %s",
                    getattr(extra_fetcher, "__name__", "extra_fetcher"),
                    username,
                )
                break

    if not result.website and result.description:
        # Last resort: the syndication API (the "official" website field)
        # is getting less and less reliable - the endpoint itself
        # frequently returns an empty response now. A lot of accounts
        # just put a link directly in their bio text (e.g.
        # "Try now: https://t.co/xxx") - we grab the FIRST link in the
        # bio as a website substitute. Not 100% the same as X's separate
        # "website" field, but it's the most useful, trustworthy link
        # available given that we've already successfully read that bio.
        bio_url = _extract_url_from_text(result.description)
        if bio_url:
            result.website = (
                _resolve_short_url(bio_url) if "t.co/" in bio_url else bio_url
            )
            logger.info("Website extracted from bio text for %s", username)

    return result


_URL_IN_TEXT_RE = re.compile(r"https?://\S+")


def _extract_url_from_text(text: str) -> Optional[str]:
    """Find the FIRST link that appears in a piece of text (e.g. a bio)."""
    if not text:
        return None
    m = _URL_IN_TEXT_RE.search(text)
    if not m:
        return None
    # Strip trailing punctuation that might have been swept up but isn't
    # actually part of the URL (e.g. a sentence-ending period).
    return m.group(0).rstrip(".,;:!?)]}\"'")


# ---------------------------------------------------------------------------
# On-demand domain guessing ("Force-check domain" button)
#
# This is NOT part of fetch_profile()'s automatic chain - it's a GUESS, not
# data read from X, and it showed real false-positive risk in testing (a
# parked/for-sale domain, or a completely unrelated company that happens to
# share the name). Because of that it's kept as an explicit, user-triggered
# action: api/index.py shows a button only when no website was found any
# other way, and this function only runs when someone taps it.
# ---------------------------------------------------------------------------

# TLDs to try, in this order, when guessing a domain. Order matters only
# for which candidate wins when more than one resolves - all candidates
# are still probed concurrently, not one-by-one.
GUESS_TLDS = [".xyz", ".com", ".fun", ".io", ".space", ".tech", ".family"]

# Snippets that suggest a domain is just parked/for-sale rather than a
# real site for this account - used to avoid confidently reporting a
# domain-for-sale page as someone's "website". Not exhaustive.
_PARKING_PAGE_MARKERS = (
    "domain is for sale",
    "this domain may be for sale",
    "buy this domain",
    "domain parking",
    "the owner of this domain",
    "godaddy.com/domains",
    "sedo.com",
    "dan.com",
)


def slugify(text: Optional[str]) -> Optional[str]:
    """Turn a name/username into a bare domain-label candidate (lowercase
    letters/digits only, no spaces/punctuation/emoji)."""
    if not text:
        return None
    slug = re.sub(r"[^a-z0-9]", "", text.lower())
    return slug or None


def _looks_like_parking_page(body: str) -> bool:
    lowered = body.lower()
    return any(marker in lowered for marker in _PARKING_PAGE_MARKERS)


def _probe_domain(domain: str) -> tuple[str, Optional[str]]:
    """
    Checks whether https://{domain} resolves to a page, and what kind.
    Returns (status, url):
      - ("live", final_url)   - resolves, and doesn't look parked/for-sale
      - ("parked", final_url) - resolves, but looks like a parking/
                                 for-sale page (see _looks_like_parking_page)
      - ("dead", None)        - doesn't resolve / times out / errors / 4xx+
    Deliberately HTTPS-only and a short timeout - this runs as part of a
    concurrent batch of probes (see probe_domain_candidates), so keeping
    each probe cheap matters for staying inside a serverless function's
    time budget.
    """
    try:
        resp = requests.get(
            f"https://{domain}",
            headers=DEFAULT_HEADERS,
            timeout=3,
            allow_redirects=True,
        )
        if resp.status_code < 400:
            if _looks_like_parking_page(resp.text[:3000]):
                return ("parked", resp.url)
            return ("live", resp.url)
    except Exception:  # noqa: BLE001
        pass
    return ("dead", None)


def probe_domain_candidates(
    username: str, name_slug: Optional[str] = None
) -> list[tuple[str, str, Optional[str]]]:
    """
    Builds domain candidates from the username and (optionally) an
    already-slugified display name, e.g. username "OverweightMkt" +
    name_slug "overweightmarket" -> overweightmkt.xyz, overweightmarket.xyz,
    overweightmkt.com, ... and probes ALL of them.

    Returns EVERY result (not just the first hit) as a list of
    (domain, status, url) tuples in priority order, so a human can look
    at the whole picture and judge which one (if any) is really theirs -
    this deliberately does not pick a "winner" itself, since testing
    showed real false-positive risk (a parked domain, or an unrelated
    company that happens to share the name).

    Called ONLY on-demand (see the module docstring above this section) -
    never from fetch_profile().
    """
    slugs = []
    for candidate in (slugify(username), slugify(name_slug)):
        if candidate and candidate not in slugs:
            slugs.append(candidate)

    domains = []
    for slug in slugs:
        for tld in GUESS_TLDS:
            domain = f"{slug}{tld}"
            if domain not in domains:
                domains.append(domain)

    if not domains:
        return []

    # Probe every candidate concurrently so the total wall time stays
    # close to a single request's timeout rather than N x timeout - this
    # matters a lot on a serverless platform with a strict execution
    # time limit.
    results: dict[str, tuple[str, Optional[str]]] = {}
    with ThreadPoolExecutor(max_workers=len(domains)) as pool:
        future_to_domain = {pool.submit(_probe_domain, d): d for d in domains}
        for future in as_completed(future_to_domain):
            domain = future_to_domain[future]
            try:
                results[domain] = future.result()
            except Exception:  # noqa: BLE001
                results[domain] = ("dead", None)

    # Preserve priority order (slug order, then TLD order) rather than
    # "whichever finished first" - a concurrent probe can complete out
    # of order.
    return [(d, results[d][0], results[d][1]) for d in domains]


def download_avatar_png(avatar_url: str) -> io.BytesIO:
    """
    Downloads the avatar and returns it as a PNG in a BytesIO,
    regardless of the original format (jpg/webp/png).
    """
    resp = requests.get(avatar_url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
    out = io.BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    out.name = "logo.png"
    return out
