from syndcrawler.core.change import (
    ChangeKind,
    HttpValidators,
    assess_change,
    fingerprint_page,
    validators_from_headers,
)


def test_validators_are_case_insensitive_and_emit_conditional_headers() -> None:
    validators = validators_from_headers(
        {
            "ETag": '"abc"',
            "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT",
        }
    )
    assert validators == HttpValidators(
        etag='"abc"',
        last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
    )
    assert validators.request_headers() == {
        "if-none-match": '"abc"',
        "if-modified-since": "Wed, 21 Oct 2015 07:28:00 GMT",
    }


def test_semantic_fingerprint_ignores_link_order_and_duplicate_links() -> None:
    first = fingerprint_page(
        b"<html>A</html>",
        title="A",
        links=("https://example.com/b", "https://example.com/a"),
        structured={"json_ld": []},
    )
    second = fingerprint_page(
        b"<html>A changed markup</html>",
        title="A",
        links=(
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/a",
        ),
        structured={"json_ld": []},
    )

    assessment = assess_change(first, second)
    assert assessment.kind is ChangeKind.REPRESENTATION_CHANGED
    assert assessment.raw_changed is True
    assert assessment.semantic_changed is False


def test_semantic_change_is_distinguished_from_raw_only_change() -> None:
    first = fingerprint_page(
        b"one",
        title="Product A",
        links=(),
        structured={"open_graph": {}},
    )
    second = fingerprint_page(
        b"two",
        title="Product B",
        links=(),
        structured={"open_graph": {}},
    )

    assessment = assess_change(first, second)
    assert assessment.kind is ChangeKind.SEMANTIC_CHANGED
    assert assessment.changed is True


def test_304_short_circuits_without_current_fingerprint() -> None:
    previous = fingerprint_page(
        b"one",
        title="A",
        links=(),
        structured={},
    )
    assessment = assess_change(previous, None, status_code=304)
    assert assessment.kind is ChangeKind.NOT_MODIFIED
    assert assessment.changed is False
