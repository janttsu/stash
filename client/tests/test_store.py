import pytest
from conftest import ids_by_title

from stashai.store import short_url


def titles(store, ids):
    return sorted(store.bookmarks[i].title for i in ids)


def test_text_search_any_and_all(store):
    assert titles(store, store.search(text=["acme"])) == [
        "ACME blog", "ACME documentation", "Acme buys Widgets Inc", "Knowledge base: DNS"]  # its url has acme
    assert titles(store, store.search(text=["acme", "blog"], match="all")) == ["ACME blog"]
    # the tab and category names are searchable too
    assert titles(store, store.search(text=["asiakkaat"])) == ["ACME blog"]


def test_short_words_match_whole_words_only(store):
    # "x" must not match every url with an x in it, "ml" not "html"
    assert titles(store, store.search(text=["x"])) == ["X"]
    assert titles(store, store.search(text=["ml"])) == ["Maximum likelihood"]


def test_host_matches_www_and_subdomains(store):
    assert titles(store, store.search(host=["acme.example"])) == [
        "ACME blog", "ACME documentation", "Knowledge base: DNS"]


def test_tags_and_places(store):
    assert titles(store, store.search(tags_any=["dns", "ruoka"])) == ["Knowledge base: DNS", "Pulla recipe"]
    assert titles(store, store.search(tags_all=["acme", "docs"])) == ["ACME documentation"]
    assert titles(store, store.search(text=["acme"], tags_none=["acme"])) == ["Acme buys Widgets Inc", "Knowledge base: DNS"]
    assert titles(store, store.search(untagged=True)) == ["X"]
    assert titles(store, store.search(where="dashboard")) == ["ACME blog"]
    assert titles(store, store.search(tab="työ", category="asiakkaat")) == ["ACME blog"]
    assert store.search(added_after="2000-01-01") and not store.search(added_before="2000-01-01")


def test_sets(store):
    s1 = store.new_set(store.search(host=["acme.example"]), "acme site")
    s2 = store.new_set(store.search(in_set=s1.name, tags_any=["acme"]), "tagged")
    assert (s1.name, s2.name) == ("S1", "S2")
    assert titles(store, store.search(in_set="s1", not_in_set="S2")) == ["Knowledge base: DNS"]
    blog = ids_by_title(store)["ACME blog"]
    assert blog not in store.search(in_set="S1", exclude_ids=[blog])
    assert len(store.resolve("ALL")) == 7
    with pytest.raises(KeyError):
        store.resolve("S9")
    with pytest.raises(ValueError):
        store.search(colour="red")
    assert "S1: 3 bookmarks – acme site" in store.sets_text()


def test_lines_and_overview(store):
    blog = ids_by_title(store)["ACME blog"]
    assert store.line(blog) == f"#{blog} ACME blog | acme.example/blog | tags: acme | Työ / Asiakkaat"
    text = store.describe(store.search(text=["acme"]))
    assert text.startswith("4 bookmarks\ntags: acme 2")
    ov = store.overview()
    assert "7 bookmarks (6 in the Catalog" in ov and "acme 2" in ov and "- Työ: Asiakkaat 1" in ov
    assert short_url("https://www.example.com/") == "example.com"
