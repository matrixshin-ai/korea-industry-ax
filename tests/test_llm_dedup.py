import json
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


def test_llm_merge_candidates_splits_and_retries_on_malformed_response():
    # 2026-09-29 incident: a chunk's response fails to parse - instead of
    # losing that whole chunk's merges, halve it and retry. Simulate 24 items
    # (all the same event, so every group of them should merge into one) with
    # the full-size call returning malformed JSON, and both halves succeeding.
    items = [_article(f"삼성 6개사, 헬릭스에 10억달러 투자 {i}",
                       f"https://x.example.com/{i}", f"매체{i}") for i in range(24)]
    call_sizes = []

    def fake_create(**kwargs):
        payload = json.loads(kwargs["messages"][0]["content"])
        call_sizes.append(len(payload))
        if len(payload) == 24:
            return _text_response("이것은 잘못된 JSON입니다")
        ids = [item["i"] for item in payload]
        return _text_response(json.dumps([ids]))

    client = MagicMock()
    client.messages.create.side_effect = fake_create

    log = {}
    merge_map = llm_dedup.llm_merge_candidates(client, items, log)

    assert call_sizes == [24, 12, 12]  # 1 failed full attempt + 2 successful half-size attempts
    assert len(log["split_retries"]) == 1
    assert "llm_dedup_errors" not in log
    # Both halves fully merged internally (12 items -> 1 rep + 11 absorbed each half).
    assert len(merge_map) == 22


def test_llm_merge_candidates_gives_up_at_floor_size_after_repeated_failure():
    items = [_article(f"기사{i}", f"https://x.example.com/{i}", "매체") for i in range(21)]
    client = MagicMock()
    client.messages.create.return_value = _text_response("항상 잘못된 응답")

    log = {}
    merge_map = llm_dedup.llm_merge_candidates(client, items, log)

    assert merge_map == {}
    # 21 splits to 10+11 (both <= MIN_CHUNK_SPLIT_SIZE=20) - no further splitting,
    # each logs its own final failure.
    assert len(log["llm_dedup_errors"]) == 2
    assert len(log["split_retries"]) == 1


def test_llm_merge_candidates_truncated_max_tokens_response_triggers_retry():
    items = [_article(f"기사{i}", f"https://x.example.com/{i}", "매체") for i in range(30)]

    def fake_create(**kwargs):
        payload = json.loads(kwargs["messages"][0]["content"])
        if len(payload) == 30:
            return _text_response('[[0', usage_in=100, usage_out=50)  # will be marked truncated below
        ids = [item["i"] for item in payload]
        return _text_response(json.dumps([ids]))

    client = MagicMock()
    client.messages.create.side_effect = fake_create
    # Patch stop_reason per-call: only the first (full-size) response is "max_tokens".
    original_side_effect = client.messages.create.side_effect

    def fake_create_with_stop_reason(**kwargs):
        response = original_side_effect(**kwargs)
        payload = json.loads(kwargs["messages"][0]["content"])
        response.stop_reason = "max_tokens" if len(payload) == 30 else "end_turn"
        return response

    client.messages.create.side_effect = fake_create_with_stop_reason

    log = {}
    merge_map = llm_dedup.llm_merge_candidates(client, items, log)

    assert len(log["split_retries"]) == 1
    assert "max_tokens" in log["split_retries"][0]["error"]
    assert len(merge_map) == 28  # both 15-item halves fully merged internally


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


def _truck_case():
    from fixtures_20261020_truck import ROUNDUP, TRUCK_EVENT, filler_titles
    fillers = [_article(t, f"https://f.example.com/{k}", "매체") for k, t in enumerate(filler_titles(300))]
    events = [_article(t, f"https://n.example.com/{i}", s) for i, t, s in TRUCK_EVENT + [ROUNDUP]]
    # 실제 빌드처럼 같은 사건 기사가 다른 후보 사이에 흩어져 들어온다 (26건 간격, 첫 항목은 다른 후보).
    items = list(fillers)
    for k, ev in enumerate(events):
        items.insert(k * 26 + 13, ev)
    truck_links = {f"https://n.example.com/{i}" for i, _, _ in TRUCK_EVENT}
    return items, truck_links


def test_order_for_chunking_puts_20261020_truck_event_in_one_chunk():
    items, truck_links = _truck_case()
    plain = [{it["link"] for it in c} & truck_links for c in llm_dedup._chunk(items, llm_dedup.MAX_CHUNK_SIZE)]
    assert sum(1 for c in plain if c) > 1  # 정렬 전: 여러 청크에 흩어짐 (10-21 빌드 재현)

    ordered = llm_dedup._order_for_chunking(items)
    assert sorted(it["link"] for it in ordered) == sorted(it["link"] for it in items)  # 순서만 바뀜
    pos = [k for k, it in enumerate(ordered) if it["link"] in truck_links]
    assert pos[-1] - pos[0] <= len(truck_links)  # 사건 기사끼리 붙어 있다 (모음 기사 1건 끼는 정도)
    chunks = [{it["link"] for it in c} & truck_links for c in llm_dedup._chunk(ordered, llm_dedup.MAX_CHUNK_SIZE)]
    assert [len(c) for c in chunks if c] == [len(truck_links)]


def test_llm_dedup_logs_chunk_ids_and_merged_groups():
    import hashlib
    items, truck_links = _truck_case()

    def fake_create(**kwargs):
        # 가짜 Haiku: 트럭 실증 제목만 한 그룹으로 묶고 나머지는 단독.
        payload = json.loads(kwargs["messages"][0]["content"])
        truck = [p["i"] for p in payload if "자율주행" in p["t"] or "대형트럭" in p["t"]]
        rest = [[p["i"]] for p in payload if p["i"] not in truck]
        return _text_response(json.dumps(([truck] if truck else []) + rest))

    client = MagicMock()
    client.messages.create.side_effect = fake_create
    log = {}
    merge_map = llm_dedup.llm_merge_candidates(client, items, log)

    sid = lambda link: hashlib.sha1(link.encode("utf-8")).hexdigest()[:8]
    assert len(log["chunks"]) == log["chunk_count"] == 4
    assert sorted(i for c in log["chunks"] for i in c["ids"]) == sorted(sid(it["link"]) for it in items)
    groups = [g for c in log["chunks"] for g in c["groups"]]
    assert len(groups) == 1 and set(groups[0]) == {sid(l) for l in truck_links}
    assert set(merge_map) | set(merge_map.values()) == truck_links
