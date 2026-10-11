"""Deterministic, offline checks for "tell me more" story details."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

from local_voice_api.retrieval import RetrievalResult  # noqa: E402
from local_voice_api.story_details import (  # noqa: E402
    BingNewsStoryDetails,
    _clean_text,
    _is_public_address,
    _story_queries,
    extract_article_paragraphs,
    fetch_public_html,
    parse_bing_news_feed,
    story_details_from_environment,
)

_HEADLINE = "US military says it disabled cargo vessel trying to run Iran blockade"

# Shaped like Bing's feed on 2026-10-11, including its per-query namespace,
# click-tracking links, zero-width characters, and cut-off summaries.
_BING_FEED = (
    '<?xml version="1.0" encoding="utf-8" ?><rss version="2.0" '
    'xmlns:News="https://www.bing.com/news/search?q=blockade&amp;format=rss">'
    "<channel><title>blockade - BingNews</title>"
    "<item><title>US military disables cargo ship attempting to breach Iran "
    "blockade</title><link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;"
    "aid=&amp;url=https%3a%2f%2fwww.business-standard.com%2fworld-news%2fus-"
    "military-disables-cargo-ship&amp;c=1&amp;mkt=en-in</link><description>The US "
    "military said on Saturday it had disabled a commercial cargo vessel in the "
    "Gulf of Oman after the ship's crew ignored warnings and attempted ‌to "
    "run a naval blockade ​against Iran.</description>"
    "<pubDate>Sat, 10 Oct 2026 20:24:00 GMT</pubDate>"
    "<News:Source>Business Standard</News:Source></item>"
    "<item><title>US fighter jet strikes cargo ship in Gulf of Oman as Iran "
    "blockade tightens</title><link>http://www.bing.com/news/apiclick.aspx?"
    "url=https%3a%2f%2ftimesofindia.indiatimes.com%2fworld%2fus-fighter-jet"
    "&amp;c=2</link><description>The vessel was Panama-flagged. It allegedly "
    "ignored warnings and attempted to breach Washington's naval blockade against "
    "Iran, ...</description><News:Source>Times of India</News:Source></item>"
    "<item><title>Curious cows and autumn fungi in reader photos</title>"
    "<link>http://www.bing.com/news/apiclick.aspx?url=https%3a%2f%2fwww.bbc.com"
    "%2fnews%2fphotos&amp;c=3</link><description>Readers sent pictures of cows "
    "and sunsets.</description></item>"
    "</channel></rss>"
).encode()

_ARTICLE_PAGE = """<!doctype html><html><head><title>Story</title>
<script>var tracking = "<p>not a paragraph</p>";</script></head>
<body><header><p>Sign up for our newsletter to get the top stories every morning.</p></header>
<nav><p>World News | Business | Sport | Opinion | Video | Podcasts | More sections</p></nav>
<article>
<p>Statesman News Service | Washington | October 11, 2026 9:05 am</p>
<p>The US military on Saturday said it disabled a commercial cargo vessel in the Gulf of Oman after its crew ignored repeated warnings.</p>
<figure><figcaption><p>A US Navy destroyer patrols the Gulf of Oman in this file photo from last year.</p></figcaption></figure>
<p>The US military on Saturday said it disabled a commercial cargo vessel in the Gulf of Oman after its crew ignored repeated warnings.</p>
<p>&ldquo;A US military fighter jet struck the stern of the Panama-flagged M/V Ocean Molica with a precision munition,&rdquo; CENTCOM said.</p>
<p>Image: Bloomberg photo of tankers waiting near the strait before the war began.</p>
<p>No crew members were hurt, and the vessel is drifting under escort towards the port of Sohar.</p>
<p>Don't miss the most important news and views of the day. Get them on our Telegram channel.</p>
</article>
<footer><p>Copyright 2026 The Statesman. All rights reserved by the publisher here.</p></footer>
</body></html>"""


class BingNewsFeedTests(unittest.TestCase):
    def test_feed_reports_have_publisher_links_and_clean_summaries(self) -> None:
        reports = parse_bing_news_feed(_BING_FEED)

        self.assertEqual(3, len(reports))
        first, second, _third = reports
        self.assertEqual(
            "https://www.business-standard.com/world-news/us-military-disables-cargo-ship",
            first.url,
        )
        self.assertIn("attempted to run a naval blockade against Iran.", first.snippet)
        self.assertIsNotNone(first.published_at)
        # The cut-off tail ("...against Iran, ...") is dropped, not spoken.
        self.assertEqual("The vessel was Panama-flagged.", second.snippet)
        self.assertEqual("bing news", second.engine)


class DatelineTests(unittest.TestCase):
    def test_wire_datelines_are_not_read_aloud(self) -> None:
        # Live replay 2026-10-11 read out "NEW DELHI, Oct 11 (Reuters) - ".
        for raw, spoken in (
            (
                "NEW DELHI, Oct 11 (Reuters) - New Delhi braced for a second day "
                "of youth protests.",
                "New Delhi braced for a second day of youth protests.",
            ),
            (
                "Washington/Dubai, Oct 11 (PTI) Leaders across the world condemned "
                "the attack.",
                "Leaders across the world condemned the attack.",
            ),
            (
                "TAMPA, Fla. — U.S. Central Command forces disabled a cargo vessel.",
                "U.S. Central Command forces disabled a cargo vessel.",
            ),
            (
                "Apple (AAPL) - shares rose after the launch event on Friday.",
                "Apple (AAPL) - shares rose after the launch event on Friday.",
            ),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(spoken, _clean_text(raw))


class ArticleExtractionTests(unittest.TestCase):
    def test_only_the_story_paragraphs_are_kept_in_order(self) -> None:
        paragraphs = extract_article_paragraphs(_ARTICLE_PAGE)

        self.assertEqual(
            (
                "The US military on Saturday said it disabled a commercial cargo "
                "vessel in the Gulf of Oman after its crew ignored repeated warnings.",
                "“A US military fighter jet struck the stern of the Panama-flagged "
                "M/V Ocean Molica with a precision munition,” CENTCOM said.",
                "No crew members were hurt, and the vessel is drifting under escort "
                "towards the port of Sohar.",
            ),
            paragraphs,
        )

    def test_structured_article_body_is_preferred(self) -> None:
        body = (
            "The US military said it disabled a cargo vessel in the Gulf of Oman "
            "on Saturday after warnings were ignored.\\n"
            "A fighter jet struck the stern of the ship with a precision munition, "
            "officials said in a statement late on Saturday night."
        )
        page = (
            '<html><head><script type="application/ld+json">'
            '{"@context": "https://schema.org", "@graph": [{"@type": "NewsArticle", '
            f'"headline": "Blockade", "articleBody": "{body}"}}]}}'
            "</script></head><body><p>Short teaser text that is not the body of "
            "this particular article at all.</p></body></html>"
        )

        paragraphs = extract_article_paragraphs(page)

        self.assertEqual(2, len(paragraphs))
        self.assertTrue(paragraphs[1].startswith("A fighter jet struck the stern"))

    def test_glued_wire_copy_is_split_into_clean_sentences(self) -> None:
        # Live fetch 2026-10-11: one body glued its paragraphs together and
        # kept the wire service's "____" footer rule.
        body = (
            "A shooting in a residential area of Erie, Pennsylvania, left nine "
            "dead including the suspected shooter and rocked the city, police "
            "said.Authorities have not released a motive for the shooting so far. "
            "\\u201cI\\u2019m not sure the city has ever seen the loss of life we just "
            "witnessed.\\u201dGov. Josh Shapiro said he offered his administration\\u2019s "
            "full support to the mayor of the city.____This story has been "
            "corrected to show the shooting happened on Saturday evening."
        )
        page = (
            '<script type="application/ld+json">'
            f'{{"@type": "NewsArticle", "articleBody": "{body}"}}</script>'
        )

        paragraphs = extract_article_paragraphs(page)
        text = " ".join(paragraphs)

        self.assertIn("police said. Authorities have not released", text)
        self.assertIn("witnessed.” Gov. Josh Shapiro said", text)
        self.assertNotIn("____", text)
        self.assertNotIn("This story has been corrected", text)


class PublicFetchTests(unittest.TestCase):
    def test_only_public_addresses_are_allowed(self) -> None:
        for address in (
            "127.0.0.1",
            "10.0.0.5",
            "192.168.1.20",
            "172.16.0.1",
            "169.254.169.254",
            "100.64.0.1",
            "::1",
            "fe80::1",
            "::ffff:127.0.0.1",
            "0.0.0.0",
            "not-an-address",
        ):
            with self.subTest(address=address):
                self.assertFalse(_is_public_address(address))
        self.assertTrue(_is_public_address("93.184.216.34"))

    def test_unsafe_urls_are_never_fetched(self) -> None:
        for url in (
            "http://127.0.0.1:8767/v1/chat/completions",
            "http://localhost/",
            "http://user:secret@example.com/",
            "https://example.com:8443/",
            "file:///etc/passwd",
            "ftp://example.com/story",
        ):
            with self.subTest(url=url):
                self.assertIsNone(fetch_public_html(url, timeout_seconds=0.5))


class BingNewsStoryDetailsTests(unittest.TestCase):
    def test_article_paragraphs_come_first_then_other_outlets(self) -> None:
        source = BingNewsStoryDetails(timeout_seconds=0.5)
        reports = parse_bing_news_feed(_BING_FEED)

        with (
            patch.object(BingNewsStoryDetails, "_search", return_value=reports),
            patch(
                "local_voice_api.story_details.fetch_public_html",
                return_value=_ARTICLE_PAGE,
            ) as fetch,
        ):
            passages = asyncio.run(source.details(_HEADLINE, limit=16))

        texts = [passage.snippet for passage in passages]
        self.assertTrue(texts[0].startswith("The US military on Saturday said"))
        self.assertIn("Ocean Molica", texts[1])
        self.assertEqual("news article", passages[0].engine)
        # Other outlets' summaries follow; the unrelated photo story is dropped.
        self.assertEqual(
            ["bing news", "bing news"],
            [passage.engine for passage in passages if passage.engine == "bing news"],
        )
        self.assertFalse(any("cows" in text for text in texts))
        self.assertTrue(fetch.called)

    def test_unreadable_articles_still_leave_the_summaries(self) -> None:
        source = BingNewsStoryDetails(timeout_seconds=0.5)
        reports = parse_bing_news_feed(_BING_FEED)

        with (
            patch.object(BingNewsStoryDetails, "_search", return_value=reports),
            patch("local_voice_api.story_details.fetch_public_html", return_value=None),
        ):
            passages = asyncio.run(source.details(_HEADLINE, limit=16))

        self.assertEqual(["bing news", "bing news"], [p.engine for p in passages])

    def test_an_unrelated_page_is_not_used_as_the_article(self) -> None:
        source = BingNewsStoryDetails(timeout_seconds=0.5)
        unrelated = (
            "<article><p>Our privacy choices let you decide how partners use data "
            "across this website and its apps.</p><p>Choose which purposes you "
            "accept, then press the button below to continue to the site.</p></article>"
        )
        report = RetrievalResult(_HEADLINE, "https://www.reuters.com/world/x", "")

        with (
            patch.object(BingNewsStoryDetails, "_search", return_value=(report,)),
            patch(
                "local_voice_api.story_details.fetch_public_html",
                return_value=unrelated,
            ),
        ):
            passages = asyncio.run(source.details(_HEADLINE, limit=16))

        self.assertEqual((), passages)


class StorySearchTests(unittest.TestCase):
    def test_a_story_is_searched_by_headline_and_by_its_content_words(self) -> None:
        # Live search 2026-10-11: the headline found 2 reports, its content
        # words found 10.
        self.assertEqual(
            [
                "India’s ‘Cockroach’ Leaders Released as New Delhi Braces for More Protests",
                "India Cockroach Leaders Released New Delhi Braces Protests",
                "India Cockroach Leaders Released New Delhi",
            ],
            _story_queries(
                "India’s ‘Cockroach’ Leaders Released as New Delhi Braces for More Protests"
            ),
        )

    def test_search_results_are_merged_without_duplicates(self) -> None:
        reuters = RetrievalResult(_HEADLINE, "https://www.reuters.com/world/a", "x")
        aljazeera = RetrievalResult(_HEADLINE, "https://www.aljazeera.com/news/b", "y")
        results = {
            "first": (reuters,),
            "second": (aljazeera, RetrievalResult(_HEADLINE, "https://www.reuters.com/world/a/", "z")),
        }
        source = BingNewsStoryDetails(timeout_seconds=0.5)

        with patch.object(
            BingNewsStoryDetails, "_search", side_effect=lambda query: results[query]
        ):
            merged = source._search_all(["first", "second"])

        self.assertEqual([reuters, aljazeera], merged)

    def test_search_failure_is_reported_when_every_query_fails(self) -> None:
        source = BingNewsStoryDetails(timeout_seconds=0.5)

        with patch.object(BingNewsStoryDetails, "_search", side_effect=OSError("down")):
            with self.assertRaises(OSError):
                source._search_all(["first", "second"])


class StoryDetailsConfigurationTests(unittest.TestCase):
    def test_story_details_are_opt_in(self) -> None:
        self.assertIsNone(story_details_from_environment({}))
        self.assertIsNone(
            story_details_from_environment({"AIRA_STORY_DETAILS_PROVIDER": "disabled"})
        )
        self.assertIsInstance(
            story_details_from_environment({"AIRA_STORY_DETAILS_PROVIDER": "bing_news"}),
            BingNewsStoryDetails,
        )
        with self.assertRaises(ValueError):
            story_details_from_environment({"AIRA_STORY_DETAILS_PROVIDER": "paid_api"})


if __name__ == "__main__":
    unittest.main()
