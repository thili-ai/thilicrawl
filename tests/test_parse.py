from __future__ import annotations

from thilicrawl.parse import extract_links, is_html, origin

BASE = "http://site.test:8000/dir/page.html"


def test_links_are_resolved_against_the_base_url() -> None:
    html = '<a href="/abs">1</a><a href="rel">2</a><a href="../up">3</a><a href="http://other.test/x">4</a>'
    assert extract_links(html, BASE) == [
        "http://site.test:8000/abs",
        "http://site.test:8000/dir/rel",
        "http://site.test:8000/up",
        "http://other.test/x",
    ]


def test_fragments_are_stripped_and_duplicates_collapsed_keeping_order() -> None:
    html = '<a href="/a#one">1</a><a href="/b">2</a><a href="/a#two">3</a><a href="/a">4</a>'
    assert extract_links(html, BASE) == ["http://site.test:8000/a", "http://site.test:8000/b"]


def test_things_that_are_not_web_pages_are_dropped() -> None:
    html = (
        '<a href="mailto:a@b.c">m</a><a href="javascript:void(0)">j</a><a href="tel:+1555">t</a>'
        '<a href="#top">top</a><a href="ftp://files.test/x">f</a><a href="/ok">ok</a>'
    )
    assert extract_links(html, BASE) == ["http://site.test:8000/ok"]


def test_anchors_without_an_href_and_other_tags_are_ignored() -> None:
    html = (
        '<a name="x">no href</a><a href="">empty</a><link href="/css"><img src="/i.png"><a href="/yes">y</a>'
    )
    assert extract_links(html, BASE) == ["http://site.test:8000/yes"]


def test_html_entities_in_urls_are_decoded() -> None:
    assert extract_links('<a href="/q?a=1&amp;b=2">x</a>', BASE) == ["http://site.test:8000/q?a=1&b=2"]


def test_malformed_html_does_not_raise() -> None:
    assert extract_links('<a href="/ok">unclosed <b><a href=/also>', BASE) == [
        "http://site.test:8000/ok",
        "http://site.test:8000/also",
    ]


def test_empty_input() -> None:
    assert extract_links("", BASE) == []


def test_a_large_page_yields_each_distinct_link_once() -> None:
    html = "".join(f'<a href="/p/{k % 50}">x</a>' for k in range(10_000))
    assert len(extract_links(html, BASE)) == 50


def test_is_html() -> None:
    assert is_html("text/html")
    assert is_html("TEXT/HTML; charset=utf-8")
    assert is_html("application/xhtml+xml")
    assert not is_html("application/octet-stream")
    assert not is_html("text/plain")
    assert not is_html(None)


def test_origin_means_scheme_host_and_port() -> None:
    assert origin("http://a.test:8000/x/y?z=1") == "http://a.test:8000"
    assert origin("https://a.test/") == "https://a.test"
    assert origin("http://a.test:8000/") != origin("http://a.test:8001/")


def test_a_typo_in_one_link_does_not_lose_the_others() -> None:
    html = '<a href="http://[oops">bad</a><a href="/fine">ok</a>'
    assert extract_links(html, BASE) == ["http://site.test:8000/fine"]
