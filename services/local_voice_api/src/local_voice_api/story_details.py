"""Free, keyless details about one news story, for "tell me more" turns.

A headline feed carries one sentence per story, so every follow-up repeated it
(live calls 2026-10-11). Bing News RSS returns several outlets' reports on the
same story, each with a one- or two-sentence summary and the publisher's link,
and the publisher's page holds the full report. Only the headline is sent out,
never the user's words or audio. Pages are fetched from public addresses only,
with bounded size and time.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from email.utils import parsedate_to_datetime
import html
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import time
from collections.abc import Iterator, Mapping
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .retrieval import (
    RetrievalResult,
    StoryDetailsSource,
    _grounding_terms,
    _safe_result_url,
    _spoken_sentences,
    _story_keywords,
)

logger = logging.getLogger(__name__)

STORY_DETAILS_PROVIDER_ENVIRONMENT = "AIRA_STORY_DETAILS_PROVIDER"
BING_NEWS_RSS_URL = "https://www.bing.com/news/search"
DEFAULT_STORY_TIMEOUT_SECONDS = 2.0
MAX_STORY_FEED_BYTES = 256 * 1024
MAX_ARTICLE_BYTES = 1_500_000
MAX_ARTICLE_CANDIDATES = 3
MAX_ARTICLE_PARAGRAPHS = 10
MAX_PASSAGE_CHARACTERS = 360
MAX_REDIRECTS = 3
# Several publishers refuse unknown clients; this is a plain browser string.
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_INVISIBLE_CHARACTERS = re.compile("[­​-‏  ⁠﻿]")
_MARKUP = re.compile(r"<[^>]+>")
_RULE = re.compile(r"_{2,}")
# Wire datelines are not speech (live replay 2026-10-11 read out "NEW DELHI,
# Oct 11 (Reuters) - "; also "Washington/Dubai, Oct 11 (PTI)", "TAMPA, Fla. — ").
_DATELINE = re.compile(
    r"^(?:[A-Z][A-Za-z.'/ -]{1,40},\s+)?"
    r"(?:(?:[A-Z][a-z]{2,8}\.?\s+\d{1,2}|\d{1,2}\s+[A-Z][a-z]{2,8})(?:,\s+\d{4})?\s*)?"
    r"\([A-Za-z][A-Za-z .&-]{1,25}\)\s*[-–—:]?\s*|"
    r"^[A-Z]{2,}(?:[ /][A-Z]{2,})*,\s+[A-Z][A-Za-z.]{1,6}\s*[—–-]\s*"
)
# A lowercase word ending a sentence, glued to the next one; "U.S." is left alone.
_GLUED_SENTENCES = re.compile(r"(?<=[a-z0-9][.!?])(?=[A-Z“\"])|(?<=[a-z0-9][.!?][”\"])(?=[A-Z])")
_TRUNCATION = re.compile(r"\s*(?:\.{3}|…)\s*$")
_JSON_LD = re.compile(
    r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
# Subscription prompts, captions, bylines, and legal lines are not the story
# (seen on 2026-10-11: "Image: Bloomberg", "Get them on our Telegram channel").
_BOILERPLATE = re.compile(
    r"\b(?:subscribe|newsletter|sign\s+up|log\s*in|telegram|whatsapp\s+channel|"
    r"follow\s+us|cookies?|all\s+rights\s+reserved|click\s+here|advertisement|"
    r"also\s+read|read\s+more|read\s+also|download\s+the\s+app|copyright|"
    r"terms\s+of\s+(?:use|service)|privacy\s+policy|only\s+the\s+headline|"
    r"syndicated\s+feed|disclaimer|"
    r"this\s+(?:story|article)\s+(?:has\s+been|was)\s+(?:updated|corrected))\b|©|"
    r"^(?:image|photo|picture|video|file\s+photo|representative\s+image)\s*[:|-]",
    re.IGNORECASE,
)
_SKIPPED_ELEMENTS = frozenset(
    {
        "aside", "button", "figcaption", "figure", "footer", "form", "header",
        "iframe", "label", "nav", "noscript", "script", "select", "style", "svg",
        "template",
    }
)


class BingNewsStoryDetails:
    """Several outlets' summaries of a story plus one article's paragraphs."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_STORY_TIMEOUT_SECONDS,
        fetch_articles: bool = True,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Story details timeout must be positive.")
        self._timeout_seconds = timeout_seconds
        self._fetch_articles = fetch_articles

    async def details(
        self, headline: str, *, limit: int
    ) -> tuple[RetrievalResult, ...]:
        headline = " ".join(headline.split())
        if not headline or limit <= 0:
            return ()
        # The searches and the article fetch each get the timeout; this bounds both.
        return await asyncio.wait_for(
            asyncio.to_thread(self._details_sync, headline, limit),
            timeout=self._timeout_seconds * 2 + 1.0,
        )

    def _details_sync(self, headline: str, limit: int) -> tuple[RetrievalResult, ...]:
        started = time.perf_counter()
        story_terms = _grounding_terms(headline)
        reports = [
            report
            for report in self._search_all(_story_queries(headline))
            if _about_story(story_terms, report)
        ]
        passages = (
            self._article_passages(reports, story_terms)
            if self._fetch_articles
            else []
        )
        passages.extend(report for report in reports if report.snippet)
        logger.info(
            "[AIRA RETRIEVAL] story_details reports=%d passages=%d elapsed_ms=%.1f",
            len(reports),
            len(passages),
            (time.perf_counter() - started) * 1000,
        )
        return tuple(passages[:limit])

    def _search_all(self, queries: list[str]) -> list[RetrievalResult]:
        """Every query at once; the headline's own results come first."""

        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(queries),
            thread_name_prefix="aira-story-search",
        )
        try:
            futures = [pool.submit(self._search, query) for query in queries]
            concurrent.futures.wait(futures, timeout=self._timeout_seconds + 0.25)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        merged: list[RetrievalResult] = []
        seen: set[str] = set()
        errors: list[BaseException] = []
        for future in futures:
            if not future.done() or future.cancelled():
                continue
            error = future.exception()
            if error is not None:
                errors.append(error)
                continue
            for report in future.result():
                parts = urlsplit(report.url)
                key = f"{(parts.hostname or '').casefold()}{parts.path.rstrip('/')}"
                if key not in seen:
                    seen.add(key)
                    merged.append(report)
        if not merged and errors:
            raise errors[0]
        if not merged and not errors and any(not future.done() for future in futures):
            raise TimeoutError("Story search timed out.")
        return merged

    def _search(self, headline: str) -> tuple[RetrievalResult, ...]:
        url = f"{BING_NEWS_RSS_URL}?{urlencode({'q': headline, 'format': 'rss'})}"
        request = Request(url, headers={"User-Agent": _BROWSER_USER_AGENT})
        with urlopen(request, timeout=self._timeout_seconds) as response:
            raw = response.read(MAX_STORY_FEED_BYTES + 1)
        if len(raw) > MAX_STORY_FEED_BYTES:
            raw = raw[:MAX_STORY_FEED_BYTES]
            raw = raw[: raw.rfind(b"</item>") + len(b"</item>")] + b"</channel></rss>"
        return parse_bing_news_feed(raw)

    def _article_passages(
        self,
        reports: list[RetrievalResult],
        story_terms: frozenset[str],
    ) -> list[RetrievalResult]:
        candidates = reports[:MAX_ARTICLE_CANDIDATES]
        if not candidates:
            return []
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(candidates),
            thread_name_prefix="aira-story-article",
        )
        try:
            futures = [
                pool.submit(self._article, report, story_terms)
                for report in candidates
            ]
            concurrent.futures.wait(futures, timeout=self._timeout_seconds)
        finally:
            # Never wait for a slow publisher; its socket timeout ends it.
            pool.shutdown(wait=False, cancel_futures=True)
        # Bing's order is relevance order, so the first readable report wins.
        for future in futures:
            if future.done() and not future.cancelled() and future.exception() is None:
                passages = future.result()
                if passages:
                    return passages
        return []

    def _article(
        self,
        report: RetrievalResult,
        story_terms: frozenset[str],
    ) -> list[RetrievalResult]:
        page = fetch_public_html(report.url, timeout_seconds=self._timeout_seconds)
        if page is None:
            return []
        paragraphs = extract_article_paragraphs(page)
        if len(paragraphs) < 2:
            return []
        # A consent wall or an unrelated page must not stand in for the story.
        lead_terms = _grounding_terms(" ".join(paragraphs[:3]))
        if len(lead_terms & story_terms) < min(2, len(story_terms)):
            return []
        return [
            RetrievalResult(
                report.title,
                report.url,
                paragraph,
                published_at=report.published_at,
                engine="news article",
            )
            for paragraph in paragraphs
        ]


def story_details_from_environment(
    environ: Mapping[str, str] | None = None,
) -> StoryDetailsSource | None:
    source = os.environ if environ is None else environ
    provider = source.get(STORY_DETAILS_PROVIDER_ENVIRONMENT, "").strip().casefold()
    if provider in {"", "disabled", "none"}:
        return None
    if provider == "bing_news":
        return BingNewsStoryDetails()
    raise ValueError("AIRA_STORY_DETAILS_PROVIDER must be 'disabled' or 'bing_news'.")


def parse_bing_news_feed(raw: bytes) -> tuple[RetrievalResult, ...]:
    root = ElementTree.fromstring(raw)
    reports: list[RetrievalResult] = []
    for item in root.iter("item"):
        # Bing binds its "News:" prefix to a per-query namespace URI.
        fields = {
            child.tag.rsplit("}", 1)[-1].casefold(): child.text or ""
            for child in item
        }
        title = _clean_text(fields.get("title", ""))[:200]
        url = _publisher_url(fields.get("link", ""))
        published_at = None
        try:
            published_at = parsedate_to_datetime(fields.get("pubdate", "")).timestamp()
        except (TypeError, ValueError, IndexError):
            pass
        if title and url:
            reports.append(
                RetrievalResult(
                    title,
                    url,
                    _complete_summary(_clean_text(fields.get("description", ""))),
                    published_at=published_at,
                    engine="bing news",
                )
            )
    return tuple(reports)


def _story_queries(headline: str) -> list[str]:
    """The headline, then its content words, then the first six of those.

    Bing matches all words: a long headline can find only a couple of reports
    on a story its content words find ten of, and the reverse also happens.
    """

    keywords = _story_keywords(headline)
    queries: list[str] = []
    for query in (headline, " ".join(keywords), " ".join(keywords[:6])):
        if query and query not in queries:
            queries.append(query)
    return queries


def _publisher_url(link: str) -> str:
    """The publisher link inside Bing's click-tracking redirect."""

    parts = urlsplit(link.strip())
    host = (parts.hostname or "").casefold()
    if host == "bing.com" or host.endswith(".bing.com"):
        return _safe_result_url(parse_qs(parts.query).get("url", [""])[0])
    return _safe_result_url(link.strip())


def _about_story(story_terms: frozenset[str], report: RetrievalResult) -> bool:
    if not story_terms:
        return False
    overlap = len(story_terms & _grounding_terms(f"{report.title} {report.snippet}"))
    return overlap >= min(len(story_terms), max(2, (len(story_terms) * 2 + 4) // 5))


def _clean_text(value: str) -> str:
    text = html.unescape(_MARKUP.sub(" ", value))
    text = _INVISIBLE_CHARACTERS.sub("", text)
    # Structured article bodies drop paragraph breaks ("police said.Authorities")
    # and keep wire-service rules ("____").
    text = _GLUED_SENTENCES.sub(" ", _RULE.sub(" ", text))
    return _DATELINE.sub("", " ".join(text.split()), count=1)


def _complete_summary(text: str) -> str:
    """Drop a summary's cut-off tail ("...attempted to run an ...")."""

    if not _TRUNCATION.search(text):
        return text
    text = _TRUNCATION.sub("", text)
    sentences = _spoken_sentences(text)
    complete = [sentence for sentence in sentences if sentence[-1] in ".!?\"'”’)"]
    return " ".join(complete) if complete else text


# --- Article pages ---------------------------------------------------------


def fetch_public_html(url: str, *, timeout_seconds: float) -> str | None:
    """GET an HTML page from a public address, following a few redirects."""

    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.port not in {None, 80, 443}
        ):
            return None
        connection_class = (
            _PublicHTTPSConnection if parts.scheme == "https" else _PublicHTTPConnection
        )
        connection = connection_class(
            parts.hostname,
            parts.port,
            timeout=timeout_seconds,
        )
        try:
            path = parts.path or "/"
            if parts.query:
                path = f"{path}?{parts.query}"
            connection.request(
                "GET",
                path,
                headers={
                    "User-Agent": _BROWSER_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    return None
                url = urljoin(url, location)
                continue
            if response.status != 200:
                return None
            if "html" not in (response.getheader("Content-Type") or "").casefold():
                return None
            if (response.getheader("Content-Encoding") or "identity").casefold() != "identity":
                return None
            raw = response.read(MAX_ARTICLE_BYTES)
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                return raw.decode(charset, "replace")
            except LookupError:
                return raw.decode("utf-8", "replace")
        except (OSError, http.client.HTTPException):
            return None
        finally:
            connection.close()
    return None


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def _connect_public(host: str, port: int, timeout: float | None) -> socket.socket:
    """Connect only to a public address: never loopback, LAN, or link-local."""

    last_error: OSError | None = None
    for family, socktype, proto, _name, address in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        if not _is_public_address(str(address[0])):
            continue
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(address)
        except OSError as error:
            sock.close()
            last_error = error
            continue
        return sock
    raise ConnectionError(f"{host} has no reachable public address.") from last_error


class _PublicHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = _connect_public(self.host, self.port, self.timeout)


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        sock = _connect_public(self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def extract_article_paragraphs(
    page: str, *, limit: int = MAX_ARTICLE_PARAGRAPHS
) -> tuple[str, ...]:
    """The story's paragraphs: structured article body first, then <p> text."""

    body = _json_ld_article_body(page)
    raw_paragraphs = (
        re.split(r"\s*\n\s*", body) if body else _ParagraphCollector.collect(page)
    )
    paragraphs: list[str] = []
    seen: set[str] = set()
    for raw in raw_paragraphs:
        for chunk in _bounded_chunks(
            _without_boilerplate(_clean_text(raw)), MAX_PASSAGE_CHARACTERS
        ):
            if not _is_story_paragraph(chunk):
                continue
            key = chunk.casefold()[:80]
            if key in seen:
                continue
            seen.add(key)
            paragraphs.append(chunk)
        if len(paragraphs) >= limit:
            break
    return tuple(paragraphs[:limit])


def _without_boilerplate(paragraph: str) -> str:
    """Drop promo, caption, and byline sentences; "" when that is all it is."""

    sentences = _spoken_sentences(paragraph)
    kept = [
        sentence
        for sentence in sentences
        if "|" not in sentence and not _BOILERPLATE.search(sentence)
    ]
    # "Don't miss the news of the day. Get them on our Telegram channel." is
    # promo throughout; a long body only loses its promo sentences.
    if len(kept) < len(sentences) and len(sentences) <= 2:
        return ""
    return " ".join(kept)


def _json_ld_article_body(page: str) -> str:
    best = ""
    for block in _JSON_LD.findall(page)[:20]:
        text = block.strip()
        if text.startswith("<!--"):
            text = text[4:].removesuffix("-->")
        try:
            data = json.loads(text)
        except ValueError:
            continue
        for body in _article_bodies(data, depth=0):
            if len(body) > len(best):
                best = body
    return best if len(best) >= 200 else ""


def _article_bodies(value: object, *, depth: int) -> Iterator[str]:
    if depth > 6:
        return
    if isinstance(value, dict):
        body = value.get("articleBody")
        if isinstance(body, str):
            yield body
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _article_bodies(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value[:50]:
            yield from _article_bodies(child, depth=depth + 1)


def _is_story_paragraph(text: str) -> bool:
    if len(text) < 60 or len(text.split()) < 10:
        return False
    # "Statesman News Service | Washington | October 11, 2026" is a byline.
    if "|" in text or _BOILERPLATE.search(text):
        return False
    if not re.search(r"[.!?][\"'”’)]?$", text):
        return False
    letters = [character for character in text if character.isalpha()]
    return sum(character.isupper() for character in letters) * 2 <= len(letters)


def _bounded_chunks(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for sentence in _spoken_sentences(text):
        if current and len(current) + 1 + len(sentence) > limit:
            chunks.append(current)
            current = ""
        current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if len(chunk) <= limit * 2]


class _ParagraphCollector(HTMLParser):
    """Collect <p> text outside navigation, captions, scripts, and forms."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._article_depth = 0
        self._buffer: list[str] | None = None
        self._buffer_in_article = False
        self.paragraphs: list[tuple[str, bool]] = []

    @classmethod
    def collect(cls, page: str) -> list[str]:
        collector = cls()
        try:
            collector.feed(page)
            collector.close()
        except Exception:
            pass
        in_article = [text for text, inside in collector.paragraphs if inside]
        # Prefer the <article> body when the page marks one up.
        if len(in_article) >= 2:
            return in_article
        return [text for text, _inside in collector.paragraphs]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_ELEMENTS:
            self._skip_depth += 1
        elif tag == "article":
            self._article_depth += 1
        elif tag == "p":
            self._flush()
            if not self._skip_depth:
                self._buffer = []
                self._buffer_in_article = self._article_depth > 0
        elif tag == "br" and self._buffer is not None:
            self._buffer.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_ELEMENTS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "article":
            self._article_depth = max(0, self._article_depth - 1)
        elif tag == "p":
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._buffer is not None and not self._skip_depth:
            self._buffer.append(data)

    def close(self) -> None:
        super().close()
        self._flush()

    def _flush(self) -> None:
        if self._buffer is not None:
            self.paragraphs.append(("".join(self._buffer), self._buffer_in_article))
        self._buffer = None
