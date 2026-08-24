import gzip

import pytest

from syndcrawler.discovery import (
    DiscoveryParseError,
    parse_feed,
    parse_robots_sitemaps,
    parse_sitemap,
)


def test_parse_sitemap_urlset_and_index() -> None:
    urlset = b"""<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://example.com/a#part</loc></url>
      <url><loc>https://example.com/b</loc></url>
      <url><loc>https://example.com/a#other</loc></url>
    </urlset>"""
    parsed = parse_sitemap(urlset, "https://example.com/sitemap.xml")
    assert parsed.kind == "sitemap-urlset"
    assert parsed.urls == (
        "https://example.com/a",
        "https://example.com/b",
    )
    assert parsed.nested_documents == ()

    index = b"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>/products.xml.gz</loc></sitemap>
      <sitemap><loc>https://cdn.example.com/pages.xml</loc></sitemap>
    </sitemapindex>"""
    parsed_index = parse_sitemap(index, "https://example.com/sitemap-index.xml")
    assert parsed_index.kind == "sitemap-index"
    assert parsed_index.nested_documents == (
        "https://example.com/products.xml.gz",
        "https://cdn.example.com/pages.xml",
    )


def test_parse_sitemap_supports_gzip_and_plain_text() -> None:
    compressed = gzip.compress(
        b"<urlset><url><loc>https://example.com/from-gzip</loc></url></urlset>"
    )
    assert parse_sitemap(compressed, "https://example.com/sitemap.xml.gz").urls == (
        "https://example.com/from-gzip",
    )

    text = "https://example.com/a\n/b#fragment\nhttps://example.com/a\n"
    assert parse_sitemap(text, "https://example.com/sitemap.txt").urls == (
        "https://example.com/a",
        "https://example.com/b",
    )


def test_parse_feeds_and_robots_sitemaps() -> None:
    rss = b"""<rss><channel>
      <item><link>https://example.com/post-1</link></item>
      <item><link>/post-2</link></item>
    </channel></rss>"""
    assert parse_feed(rss, "https://example.com/feed.xml").urls == (
        "https://example.com/post-1",
        "https://example.com/post-2",
    )

    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <link rel="self" href="/api/entry/1"/>
        <link rel="alternate" href="/article/1"/>
      </entry>
    </feed>"""
    assert parse_feed(atom, "https://example.com/feed.atom").urls == (
        "https://example.com/article/1",
    )

    robots = """User-agent: *
    Disallow: /private
    Sitemap: https://example.com/sitemap.xml
    sitemap: /news-sitemap.xml # current news
    """
    assert parse_robots_sitemaps(robots, "https://example.com/robots.txt") == (
        "https://example.com/sitemap.xml",
        "https://example.com/news-sitemap.xml",
    )


def test_discovery_xml_rejects_entities_and_size_bombs() -> None:
    entity_document = b"""<!DOCTYPE foo [<!ENTITY x "boom">]>
    <urlset><url><loc>&x;</loc></url></urlset>"""
    with pytest.raises(DiscoveryParseError, match="DTD/entity"):
        parse_sitemap(entity_document, "https://example.com/sitemap.xml")

    with pytest.raises(DiscoveryParseError, match="exceeds"):
        parse_sitemap(b"x" * 101, "https://example.com/sitemap.txt", max_bytes=100)

    compressed = gzip.compress(b"x" * 101)
    with pytest.raises(DiscoveryParseError, match="decompressed"):
        parse_sitemap(compressed, "https://example.com/sitemap.xml.gz", max_bytes=100)
