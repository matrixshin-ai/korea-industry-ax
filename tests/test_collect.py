import os
import urllib.error
from unittest.mock import patch

import collect


def test_should_drop_item_short_title_empty_summary():
    assert collect.should_drop_item("짧은제목", "") is True


def test_should_drop_item_keeps_when_summary_present():
    assert collect.should_drop_item("짧은제목", "요약이 있으면 유지") is False


def test_should_drop_item_keeps_long_title():
    assert collect.should_drop_item("충분히 긴 제목의 기사입니다", "") is False


def test_source_from_domain_known():
    assert collect.source_from_domain("https://www.hankyung.com/economy/article/1", {"hankyung.com": "한국경제"}) == "한국경제"


def test_source_from_domain_unknown_falls_back_to_domain():
    assert collect.source_from_domain("https://www.unknownpress.co.kr/a/1", {"hankyung.com": "한국경제"}) == "unknownpress.co.kr"


class _FakeFeed:
    def __init__(self, entries=None, bozo=False):
        self.entries = entries or []
        self.bozo = bozo
        self.feed = type("F", (), {"title": "Fake Feed"})()


def test_fetch_rss_candidates_empty_response_does_not_crash():
    feeds = [{"url": "https://example.com/rss.xml", "source": "테스트매체"}]
    with patch("collect.feedparser.parse", return_value=_FakeFeed(entries=[])):
        candidates, log = collect.fetch_rss_candidates(feeds, hours=48)
    assert candidates == []
    assert log[0]["status"] == "ok"


def test_fetch_rss_candidates_timeout_is_logged_and_does_not_stop_run():
    feeds = [
        {"url": "https://timeout.example.com/rss.xml", "source": "타임아웃매체"},
        {"url": "https://ok.example.com/rss.xml", "source": "정상매체"},
    ]

    def fake_parse(url, agent=None):
        if "timeout" in url:
            raise TimeoutError("simulated timeout")
        return _FakeFeed(entries=[])

    with patch("collect.feedparser.parse", side_effect=fake_parse):
        candidates, log = collect.fetch_rss_candidates(feeds, hours=48)

    statuses = {entry["source"]: entry["status"] for entry in log}
    assert statuses["타임아웃매체"] == "fail"
    assert statuses["정상매체"] == "ok"


def test_fetch_naver_candidates_without_credentials_returns_empty_and_continues():
    with patch.dict(os.environ, {"NAVER_CLIENT_ID": "", "NAVER_CLIENT_SECRET": ""}, clear=False):
        candidates, log, attempted = collect.fetch_naver_candidates(["테스트쿼리"], hours=48, domain_map={})
    assert candidates == []
    assert log == []
    assert attempted is False


def test_fetch_naver_candidates_auth_failure_continues_to_next_query():
    def fake_search_news_page(query, start, client_id, client_secret):
        if query == "실패쿼리":
            raise urllib.error.HTTPError(url="", code=401, msg="Unauthorized", hdrs=None, fp=None)
        return {"items": []}

    with patch.dict(os.environ, {"NAVER_CLIENT_ID": "id", "NAVER_CLIENT_SECRET": "secret"}):
        with patch("collect.search_news_page", side_effect=fake_search_news_page):
            candidates, log, attempted = collect.fetch_naver_candidates(
                ["실패쿼리", "정상쿼리"], hours=48, domain_map={}
            )

    assert attempted is True
    by_query = {entry["query"]: entry for entry in log}
    assert "error" in by_query["실패쿼리"]
    assert "error" not in by_query["정상쿼리"]
