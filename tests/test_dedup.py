from dedup import deduplicate_articles


def _article(title, summary, link, source, score=0):
    return {"title": title, "summary": summary, "link": link, "source": source, "score": score,
            "published": "2026-01-05T05:00:00+09:00"}


def test_same_event_same_number_merges_with_related():
    a = _article(
        "현대차 울산공장, AI 자율제조 라인에 3000억원 투자",
        "현대차가 울산 공장에 AI 기반 자율제조 라인 구축을 위해 3000억원을 투자한다고 밝혔다.",
        "https://a.example.com/1", "한국경제",
    )
    b = _article(
        "현대차, 울산 AI 자율제조 라인에 3000억 투입",
        "현대차가 울산 공장 AI 자율제조 라인에 3000억원을 투입하기로 했다.",
        "https://b.example.com/2", "연합뉴스",
    )
    result = deduplicate_articles([a, b])
    assert len(result) == 1
    assert len(result[0]["related"]) == 1
    assert result[0]["related"][0]["link"] in ("https://a.example.com/1", "https://b.example.com/2")


def test_different_stage_same_org_does_not_merge():
    groundbreaking = _article(
        "현대차 울산공장 자율제조 라인 착공",
        "현대차가 울산공장에 자율제조 라인 착공식을 가졌다.",
        "https://c.example.com/3", "한국경제",
    )
    completion = _article(
        "현대차 울산공장 자율제조 라인 준공",
        "현대차가 울산공장 자율제조 라인을 준공했다고 밝혔다.",
        "https://d.example.com/4", "연합뉴스",
    )
    result = deduplicate_articles([groundbreaking, completion])
    assert len(result) == 2


def test_exact_title_duplicate_collapses_to_one():
    a = _article("동일 제목 기사", "요약1", "https://e.example.com/1", "매체A")
    b = _article("동일 제목 기사", "요약2", "https://e.example.com/2", "매체B")
    result = deduplicate_articles([a, b])
    assert len(result) == 1


def test_unrelated_articles_stay_separate_with_empty_related():
    a = _article("울산 조선업 수주 호황", "조선업계가 수주 랠리를 이어가고 있다.", "https://f.example.com/1", "울산매일")
    b = _article("반도체 수출 역대 최대", "반도체 수출이 역대 최대치를 기록했다.", "https://g.example.com/1", "한국경제")
    result = deduplicate_articles([a, b])
    assert len(result) == 2
    assert result[0]["related"] == []
    assert result[1]["related"] == []


def test_empty_input():
    assert deduplicate_articles([]) == []
