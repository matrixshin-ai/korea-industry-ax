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


def test_llm_merge_candidates_returns_empty_for_no_items():
    client = MagicMock()
    assert llm_dedup.llm_merge_candidates(client, [], {}) == {}
    client.messages.create.assert_not_called()


def test_llm_merge_candidates_builds_merge_map_from_response():
    a = _article("우리금융 포항 AI데이터센터 6천억 PF", "https://a.example.com/1", "매체A")
    b = _article("우리금융, 포항 AI데이터센터에 6000억 지원", "https://b.example.com/2", "매체B")
    client = MagicMock()
    client.messages.create.return_value = _text_response('[[0, 1]]')

    log = {}
    merge_map = llm_dedup.llm_merge_candidates(client, [a, b], log)

    assert len(merge_map) == 1
    absorbed_link, rep_link = next(iter(merge_map.items()))
    assert {absorbed_link, rep_link} == {"https://a.example.com/1", "https://b.example.com/2"}
    assert log["llm_dedup_input_tokens"] == 100
    assert log["llm_dedup_output_tokens"] == 50
    assert log["chunk_count"] == 1


def test_llm_merge_candidates_keeps_different_stage_items_separate():
    a = _article("현대차 울산공장 AI라인 착공", "https://a.example.com/1", "매체A")
    b = _article("현대차 울산공장 AI라인 준공", "https://b.example.com/2", "매체B")
    client = MagicMock()
    # Model decides these are different stages - each its own singleton group.
    client.messages.create.return_value = _text_response('[[0], [1]]')

    merge_map = llm_dedup.llm_merge_candidates(client, [a, b], {})

    assert merge_map == {}


def test_llm_merge_candidates_handles_api_error_gracefully():
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("simulated failure")
    a = _article("A", "https://a.example.com/1", "매체A")
    b = _article("B", "https://b.example.com/2", "매체B")

    log = {}
    merge_map = llm_dedup.llm_merge_candidates(client, [a, b], log)

    assert merge_map == {}
    assert "llm_dedup_errors" in log


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


def test_llm_merge_candidates_splits_into_chunks_of_max_chunk_size(monkeypatch):
    monkeypatch.setattr(llm_dedup, "MAX_CHUNK_SIZE", 2)
    items = [_article(f"기사{i}", f"https://x.example.com/{i}", "매체") for i in range(5)]
    client = MagicMock()
    # Every chunk: no merges (each id its own group).
    client.messages.create.return_value = _text_response('[[0], [1]]')

    log = {}
    merge_map = llm_dedup.llm_merge_candidates(client, items, log)

    assert merge_map == {}
    assert log["chunk_count"] == 3  # ceil(5/2)
    assert client.messages.create.call_count == 3


def test_llm_merge_candidates_ignores_duplicate_id_across_groups_in_one_response():
    # A malformed response repeats id 1 across two groups - the first group it
    # appears in wins, the second (which would otherwise chain c into a+b) is
    # ignored for that id (defensive guard against a hallucinated response).
    a = _article("현대차 울산 AI 라인 3000억 투자", "https://a.example.com/1", "매체A")
    b = _article("현대차 AI 라인 3000억", "https://b.example.com/2", "매체B")
    c = _article("무관 기사", "https://c.example.com/3", "매체C")
    client = MagicMock()
    client.messages.create.return_value = _text_response('[[0, 1], [1, 2]]')

    merge_map = llm_dedup.llm_merge_candidates(client, [a, b, c], {})

    assert merge_map == {"https://b.example.com/2": "https://a.example.com/1"}
    assert "https://c.example.com/3" not in merge_map
    assert "https://c.example.com/3" not in merge_map.values()
