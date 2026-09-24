import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import build


def _iso(dt):
    return dt.isoformat()


def test_build_main_drops_c_x_unpublished_dedupes_and_sorts(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    recent = _iso(now - timedelta(hours=1))
    old = _iso(now - timedelta(hours=100))  # outside even the 48h Monday window

    classified = [
        {"title": "X등급 기사", "link": "https://x.example.com/1", "source": "매체", "summary": "",
         "published": recent, "grade": "X", "section": None, "region": None, "loc": None,
         "ulsan_score": 0, "industry": None, "tech": [], "core": 0},
        {"title": "C등급 기사", "link": "https://x.example.com/1b", "source": "매체", "summary": "",
         "published": recent, "grade": "C", "section": 1, "region": "전국", "loc": None,
         "ulsan_score": 0, "industry": "기타", "tech": [], "core": 0},
        {"title": "미분류 기사", "link": "https://x.example.com/2", "source": "매체", "summary": "",
         "published": recent, "grade": None, "unclassified": True},
        {"title": "기간 밖 S등급 기사", "link": "https://x.example.com/3", "source": "매체", "summary": "",
         "published": old, "grade": "S", "section": 1, "region": "울산", "loc": None,
         "ulsan_score": 10, "industry": "조선", "tech": ["로봇"], "core": 1},
        {"title": "B등급 기사", "link": "https://x.example.com/4", "source": "매체A", "summary": "",
         "published": recent, "grade": "B", "section": 1, "region": "전국", "loc": None,
         "ulsan_score": 0, "industry": "금융", "tech": [], "core": 0},
        {"title": "S등급 기사", "link": "https://x.example.com/5", "source": "매체B", "summary": "",
         "published": recent, "grade": "S", "section": 1, "region": "울산", "loc": None,
         "ulsan_score": 10, "industry": "조선", "tech": ["로봇"], "core": 1},
        {"title": "A등급 정책 기사", "link": "https://x.example.com/6", "source": "매체C", "summary": "",
         "published": recent, "grade": "A", "section": 3, "region": "전국", "loc": None,
         "ulsan_score": 0, "industry": "공공", "tech": [], "core": 0},
    ]

    classified_path = tmp_path / "classified.json"
    data_path = tmp_path / "public" / "data.json"
    logs_dir = tmp_path / "logs"
    classified_path.write_text(json.dumps(classified), encoding="utf-8")

    monkeypatch.setattr(build, "CLASSIFIED_PATH", str(classified_path))
    monkeypatch.setattr(build, "DATA_PATH", str(data_path))
    monkeypatch.setattr(build, "LOGS_DIR", str(logs_dir))
    monkeypatch.setattr(build, "get_collection_hours", lambda: 24)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    build.main()

    result = json.loads(data_path.read_text(encoding="utf-8"))

    section1_titles = [it["title"] for it in result["sections"]["1"]["items"]]
    assert "X등급 기사" not in section1_titles
    assert "C등급 기사" not in section1_titles
    assert "미분류 기사" not in section1_titles
    assert "기간 밖 S등급 기사" not in section1_titles
    assert section1_titles == ["S등급 기사", "B등급 기사"]

    top = result["sections"]["1"]["items"][0]
    assert top["score"] == 100 + 10 + 1 * 10  # S(100) + ulsan(10) + core(1)*10 == 110
    assert top["grade"] == "S"

    section3_titles = [it["title"] for it in result["sections"]["3"]["items"]]
    assert section3_titles == ["A등급 정책 기사"]

    assert result["collection_hours"] == 24
    assert result["stats"]["dropped_unclassified"] == 1  # the grade=None item
    assert result["stats"]["dropped_not_published_after_regrade"] == 2  # X, C (neither merged into anything)
    assert result["stats"]["dropped_out_of_window"] == 1
    assert result["stats"]["published_grade_counts"] == {"S": 1, "A": 1, "B": 1}


def test_regrade_representatives_unifies_group_to_highest_graded_member():
    # An X-graded duplicate nested under a B-graded algorithmic representative -
    # the group must publish as the S-graded member, with both others as `related`.
    x_item = {"title": "X", "link": "https://x.example.com/1", "source": "매체X", "grade": "X"}
    s_item = {"title": "S", "link": "https://s.example.com/1", "source": "매체S", "grade": "S", "section": 1}
    b_rep = {"title": "B (algorithmic representative)", "link": "https://b.example.com/1", "source": "매체B",
              "grade": "B", "section": 1, "related": [x_item, s_item]}

    result = build.regrade_representatives([b_rep])

    assert len(result) == 1
    assert result[0]["grade"] == "S"
    assert result[0]["link"] == "https://s.example.com/1"
    related_links = {r["link"] for r in result[0]["related"]}
    assert related_links == {"https://x.example.com/1", "https://b.example.com/1"}


def test_build_main_unifies_algorithmically_merged_group_to_best_grade(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    recent = _iso(now - timedelta(hours=1))
    # Same event (org+number match for dedup.py's gate condition), one graded S
    # and one graded X - the X one should not sink the group; it becomes a
    # `related` citation under the S-graded article.
    classified = [
        {"title": "현대차 울산공장, AI 자율제조 라인에 3000억원 투자", "link": "https://a.example.com/1",
         "source": "한국경제", "summary": "현대차가 울산 공장에 AI 자율제조 라인 구축을 위해 3000억원을 투자한다.",
         "published": recent, "grade": "S", "section": 1, "region": "울산", "loc": None,
         "ulsan_score": 10, "industry": "자동차", "tech": ["자율제조"], "core": 1},
        {"title": "현대차, 울산 AI 자율제조 라인에 3000억 투입", "link": "https://b.example.com/2",
         "source": "연합뉴스", "summary": "현대차가 울산 공장 AI 자율제조 라인에 3000억원을 투입하기로 했다.",
         "published": recent, "grade": "X", "section": None, "region": None, "loc": None,
         "ulsan_score": 0, "industry": None, "tech": [], "core": 0},
    ]

    classified_path = tmp_path / "classified.json"
    data_path = tmp_path / "public" / "data.json"
    logs_dir = tmp_path / "logs"
    classified_path.write_text(json.dumps(classified), encoding="utf-8")

    monkeypatch.setattr(build, "CLASSIFIED_PATH", str(classified_path))
    monkeypatch.setattr(build, "DATA_PATH", str(data_path))
    monkeypatch.setattr(build, "LOGS_DIR", str(logs_dir))
    monkeypatch.setattr(build, "get_collection_hours", lambda: 24)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    build.main()

    result = json.loads(data_path.read_text(encoding="utf-8"))
    assert result["article_count"] == 1
    item = result["sections"]["1"]["items"][0]
    assert item["grade"] == "S"
    assert len(item["related"]) == 1
    assert item["related"][0]["link"] == "https://b.example.com/2"


def test_build_main_skips_item_with_invalid_section_instead_of_crashing(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    recent = _iso(now - timedelta(hours=1))
    classified = [
        # A malformed Haiku response could in principle slip an S/A/B grade
        # through with no usable section - build.py must skip it, not crash.
        {"title": "섹션 없는 기사", "link": "https://x.example.com/1", "source": "매체", "summary": "",
         "published": recent, "grade": "S", "section": None, "region": "울산", "loc": None,
         "ulsan_score": 10, "industry": "조선", "tech": [], "core": 0},
        {"title": "정상 기사", "link": "https://x.example.com/2", "source": "매체", "summary": "",
         "published": recent, "grade": "B", "section": 1, "region": "전국", "loc": None,
         "ulsan_score": 0, "industry": "금융", "tech": [], "core": 0},
    ]

    classified_path = tmp_path / "classified.json"
    data_path = tmp_path / "public" / "data.json"
    logs_dir = tmp_path / "logs"
    classified_path.write_text(json.dumps(classified), encoding="utf-8")

    monkeypatch.setattr(build, "CLASSIFIED_PATH", str(classified_path))
    monkeypatch.setattr(build, "DATA_PATH", str(data_path))
    monkeypatch.setattr(build, "LOGS_DIR", str(logs_dir))
    monkeypatch.setattr(build, "get_collection_hours", lambda: 24)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    build.main()  # must not raise

    result = json.loads(data_path.read_text(encoding="utf-8"))
    assert result["article_count"] == 1
    assert result["stats"]["dropped_invalid_section"] == 1


def test_compute_yield_log_aggregates_per_feed_and_per_query():
    classified = [
        {"title": "a", "grade": "S", "source": "매체A", "origin_feed": "https://a.example.com/rss"},
        {"title": "b", "grade": "B", "source": "매체A", "origin_feed": "https://a.example.com/rss"},
        {"title": "c", "grade": "X", "source": "매체A", "origin_feed": "https://a.example.com/rss"},
        {"title": "d", "grade": "A", "source": "매체B", "origin_query": "\"AI 전환\""},
        {"title": "e", "grade": "C", "source": "매체B", "origin_query": "\"AI 전환\""},
    ]

    result = build.compute_yield_log(classified)

    feed_stats = result["rss"]["https://a.example.com/rss"]
    assert feed_stats["candidates"] == 3
    assert feed_stats["s"] == 1
    assert feed_stats["a"] == 0
    assert feed_stats["source"] == "매체A"

    query_stats = result["naver"]['"AI 전환"']
    assert query_stats["candidates"] == 2
    assert query_stats["s"] == 0
    assert query_stats["a"] == 1


def test_compute_yield_log_ignores_items_without_origin():
    result = build.compute_yield_log([{"title": "x", "grade": "S"}])
    assert result == {"rss": {}, "naver": {}}


def test_build_main_writes_yield_log(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    recent = _iso(now - timedelta(hours=1))
    classified = [
        {"title": "S등급 기사", "link": "https://x.example.com/1", "source": "매체", "summary": "",
         "published": recent, "grade": "S", "section": 1, "region": "전국", "loc": None,
         "ulsan_score": 0, "industry": "기타", "tech": [], "core": 0, "origin_feed": "https://feed.example.com/rss"},
    ]
    classified_path = tmp_path / "classified.json"
    data_path = tmp_path / "public" / "data.json"
    logs_dir = tmp_path / "logs"
    classified_path.write_text(json.dumps(classified), encoding="utf-8")

    monkeypatch.setattr(build, "CLASSIFIED_PATH", str(classified_path))
    monkeypatch.setattr(build, "DATA_PATH", str(data_path))
    monkeypatch.setattr(build, "LOGS_DIR", str(logs_dir))
    monkeypatch.setattr(build, "get_collection_hours", lambda: 24)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    build.main()

    yield_files = list(logs_dir.glob("yield_*.json"))
    assert len(yield_files) == 1
    yield_data = json.loads(yield_files[0].read_text(encoding="utf-8"))
    assert yield_data["rss"]["https://feed.example.com/rss"]["s"] == 1

    # origin fields are internal-only - shouldn't leak into the public payload.
    published_item = json.loads(data_path.read_text(encoding="utf-8"))["sections"]["1"]["items"][0]
    assert "origin_feed" not in published_item


def test_build_main_skips_llm_dedup_without_api_key(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    recent = _iso(now - timedelta(hours=1))
    classified = [
        {"title": "현대차 울산공장, AI 자율제조 라인 3000억 투자", "link": "https://a.example.com/1",
         "source": "한국경제", "summary": "현대차가 울산 공장에 AI 자율제조 라인 구축을 위해 3000억원을 투자한다.",
         "published": recent, "grade": "S", "section": 1, "region": "울산", "loc": None,
         "ulsan_score": 10, "industry": "자동차", "tech": ["자율제조"], "core": 1},
        {"title": "현대차, 울산 AI 자율제조 라인에 3000억 투입", "link": "https://b.example.com/2",
         "source": "연합뉴스", "summary": "현대차가 울산 공장 AI 자율제조 라인에 3000억원을 투입하기로 했다.",
         "published": recent, "grade": "S", "section": 1, "region": "울산", "loc": None,
         "ulsan_score": 10, "industry": "자동차", "tech": ["자율제조"], "core": 1},
    ]

    classified_path = tmp_path / "classified.json"
    data_path = tmp_path / "public" / "data.json"
    logs_dir = tmp_path / "logs"
    classified_path.write_text(json.dumps(classified), encoding="utf-8")

    monkeypatch.setattr(build, "CLASSIFIED_PATH", str(classified_path))
    monkeypatch.setattr(build, "DATA_PATH", str(data_path))
    monkeypatch.setattr(build, "LOGS_DIR", str(logs_dir))
    monkeypatch.setattr(build, "get_collection_hours", lambda: 24)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with patch("anthropic.Anthropic") as mock_anthropic:
        build.main()
        mock_anthropic.assert_not_called()

    result = json.loads(data_path.read_text(encoding="utf-8"))
    # Algorithmic dedup.py alone should already merge this pair (org+number match).
    assert result["article_count"] == 1
