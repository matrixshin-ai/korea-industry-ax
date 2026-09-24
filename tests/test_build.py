import json
from datetime import datetime, timedelta, timezone

import build


def _iso(dt):
    return dt.isoformat()


def test_build_main_drops_irrelevant_dedupes_and_sorts(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    recent = _iso(now - timedelta(hours=1))
    old = _iso(now - timedelta(hours=100))  # outside even the 48h Monday window

    classified = [
        {"title": "관련없는 기사", "link": "https://x.example.com/1", "source": "매체", "summary": "",
         "published": recent, "relevant": False, "section": None, "region": "국내",
         "ulsan_score": 0, "ax": 0, "industry": None, "tech": [], "core": 0},
        {"title": "미분류 기사", "link": "https://x.example.com/2", "source": "매체", "summary": "",
         "published": recent, "relevant": None, "unclassified": True},
        {"title": "기간 밖 기사", "link": "https://x.example.com/3", "source": "매체", "summary": "",
         "published": old, "relevant": True, "section": 1, "region": "울산",
         "ulsan_score": 10, "ax": 50, "industry": "조선", "tech": ["로봇"], "core": 1},
        {"title": "낮은 점수 기사", "link": "https://x.example.com/4", "source": "매체A", "summary": "",
         "published": recent, "relevant": True, "section": 1, "region": "국내",
         "ulsan_score": 0, "ax": 20, "industry": "금융", "tech": [], "core": 0},
        {"title": "높은 점수 기사", "link": "https://x.example.com/5", "source": "매체B", "summary": "",
         "published": recent, "relevant": True, "section": 1, "region": "울산",
         "ulsan_score": 10, "ax": 50, "industry": "조선", "tech": ["로봇"], "core": 1},
        {"title": "정책 기사", "link": "https://x.example.com/6", "source": "매체C", "summary": "",
         "published": recent, "relevant": True, "section": 3, "region": "국내",
         "ulsan_score": 0, "ax": 30, "industry": "공공", "tech": [], "core": 0},
    ]

    classified_path = tmp_path / "classified.json"
    data_path = tmp_path / "public" / "data.json"
    logs_dir = tmp_path / "logs"
    classified_path.write_text(json.dumps(classified), encoding="utf-8")

    monkeypatch.setattr(build, "CLASSIFIED_PATH", str(classified_path))
    monkeypatch.setattr(build, "DATA_PATH", str(data_path))
    monkeypatch.setattr(build, "LOGS_DIR", str(logs_dir))
    monkeypatch.setattr(build, "get_collection_hours", lambda: 24)

    build.main()

    result = json.loads(data_path.read_text(encoding="utf-8"))

    section1_titles = [it["title"] for it in result["sections"]["1"]["items"]]
    assert "관련없는 기사" not in section1_titles
    assert "미분류 기사" not in section1_titles
    assert "기간 밖 기사" not in section1_titles
    assert section1_titles == ["높은 점수 기사", "낮은 점수 기사"]

    high = result["sections"]["1"]["items"][0]
    assert high["score"] == 50 + 10 + 1 * 10  # ax + ulsan_score + core*10 == 70

    section3_titles = [it["title"] for it in result["sections"]["3"]["items"]]
    assert section3_titles == ["정책 기사"]

    assert result["collection_hours"] == 24
    assert result["stats"]["dropped_not_relevant"] == 2
    assert result["stats"]["dropped_out_of_window"] == 1
