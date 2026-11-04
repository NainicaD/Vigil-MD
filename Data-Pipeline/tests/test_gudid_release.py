"""Unit tests for scripts/acquisition/gudid_release.py (docs/issue.md #6, #7). No network."""
import pytest

from scripts.acquisition.gudid_release import pick_latest_gudid

BASE = "https://accessgudid.nlm.nih.gov/release_files/download/"


def rss(*links: str) -> bytes:
    items = "".join(f"<item><link>{link}</link></item>" for link in links)
    return f"<rss><channel>{items}</channel></rss>".encode()


def test_single_release():
    url = BASE + "gudid_full_release_20261102.zip"
    assert pick_latest_gudid(rss(url)) == (url, "20261102")


@pytest.mark.parametrize("order", [1, -1])
def test_newest_wins_whatever_the_feed_order(order):
    links = [BASE + f"gudid_full_release_{d}.zip" for d in ("20260901", "20261102", "20261001")]
    url, snapshot = pick_latest_gudid(rss(*links[::order]))
    assert snapshot == "20261102" and url.endswith("_20261102.zip")


def test_ignores_other_links():
    url = BASE + "gudid_full_release_20261001.zip"
    feed = rss(BASE + "gudid_full_release_20261201.txt",   # not a zip
               "https://example.org/other_20261201.zip",    # not GUDID
               "  " + url + "  ")                           # whitespace is stripped
    assert pick_latest_gudid(feed) == (url, "20261001")


def test_date_comes_from_the_file_name_only():
    url = "https://accessgudid.nlm.nih.gov/20991231/gudid_full_release_20261102.zip"
    assert pick_latest_gudid(rss(url))[1] == "20261102"


def test_needs_exactly_eight_digits():
    with pytest.raises(RuntimeError, match="8-digit date"):
        pick_latest_gudid(rss(BASE + "gudid_full_release_202611021.zip"))


@pytest.mark.parametrize("feed", [
    rss(BASE + "gudid_full_release_latest.zip"),   # no date: no guessed snapshot
    rss(),                                         # empty feed
])
def test_raises_without_a_dated_release(feed):
    with pytest.raises(RuntimeError, match="No GUDID full release"):
        pick_latest_gudid(feed)
