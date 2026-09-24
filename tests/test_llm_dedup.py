from unittest.mock import MagicMock

import llm_dedup


def _article(title, link, source, score=0):
    return {"title": title, "link": link, "source": source, "score": score, "related": []}


def _text_response(text, usage_in=100, usage_out=50):
    block = type("Block", (), {"type": "text", "text": text})()
    response = MagicMock()
    response.content = [block]
    response.usage = type("Usage", (), {"input_tokens": usage_in, "output_tokens": usage_out})()
    return response


def test_build_clusters_groups_items_sharing_an_organization():
    a = _article("현대차 울산공장 AI 도입", "https://a.example.com/1", "매체A")
    b = _article("현대차, 울산에서 AI 라인 가동", "https://b.example.com/2", "매체B")
    unrelated = _article("삼성전자 반도체 공장 AI", "https://c.example.com/3", "매체C")

    clusters = llm_dedup.build_clusters([a, b, unrelated])

    # 현대차 isn't a single-word match in ORGANIZATIONS ("현대차" is listed) - both a,b share it.
    assert len(clusters) == 1
    assert {it["link"] for it in clusters[0]} == {"https://a.example.com/1", "https://b.example.com/2"}


def test_build_clusters_skips_singletons():
    only_one = _article("현대차 단독 기사", "https://a.example.com/1", "매체A")
    clusters = llm_dedup.build_clusters([only_one])
    assert clusters == []


def test_llm_merge_clusters_returns_empty_for_no_clusters():
    client = MagicMock()
    assert llm_dedup.llm_merge_clusters(client, [], {}) == {}
    client.messages.create.assert_not_called()


def test_llm_merge_clusters_builds_merge_map_from_response():
    a = _article("우리금융 포항 AI데이터센터 6천억 PF", "https://a.example.com/1", "매체A")
    b = _article("우리금융, 포항 AI데이터센터에 6000억 지원", "https://b.example.com/2", "매체B")
    client = MagicMock()
    client.messages.create.return_value = _text_response('[{"c": 0, "groups": [[0, 1]]}]')

    log = {}
    merge_map = llm_dedup.llm_merge_clusters(client, [[a, b]], log)

    assert len(merge_map) == 1
    absorbed_link, rep_link = next(iter(merge_map.items()))
    assert {absorbed_link, rep_link} == {"https://a.example.com/1", "https://b.example.com/2"}
    assert log["llm_dedup_input_tokens"] == 100
    assert log["llm_dedup_output_tokens"] == 50


def test_llm_merge_clusters_keeps_different_stage_items_separate():
    a = _article("현대차 울산공장 AI라인 착공", "https://a.example.com/1", "매체A")
    b = _article("현대차 울산공장 AI라인 준공", "https://b.example.com/2", "매체B")
    client = MagicMock()
    # Model decides these are different stages - each its own singleton group.
    client.messages.create.return_value = _text_response('[{"c": 0, "groups": [[0], [1]]}]')

    merge_map = llm_dedup.llm_merge_clusters(client, [[a, b]], {})

    assert merge_map == {}


def test_llm_merge_clusters_handles_api_error_gracefully():
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("simulated failure")
    a = _article("A", "https://a.example.com/1", "매체A")
    b = _article("B", "https://b.example.com/2", "매체B")

    log = {}
    merge_map = llm_dedup.llm_merge_clusters(client, [[a, b]], log)

    assert merge_map == {}
    assert "llm_dedup_error" in log


def test_apply_merge_map_folds_absorbed_into_related():
    a = _article("대표 기사", "https://a.example.com/1", "매체A")
    b = _article("흡수된 기사", "https://b.example.com/2", "매체B")
    items = [a, b]
    merge_map = {"https://b.example.com/2": "https://a.example.com/1"}

    result = llm_dedup.apply_merge_map(items, merge_map)

    assert len(result) == 1
    assert result[0]["link"] == "https://a.example.com/1"
    # Full absorbed item is kept (not reduced to source/link) so a later
    # grade-aware re-representation pass can still see its grade.
    assert len(result[0]["related"]) == 1
    assert result[0]["related"][0]["link"] == "https://b.example.com/2"
    assert result[0]["related"][0]["source"] == "매체B"


def test_apply_merge_map_no_merges_returns_all_items_unchanged():
    a = _article("A", "https://a.example.com/1", "매체A")
    b = _article("B", "https://b.example.com/2", "매체B")
    result = llm_dedup.apply_merge_map([a, b], {})
    assert len(result) == 2
