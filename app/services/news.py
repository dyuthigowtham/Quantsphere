import asyncio
import calendar
import html
import logging
import re
from datetime import datetime, timezone

import feedparser
import httpx

logger = logging.getLogger("quantsphere.news")

_IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE
)
_OG_IMAGE_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']', re.IGNORECASE
)

_CACHE_TTL_SECONDS = 180.0
_MAX_ARTICLES_PER_CATEGORY = 20
_OG_FETCH_CONCURRENCY = 8
_OG_FETCH_MAX_BYTES = 200_000
_OG_FETCH_TIMEOUT_SECONDS = 5.0

# Official RSS feeds only — RSS is explicitly published by these outlets for
# third-party syndication, unlike scraping a page or an undocumented JSON
# endpoint. "International" and "national" (India, matching the NSE/BSE/
# NIFTY/SENSEX coverage already in the app) each draw from two publishers so
# one feed going stale/down doesn't blank the whole category.
_FEEDS: dict[str, list[tuple[str, str]]] = {
    "international": [
        ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
        ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ],
    "national": [
        ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
        ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ],
}


def _extract_image(entry: dict) -> str | None:
    """
    Purpose:    Best-effort thumbnail URL for one RSS entry, trying every
                format these publishers actually use before giving up —
                article headlines feel far more "catchy" with an image, and
                most of these feeds already carry one.
    Args:       entry (dict): A feedparser entry.
    Returns:    str | None: An image URL, or None if the feed has no image
                    for this entry (e.g. CNBC's feed carries no images at all).
    Raises:     None.
    """
    media_content = entry.get("media_content") or []
    if media_content and media_content[0].get("url"):
        return media_content[0]["url"]

    media_thumbnail = entry.get("media_thumbnail") or []
    if media_thumbnail and media_thumbnail[0].get("url"):
        return media_thumbnail[0]["url"]

    for link in entry.get("links", []):
        if link.get("rel") == "enclosure" and link.get("type", "").startswith("image/"):
            return link.get("href")

    match = _IMG_SRC_RE.search(entry.get("summary", ""))
    if match:
        return match.group(1)

    return None


def _parse_published(entry: dict) -> datetime | None:
    """
    Purpose:    Best-effort published/updated timestamp from a feedparser
                entry, as a UTC datetime. Uses feedparser's own pre-parsed
                *_parsed struct_time (already UTC-normalized) rather than
                hand-parsing the raw date string, since publishers use
                different formats — CNBC's RSS uses RFC-822 ("Sun, 23 Aug
                2026 06:39:00 GMT"), Yahoo Finance's uses ISO 8601
                ("2026-08-22T18:47:00Z") — and feedparser already handles both.
    Args:       entry (dict): A feedparser entry.
    Returns:    datetime | None: UTC timestamp, or None if the entry has
                    neither a published nor updated date.
    Raises:     None.
    """
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return None
    return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)


async def _fetch_feed(client: httpx.AsyncClient, source: str, url: str) -> list[dict]:
    """
    Purpose:    Fetch and parse one RSS feed into a list of plain article dicts.
    Args:       client (httpx.AsyncClient): Shared async HTTP client.
                source (str): Human-readable publisher name for attribution.
                url (str): The feed's URL.
    Returns:    list[dict]: Articles with title/link/source/published_at.
                    Empty list on any failure — one bad feed shouldn't blank
                    the whole category.
    Raises:     None.
    """
    try:
        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        return [
            {
                "title": entry.title,
                "link": entry.link,
                "source": source,
                "published_at": _parse_published(entry),
                "image": _extract_image(entry),
            }
            for entry in parsed.entries
            if entry.get("title") and entry.get("link")
        ]
    except Exception:
        logger.warning("Failed to fetch/parse news feed %s (%s)", source, url, exc_info=True)
        return []


async def _fetch_og_image(client: httpx.AsyncClient, semaphore: asyncio.Semaphore, url: str) -> str | None:
    """
    Purpose:    Fallback thumbnail for articles whose RSS entry carried no
                image (e.g. CNBC's feed never includes one) — almost every
                publisher still tags its own article pages with an
                `og:image` meta tag for social-media link previews, so this
                works regardless of which feed is missing image data.
                Streams the response and stops as soon as `</head>` (or the
                og:image tag itself) is seen, so this never pulls a full
                article page over the wire.
    Args:       client (httpx.AsyncClient): Shared async HTTP client.
                semaphore (asyncio.Semaphore): Caps concurrent article fetches.
                url (str): The article's own page URL (not the feed URL).
    Returns:    str | None: The og:image URL, or None on any failure/absence.
    Raises:     None.
    """
    try:
        async with semaphore:
            async with client.stream(
                "GET", url, headers={"User-Agent": "Mozilla/5.0"}, timeout=_OG_FETCH_TIMEOUT_SECONDS
            ) as response:
                if response.status_code >= 400:
                    return None
                buffer = b""
                async for chunk in response.aiter_bytes():
                    buffer += chunk
                    text = buffer.decode("utf-8", errors="ignore")
                    match = _OG_IMAGE_RE.search(text) or _OG_IMAGE_RE_ALT.search(text)
                    if match:
                        return html.unescape(match.group(1))
                    if b"</head>" in buffer or len(buffer) >= _OG_FETCH_MAX_BYTES:
                        break
        return None
    except Exception:
        return None


class NewsCache:
    """
    Purpose:    Time-boxed cache of parsed RSS headlines per category, so the
                dashboard's news panel doesn't re-fetch/re-parse every
                publisher's feed on every page load — RSS updates on the
                order of minutes, not seconds. Injected via app.dependencies
                rather than used as a module global, matching PriceCache.
    """

    def __init__(self) -> None:
        """
        Purpose:    Initialize with no cached categories yet.
        Args:       None.
        Returns:    None.
        Raises:     None.
        """
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._lock = asyncio.Lock()

    async def get_articles(self, category: str) -> list[dict]:
        """
        Purpose:    Fetch (or return cached) headlines for one news category.
        Args:       category (str): "international" or "national".
        Returns:    list[dict]: Articles, newest first, capped and deduped.
        Raises:     ValueError: If category isn't a recognized feed group.
        """
        if category not in _FEEDS:
            raise ValueError(f"Unknown news category: {category!r}")

        async with self._lock:
            cached = self._cache.get(category)
            now = asyncio.get_event_loop().time()
            if cached and now - cached[0] < _CACHE_TTL_SECONDS:
                return cached[1]

        async with httpx.AsyncClient(timeout=10.0) as client:
            results = await asyncio.gather(*(_fetch_feed(client, source, url) for source, url in _FEEDS[category]))

            seen_links: set[str] = set()
            articles: list[dict] = []
            for feed_articles in results:
                for article in feed_articles:
                    if article["link"] in seen_links:
                        continue
                    seen_links.add(article["link"])
                    articles.append(article)

            articles.sort(key=lambda a: a["published_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            articles = articles[:_MAX_ARTICLES_PER_CATEGORY]

            missing = [article for article in articles if not article["image"]]
            if missing:
                semaphore = asyncio.Semaphore(_OG_FETCH_CONCURRENCY)
                fetched_images = await asyncio.gather(
                    *(_fetch_og_image(client, semaphore, article["link"]) for article in missing)
                )
                for article, image in zip(missing, fetched_images):
                    article["image"] = image

        async with self._lock:
            self._cache[category] = (asyncio.get_event_loop().time(), articles)
        return articles
