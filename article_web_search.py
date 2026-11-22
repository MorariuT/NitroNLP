"""Collect dated news search results from Python, independently of Codex."""

from email.utils import parsedate_to_datetime
from html import unescape
import re
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree


NEWS_FEEDS = (
    (
        "Bing News", "https://www.bing.com/news/search",
        {"format": "rss", "setlang": "ro-RO", "mkt": "ro-RO"},
    ),
    (
        "Google News", "https://news.google.com/rss/search",
        {"hl": "ro", "gl": "RO", "ceid": "RO:ro"},
    ),
)
MAX_SOURCES = 5
COMMON_WORDS = set(
    "acest această aceste astfel care cele către dintre deja despre după fost mai mult "
    "pentru peste prin spune sunt unei unde unui unor când fără încă foarte înainte "
    "with that from this have were will been their there about after before".split()
)


def plain_text(html):
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", html or "")).split())


def news_queries(statement, president):
    terms = {}
    for word in re.findall(r"[^\W_]+", statement):
        if (len(word) > 3 and word.casefold() not in COMMON_WORDS) or word.isdecimal():
            terms.setdefault(word.casefold(), word)
    words = list(terms.values())
    detailed = " ".join(words[:9]) or statement[:160]
    short = " ".join(sorted(words, key=len, reverse=True)[:4]) or detailed
    return list(dict.fromkeys(f"{president} {query}".strip() for query in (detailed, short)))


def search_feed(feed, query, article_date):
    name, endpoint, defaults = feed
    if name == "Google News":
        query += f" after:{article_date.isoformat()}"
    url = f"{endpoint}?{urlencode({**defaults, 'q': query})}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/rss+xml"})
    with urlopen(request, timeout=12) as response:
        body = response.read(512 * 1024 + 1)
    if len(body) > 512 * 1024:
        raise ValueError("search response exceeds the size limit")
    channel = ElementTree.fromstring(body).find("channel")
    if channel is None:
        raise ValueError("search endpoint did not return an RSS feed")

    for item in channel.findall("item"):
        try:
            published = parsedate_to_datetime(item.findtext("pubDate")).date()
            link = (item.findtext("link") or "").strip()
            parsed_url = urlsplit(link)
            if parsed_url.hostname == "www.bing.com" and parsed_url.path == "/news/apiclick.aspx":
                destination = parse_qs(parsed_url.query).get("url", [""])[0]
                if urlsplit(destination).path.strip("/"):
                    link = destination
                    parsed_url = urlsplit(link)
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            published <= article_date
            or parsed_url.scheme not in ("https", "http")
            or not parsed_url.hostname
        ):
            continue
        title = plain_text(item.findtext("title"))
        outlet = plain_text(item.findtext("source") or item.findtext("{*}Source"))
        headline, separator, suffix = title.rpartition(" - ")
        if separator and (not outlet or suffix.casefold() == outlet.casefold()):
            title, outlet = headline, outlet or suffix
        if not title or not outlet:
            continue
        yield {
            "outlet": outlet,
            "url": link,
            "publication_date": published.isoformat(),
            "headline": title,
            # Google News descriptions often list other syndicated articles.
            "excerpt": plain_text(item.findtext("description"))[:650] if name == "Bing News" else "",
        }


def collect_news(facts, article_date, president):
    evidence = []
    for fact in facts:
        sources = {}
        errors = []
        queries = []
        search_succeeded = False
        for query in news_queries(fact["statement"], president):
            queries.append(query)
            for feed in NEWS_FEEDS:
                try:
                    for result in search_feed(feed, query, article_date):
                        identity = (result["outlet"].casefold(), result["headline"].casefold())
                        if identity not in sources:
                            sources[identity] = {"source_id": len(sources) + 1, **result}
                        if len(sources) == MAX_SOURCES:
                            break
                    search_succeeded = True
                except (OSError, ValueError, ElementTree.ParseError) as exc:
                    errors.append(f"{feed[0]}: {exc}")
                if len(sources) == MAX_SOURCES:
                    break
            if sources:
                break
        evidence.append({
            "fact_id": fact["fact_id"],
            "queries": queries,
            "sources": list(sources.values()),
            "search_succeeded": search_succeeded,
            "search_errors": errors,
        })
    return evidence
