import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

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
        {"title": "울산 B등급 기사", "link": "https://x.example.com/4b", "source": "매체D", "summary": "",
         "published": recent, "grade": "B", "section": 1, "region": "울산", "loc": None,
         "ulsan_score": 8, "industry": "기타", "tech": [], "core": 0},  # B is never published (requirement 1), regardless of ulsan_score
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
    assert "B등급 기사" not in section1_titles  # B is never published, regardless of ulsan_score
    assert "울산 B등급 기사" not in section1_titles  # same - ulsan_score 8 no longer publishes B
    assert section1_titles == ["S등급 기사"]

    top = result["sections"]["1"]["items"][0]
    assert top["score"] == 100 + 10 + 1 * 10  # S(100) + ulsan(10) + core(1)*10 == 110
    assert top["grade"] == "S"

    section3_titles = [it["title"] for it in result["sections"]["3"]["items"]]
    assert section3_titles == ["A등급 정책 기사"]

    assert result["collection_hours"] == 24
    assert result["stats"]["dropped_unclassified"] == 1  # the grade=None item
    assert result["stats"]["dropped_not_published_after_regrade"] == 4  # X, C, B(u=0), B(u=8)
    assert result["stats"]["dropped_out_of_window"] == 1
    assert result["stats"]["published_grade_counts"] == {"S": 1, "A": 1}


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
         "published": recent, "grade": "A", "section": 1, "region": "전국", "loc": None,
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


def test_build_main_uses_llm_dedup_full_title_pass_when_algo_dedup_cannot_merge(tmp_path, monkeypatch):
    # Requirement 3: two S-graded articles about the same event but worded too
    # differently for dedup.py's algorithmic gate (no shared org/number/quote)
    # - only the full-title Haiku pass can merge these.
    now = datetime.now(timezone.utc)
    recent = _iso(now - timedelta(hours=1))
    classified = [
        {"title": "울산 어느 조선소, 로봇 용접 도입해 생산성 확 끌어올려", "link": "https://a.example.com/1",
         "source": "매체A", "summary": "", "published": recent, "grade": "S", "section": 1, "region": "울산",
         "loc": None, "ulsan_score": 10, "industry": "조선", "tech": ["로봇"], "core": 1},
        {"title": "조선업계 최초 완전자동 용접 라인 가동 시작", "link": "https://b.example.com/2",
         "source": "매체B", "summary": "", "published": recent, "grade": "S", "section": 1, "region": "울산",
         "loc": None, "ulsan_score": 10, "industry": "조선", "tech": ["로봇"], "core": 1},
    ]

    classified_path = tmp_path / "classified.json"
    data_path = tmp_path / "public" / "data.json"
    logs_dir = tmp_path / "logs"
    classified_path.write_text(json.dumps(classified), encoding="utf-8")

    monkeypatch.setattr(build, "CLASSIFIED_PATH", str(classified_path))
    monkeypatch.setattr(build, "DATA_PATH", str(data_path))
    monkeypatch.setattr(build, "LOGS_DIR", str(logs_dir))
    monkeypatch.setattr(build, "get_collection_hours", lambda: 24)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    block = type("Block", (), {"type": "text", "text": "[[0, 1]]"})()
    response = MagicMock()
    response.content = [block]
    response.usage = type("Usage", (), {"input_tokens": 10, "output_tokens": 5})()

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.return_value = response
        build.main()
        mock_anthropic.return_value.messages.create.assert_called_once()

    result = json.loads(data_path.read_text(encoding="utf-8"))
    assert result["article_count"] == 1
    assert len(result["sections"]["1"]["items"][0]["related"]) == 1


def _setup_main(tmp_path, monkeypatch, classified):
    classified_path = tmp_path / "classified.json"
    data_path = tmp_path / "public" / "data.json"
    classified_path.write_text(json.dumps(classified), encoding="utf-8")
    monkeypatch.setattr(build, "CLASSIFIED_PATH", str(classified_path))
    monkeypatch.setattr(build, "DATA_PATH", str(data_path))
    monkeypatch.setattr(build, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(build, "get_collection_hours", lambda: 24)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return data_path


def test_is_publishable_rules():
    # Requirement 1: only S/A publish. B never does, regardless of ulsan_score.
    assert build.is_publishable({"grade": "S", "ulsan_score": 0})
    assert build.is_publishable({"grade": "A", "ulsan_score": 0})
    assert not build.is_publishable({"grade": "B", "ulsan_score": 10})
    assert not build.is_publishable({"grade": "B", "ulsan_score": 8})
    assert not build.is_publishable({"grade": "B", "ulsan_score": 4})
    assert not build.is_publishable({"grade": "C", "ulsan_score": 10})
    assert not build.is_publishable({"grade": "X", "ulsan_score": 10})


def test_build_main_applies_daily_cap_by_score_across_sections(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    classified = []
    # 150 A-grade (score 70) in section 2, 100 S-grade (score 100) in section 3:
    # the cap keeps all 100 S plus the 100 newest A.
    for i in range(150):
        classified.append({"title": f"A기사 가{i} 나{i*7}", "link": f"https://a.example.com/{i}", "source": "매체",
                           "summary": "", "published": _iso(now - timedelta(minutes=i + 1)), "grade": "A",
                           "section": 2, "region": "전국", "loc": None, "ulsan_score": 0,
                           "industry": "기타", "tech": [], "core": 0})
    for i in range(100):
        classified.append({"title": f"S기사 다{i} 라{i*7}", "link": f"https://s.example.com/{i}", "source": "매체",
                           "summary": "", "published": _iso(now - timedelta(minutes=i + 1)), "grade": "S",
                           "section": 3, "region": "전국", "loc": None, "ulsan_score": 0,
                           "industry": "기타", "tech": [], "core": 0})
    data_path = _setup_main(tmp_path, monkeypatch, classified)

    build.main()

    result = json.loads(data_path.read_text(encoding="utf-8"))
    assert result["article_count"] == build.DAILY_CAP == 200
    assert result["stats"]["publishable_before_cap"] == 250
    assert result["stats"]["publishable_before_cap_by_section"] == {"1": 0, "2": 150, "3": 100}
    assert result["stats"]["dropped_over_cap"] == 50
    assert len(result["sections"]["3"]["items"]) == 100
    sec2 = result["sections"]["2"]["items"]
    assert len(sec2) == 100
    assert sec2[0]["link"] == "https://a.example.com/0"  # newest first within equal score
    assert "https://a.example.com/149" not in {it["link"] for it in sec2}


def test_regrade_representatives_swaps_representative_when_best_title_lacks_ax_content():
    # Requirement 4: the highest (grade, score) member is picked first - but if
    # its title doesn't itself name AI/AX (e.g. the summit's foreign-policy
    # angle, "군함 건조", rather than its AX announcement), fall back to the
    # highest-ranked S/A member whose title DOES name AI/AX.
    no_ax_title = {"title": "李 대통령, 뉴욕서 트럼프와 군함 건조 협력 논의", "link": "https://a.example.com/1",
                   "source": "매체A", "grade": "S", "section": 3, "ulsan_score": 0, "core": 0}
    ax_title = {"title": "李 대통령, 뉴욕 투자서밋서 AI 메가프로젝트 발표", "link": "https://b.example.com/2",
                "source": "매체B", "grade": "A", "section": 3, "ulsan_score": 0, "core": 0}
    host = {**no_ax_title, "related": [ax_title]}

    result = build.regrade_representatives([host])

    assert len(result) == 1
    assert result[0]["grade"] == "A"
    assert result[0]["link"] == "https://b.example.com/2"
    related_links = {r["link"] for r in result[0]["related"]}
    assert related_links == {"https://a.example.com/1"}


def test_regrade_representatives_keeps_best_when_no_ax_titled_alternative_exists():
    # If no member's title names AI/AX, keep the highest (grade, score) pick
    # as-is rather than leaving the group without a representative.
    no_ax_title = {"title": "李 대통령, 뉴욕서 트럼프와 군함 건조 협력 논의", "link": "https://a.example.com/1",
                   "source": "매체A", "grade": "S", "section": 3, "ulsan_score": 0, "core": 0}
    also_no_ax = {"title": "李 대통령, 월가 인사들과 만찬", "link": "https://b.example.com/2",
                  "source": "매체B", "grade": "A", "section": 3, "ulsan_score": 0, "core": 0}
    host = {**no_ax_title, "related": [also_no_ax]}

    result = build.regrade_representatives([host])

    assert result[0]["grade"] == "S"
    assert result[0]["link"] == "https://a.example.com/1"
