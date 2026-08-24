from syndcrawler.runtime.html import parse_html


def test_parse_html_extracts_navigation_and_structured_evidence() -> None:
    html = """<!doctype html>
    <html>
      <head>
        <title>Product</title>
        <link rel="canonical" href="/products/widget?b=2&a=1#section">
        <meta property="og:title" content="Widget">
        <meta property="og:image" content="https://cdn.example.com/one.jpg">
        <meta property="og:image" content="https://cdn.example.com/two.jpg">
        <script type="application/ld+json">
          {"@context":"https://schema.org","@type":"Product","name":"Widget"}
        </script>
      </head>
      <body>
        <a href="/next#fragment">Next</a>
        <a href="mailto:test@example.com">Mail</a>
      </body>
    </html>"""

    parsed = parse_html(html, "https://shop.example.com/products/widget")

    assert parsed.title == "Product"
    assert parsed.links == ("https://shop.example.com/next",)
    assert parsed.structured.canonical_url == (
        "https://shop.example.com/products/widget?b=2&a=1"
    )
    assert parsed.structured.json_ld == (
        {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Widget",
        },
    )
    assert parsed.structured.open_graph == {
        "og:title": ("Widget",),
        "og:image": (
            "https://cdn.example.com/one.jpg",
            "https://cdn.example.com/two.jpg",
        ),
    }
    assert parsed.structured.malformed_json_ld_count == 0


def test_parse_html_tolerates_malformed_json_ld() -> None:
    html = """<html><head>
      <script type="application/ld+json">{"broken":</script>
      <script type="application/ld+json; charset=utf-8">{"ok": true}</script>
    </head></html>"""

    parsed = parse_html(html, "https://example.com/")

    assert parsed.structured.json_ld == ({"ok": True},)
    assert parsed.structured.malformed_json_ld_count == 1
