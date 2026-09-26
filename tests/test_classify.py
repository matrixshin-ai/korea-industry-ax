import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import classify


def _usage(input_tokens=100, output_tokens=50):
    return type("Usage", (), {"input_tokens": input_tokens, "output_tokens": output_tokens})()


def _text_response(text, stop_reason="end_turn", usage=None):
    block = type("Block", (), {"type": "text", "text": text})()
    response = MagicMock()
    response.content = [block]
    response.usage = usage or _usage()
    response.stop_reason = stop_reason
    return response


def test_keyword_prefilter_passes_on_keyword_in_title():
    assert classify.passes_keyword_prefilter({"title": "현대차 AI 자율제조 라인 구축", "summary": ""}) is True


def test_keyword_prefilter_rejects_unrelated_article():
    assert classify.passes_keyword_prefilter({"title": "코스피 2% 상승 마감", "summary": "외국인 순매수 전환"}) is False


def test_keyword_prefilter_matches_digital_twin_without_space():
    assert classify.passes_keyword_prefilter({"title": "디지털트윈 기반 재난안전망", "summary": ""}) is True


def test_parse_json_array_plain():
    assert classify.parse_json_array('[{"i": 0, "g": "X"}]') == [{"i": 0, "g": "X"}]


def test_parse_json_array_strips_code_fence():
    text = '```json\n[{"i": 0, "g": "X"}]\n```'
    assert classify.parse_json_array(text) == [{"i": 0, "g": "X"}]


def test_expand_compact_result_x_is_minimal():
    assert classify.expand_compact_result({"i": 0, "m": "언급", "g": "X"}) == {
        "grade": "X", "m": "언급", "section": None, "region": None, "loc": None, "ulsan_score": 0,
        "industry": None, "tech": [], "core": 0, "evidence": None,
    }


def test_expand_compact_result_s_maps_all_fields():
    compact = {"i": 0, "m": "주제", "g": "S", "s": 1, "r": "울산", "loc": None, "u": 10,
               "ind": "조선", "tech": ["로봇", "피지컬AI", "기타없는값"], "core": 1,
               "e": "HD현대重-발전엔진공장 신설-울산"}
    expanded = classify.expand_compact_result(compact)
    assert expanded["grade"] == "S"
    assert expanded["m"] == "주제"
    assert expanded["section"] == 1
    assert expanded["region"] == "울산"
    assert expanded["loc"] is None
    assert expanded["ulsan_score"] == 10
    assert expanded["industry"] == "조선"
    assert expanded["tech"] == ["로봇", "피지컬AI"]  # capped at 2
    assert expanded["core"] == 1
    assert expanded["evidence"] == "HD현대重-발전엔진공장 신설-울산"


def test_expand_compact_result_m_bujeon_caps_grade_at_b():
    s_capped = classify.expand_compact_result(
        {"i": 0, "m": "부분", "g": "S", "s": 1, "r": "전국", "u": 0, "ind": "조선", "tech": [], "core": 0}
    )
    assert s_capped["grade"] == "B"

    a_capped = classify.expand_compact_result(
        {"i": 0, "m": "부분", "g": "A", "s": 1, "r": "전국", "u": 0, "ind": "조선", "tech": [], "core": 0}
    )
    assert a_capped["grade"] == "B"

    c_unaffected = classify.expand_compact_result(
        {"i": 0, "m": "부분", "g": "C", "s": 1, "r": "전국", "u": 0, "ind": "조선", "tech": [], "core": 0}
    )
    assert c_unaffected["grade"] == "C"  # already <= B, cap is a no-op


def test_expand_compact_result_m_eongeup_forces_x_regardless_of_grade():
    result = classify.expand_compact_result(
        {"i": 0, "m": "언급", "g": "S", "s": 1, "r": "울산", "u": 10, "ind": "조선", "tech": [],
         "core": 1, "e": "누군가-뭔가 했다-어딘가"}
    )
    assert result["grade"] == "X"
    assert result["m"] == "언급"


def test_expand_compact_result_missing_section_forces_x_instead_of_none():
    # A malformed Haiku response (missing/null "s") for a non-X grade must not
    # silently produce section=None - that crashes build.py's section lookup.
    missing = classify.expand_compact_result(
        {"i": 0, "m": "주제", "g": "B", "r": "전국", "u": 0, "ind": "금융", "tech": [], "core": 0}
    )
    assert missing["grade"] == "X"
    assert missing["section"] is None

    invalid = classify.expand_compact_result(
        {"i": 0, "m": "주제", "g": "B", "s": 7, "r": "전국", "u": 0, "ind": "금융", "tech": [], "core": 0}
    )
    assert invalid["grade"] == "X"


def test_expand_compact_result_missing_evidence_downgrades_s_and_a_by_one_level():
    s_no_evidence = classify.expand_compact_result(
        {"i": 0, "m": "주제", "g": "S", "s": 1, "r": "울산", "u": 10, "ind": "조선", "tech": [], "core": 0}
    )
    assert s_no_evidence["grade"] == "A"
    assert s_no_evidence["evidence"] is None

    a_no_evidence = classify.expand_compact_result(
        {"i": 0, "m": "주제", "g": "A", "s": 1, "r": "전국", "u": 0, "ind": "조선", "tech": [], "core": 0}
    )
    assert a_no_evidence["grade"] == "B"
    assert a_no_evidence["evidence"] is None

    s_with_evidence = classify.expand_compact_result(
        {"i": 0, "m": "주제", "g": "S", "s": 1, "r": "울산", "u": 10, "ind": "조선", "tech": [], "core": 0,
         "e": "현대차-휴머노이드 학습-울산공장"}
    )
    assert s_with_evidence["grade"] == "S"
    assert s_with_evidence["evidence"] == "현대차-휴머노이드 학습-울산공장"


def test_expand_compact_result_loc_only_kept_for_tajachidae():
    kept = classify.expand_compact_result({"i": 0, "g": "B", "s": 1, "r": "타지자체", "loc": "경북",
                                             "u": 0, "ind": "반도체", "tech": [], "core": 0})
    assert kept["loc"] == "경북"

    dropped = classify.expand_compact_result({"i": 0, "g": "B", "s": 1, "r": "전국", "loc": "경북",
                                                "u": 0, "ind": "반도체", "tech": [], "core": 0})
    assert dropped["loc"] is None  # loc only valid when region is 타지자체


def test_expand_compact_result_invalid_loc_value_dropped():
    result = classify.expand_compact_result({"i": 0, "g": "B", "s": 1, "r": "타지자체", "loc": "존재하지않는시도",
                                               "u": 0, "ind": "반도체", "tech": [], "core": 0})
    assert result["loc"] is None


def test_expand_compact_result_sanitizes_invalid_industry_to_gita():
    expanded = classify.expand_compact_result({"i": 0, "g": "B", "s": 1, "r": "전국", "u": 0,
                                                 "ind": "존재하지않는업종", "tech": [], "core": 0})
    assert expanded["industry"] == "기타"


def test_expand_compact_result_it_industry_option_kept():
    expanded = classify.expand_compact_result({"i": 0, "g": "A", "s": 2, "r": "전국", "u": 0,
                                                 "ind": "IT·통신·데이터센터", "tech": ["데이터센터"], "core": 0})
    assert expanded["industry"] == "IT·통신·데이터센터"


def test_expand_compact_result_ulsan_score_forced_zero_when_region_not_ulsan():
    # Requirement 2: u can only be positive when region == "울산" - a
    # defense-in-depth check in code, not just in the prompt.
    for region in ("타지자체", "전국", "해외"):
        result = classify.expand_compact_result(
            {"i": 0, "m": "주제", "g": "A", "s": 1, "r": region, "u": 10, "ind": "조선", "tech": [], "core": 0,
             "e": "e"}
        )
        assert result["ulsan_score"] == 0, region

    kept = classify.expand_compact_result(
        {"i": 0, "m": "주제", "g": "S", "s": 1, "r": "울산", "u": 10, "ind": "조선", "tech": [], "core": 0,
         "e": "e"}
    )
    assert kept["ulsan_score"] == 10


def test_expand_compact_result_core_requires_core_industry_and_section_1():
    not_core_industry = classify.expand_compact_result(
        {"i": 0, "g": "S", "s": 1, "r": "전국", "u": 0, "ind": "금융", "tech": [], "core": 1}
    )
    assert not_core_industry["core"] == 0

    not_section_1 = classify.expand_compact_result(
        {"i": 0, "g": "S", "s": 3, "r": "전국", "u": 0, "ind": "조선", "tech": [], "core": 1}
    )
    assert not_section_1["core"] == 0

    valid = classify.expand_compact_result(
        {"i": 0, "g": "S", "s": 1, "r": "울산", "u": 10, "ind": "조선", "tech": [], "core": 1}
    )
    assert valid["core"] == 1


def test_cache_key_stable_across_tracking_params():
    a = classify.cache_key("https://example.com/a/1?utm_source=naver")
    b = classify.cache_key("https://example.com/a/1")
    assert a == b


def test_fields_to_cache_entry_and_back_roundtrip():
    fields = {"grade": "A", "m": "주제", "section": 2, "region": "타지자체", "loc": "경남", "ulsan_score": 0,
              "industry": "반도체", "tech": ["데이터센터"], "core": 0, "evidence": "OO사-AI반도체 실증-경남공장"}
    entry = classify.fields_to_cache_entry(fields, "2026-01-05T05:00:00+09:00")
    assert entry["pub"] == "2026-01-05T05:00:00+09:00"
    assert classify.cache_entry_to_fields(entry) == fields


def test_fields_to_cache_entry_x_grade_is_minimal():
    entry = classify.fields_to_cache_entry({"grade": "X", "m": "언급"}, "2026-01-05T05:00:00+09:00")
    assert entry == {"g": "X", "m": "언급", "pub": "2026-01-05T05:00:00+09:00", "v": classify.PROMPT_VERSION}


def test_load_and_prune_cache_drops_entries_older_than_48h(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=10)).isoformat()
    stale = (now - timedelta(hours=60)).isoformat()
    cache_path = tmp_path / "classify_cache.json"
    cache_path.write_text(json.dumps({
        "fresh_key": {"g": "B", "s": 1, "r": "전국", "loc": None, "u": 0, "ind": "금융", "t": [], "core": 0, "pub": fresh},
        "stale_key": {"g": "X", "pub": stale},
    }), encoding="utf-8")
    monkeypatch.setattr(classify, "CLASSIFY_CACHE_PATH", str(cache_path))

    pruned = classify.load_and_prune_cache()

    assert "fresh_key" in pruned
    assert "stale_key" not in pruned


def test_load_and_prune_cache_missing_file_is_cold_start(tmp_path, monkeypatch):
    monkeypatch.setattr(classify, "CLASSIFY_CACHE_PATH", str(tmp_path / "does_not_exist.json"))
    assert classify.load_and_prune_cache() == {}


def test_classify_groups_sync_marks_unclassified_on_malformed_json_after_min_split():
    client = MagicMock()
    client.messages.create.return_value = _text_response("이것은 JSON이 아닙니다")

    items = [{"id": i, "title": f"t{i}", "summary": "s", "source": "src"} for i in range(10)]
    log = {"failed_batches": []}

    results = classify.classify_groups_sync(client, [items], log)

    assert all(results[i]["unclassified"] is True for i in range(10))
    assert len(log["failed_batches"]) == 1  # already at MIN_SPLIT_SIZE, no further split


def test_classify_groups_sync_marks_unclassified_on_api_exception():
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("simulated API failure")

    items = [{"id": i, "title": f"t{i}", "summary": "s", "source": "src"} for i in range(10)]
    log = {"failed_batches": []}

    results = classify.classify_groups_sync(client, [items], log)

    assert all(results[i]["unclassified"] is True for i in range(10))
    assert len(log["failed_batches"]) == 1


def test_classify_groups_sync_success_uses_compact_schema():
    client = MagicMock()
    client.messages.create.return_value = _text_response(
        '[{"i":0,"m":"주제","g":"S","s":1,"r":"울산","loc":null,"u":10,"ind":"조선","tech":["로봇"],"core":1,'
        '"e":"현대차-휴머노이드 학습-울산공장"},{"i":1,"m":"언급","g":"X"}]'
    )
    items = [
        {"id": 0, "title": "t0", "summary": "s0", "source": "src"},
        {"id": 1, "title": "t1", "summary": "s1", "source": "src"},
    ]
    log = {"failed_batches": []}

    results = classify.classify_groups_sync(client, [items], log)

    assert results[0]["grade"] == "S"
    assert results[0]["section"] == 1
    assert results[1]["grade"] == "X"
    assert log["failed_batches"] == []
    assert log["sync_input_tokens"] == 100
    assert log["sync_output_tokens"] == 50


def test_classify_groups_sync_splits_in_half_on_truncated_max_tokens_then_succeeds():
    client = MagicMock()
    big_group = [{"id": i, "title": f"t{i}", "summary": "s", "source": "src"} for i in range(20)]

    call_count = {"n": 0}

    def fake_create(**kwargs):
        call_count["n"] += 1
        content = json.loads(kwargs["messages"][0]["content"])
        if len(content) == 20:
            return _text_response('[{"i":0', stop_reason="max_tokens")
        ids = [item["i"] for item in content]
        payload = json.dumps([{"i": i, "g": "X"} for i in ids])
        return _text_response(payload)

    client.messages.create.side_effect = fake_create
    log = {"failed_batches": []}

    results = classify.classify_groups_sync(client, [big_group], log)

    assert len(results) == 20
    assert all(v["grade"] == "X" for v in results.values())
    assert call_count["n"] == 3  # 1 truncated full attempt + 2 successful half-size attempts
    assert log["failed_batches"] == []
    assert len(log["split_retries"]) == 1


def test_classify_via_batches_api_completes_without_timeout():
    client = MagicMock()
    client.messages.batches.create.return_value = type("B", (), {"id": "batch_1", "processing_status": "ended"})()
    msg = _text_response(
        '[{"i":0,"m":"주제","g":"A","s":2,"r":"전국","loc":null,"u":0,"ind":"반도체","tech":[],"core":0,'
        '"e":"OO연구소-AI반도체 실증-대전"}]'
    )
    result = type("R", (), {"custom_id": "g0", "result": type("Res", (), {"type": "succeeded", "message": msg})()})()
    client.messages.batches.results.return_value = [result]

    items = [{"id": 0, "title": "t0", "summary": "s0", "source": "src"}]
    log = {"failed_batches": []}

    results = classify.classify_via_batches_api(client, items, log, timeout_seconds=5, poll_interval=0.01)

    assert results[0]["grade"] == "A"
    assert results[0]["section"] == 2
    client.messages.batches.cancel.assert_not_called()
    assert log["batches_api_timed_out"] is False


def test_classify_via_batches_api_timeout_cancels_and_falls_back_to_sync():
    client = MagicMock()
    in_progress = type("B", (), {"id": "batch_2", "processing_status": "in_progress"})()
    client.messages.batches.create.return_value = in_progress
    client.messages.batches.retrieve.return_value = in_progress
    client.messages.batches.results.return_value = []
    client.messages.create.return_value = _text_response('[{"i":0,"g":"X"}]')

    items = [{"id": 0, "title": "t0", "summary": "s0", "source": "src"}]
    log = {"failed_batches": []}

    results = classify.classify_via_batches_api(
        client, items, log, timeout_seconds=0.05, poll_interval=0.01, cancel_wait_seconds=0.05,
    )

    client.messages.batches.cancel.assert_called_once_with("batch_2")
    assert log["batches_api_timed_out"] is True
    assert log["batches_api_fallback_item_count"] == 1
    assert results[0]["grade"] == "X"
    client.messages.create.assert_called_once()


def test_main_uses_cache_and_skips_api_call(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    candidates = [
        {"title": "현대차 울산공장 AI 휴머노이드 학습", "summary": "요약", "link": "https://example.com/cached", "source": "매체",
         "published": (now - timedelta(hours=5)).isoformat()},
    ]
    cache = {
        classify.cache_key("https://example.com/cached"): {
            "g": "S", "m": "주제", "s": 1, "r": "울산", "loc": None, "u": 10, "ind": "조선", "t": ["로봇"],
            "core": 1, "e": "현대차-휴머노이드 학습-울산공장",
            "pub": (now - timedelta(hours=5)).isoformat(), "v": classify.PROMPT_VERSION,
        }
    }

    candidates_path = tmp_path / "candidates.json"
    cache_path = tmp_path / "data" / "classify_cache.json"
    cache_path.parent.mkdir()
    classified_path = tmp_path / "classified.json"
    logs_dir = tmp_path / "logs"

    candidates_path.write_text(json.dumps(candidates), encoding="utf-8")
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    monkeypatch.setattr(classify, "CANDIDATES_PATH", str(candidates_path))
    monkeypatch.setattr(classify, "CLASSIFY_CACHE_PATH", str(cache_path))
    monkeypatch.setattr(classify, "CLASSIFIED_PATH", str(classified_path))
    monkeypatch.setattr(classify, "LOGS_DIR", str(logs_dir))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with patch("anthropic.Anthropic") as mock_anthropic:
        classify.main()
        mock_anthropic.assert_not_called()

    result = json.loads(classified_path.read_text(encoding="utf-8"))
    assert result[0]["grade"] == "S"
    assert result[0]["section"] == 1


def test_main_cold_start_with_no_cache_file_still_works(tmp_path, monkeypatch):
    candidates = [
        {"title": "코스피 상승", "summary": "요약", "link": "https://example.com/1", "source": "매체",
         "published": datetime.now(timezone.utc).isoformat()},
    ]
    candidates_path = tmp_path / "candidates.json"
    candidates_path.write_text(json.dumps(candidates), encoding="utf-8")

    monkeypatch.setattr(classify, "CANDIDATES_PATH", str(candidates_path))
    monkeypatch.setattr(classify, "CLASSIFY_CACHE_PATH", str(tmp_path / "data" / "classify_cache.json"))
    monkeypatch.setattr(classify, "CLASSIFIED_PATH", str(tmp_path / "classified.json"))
    monkeypatch.setattr(classify, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    classify.main()  # must not raise despite no cache file existing

    result = json.loads((tmp_path / "classified.json").read_text(encoding="utf-8"))
    assert result[0]["grade"] == "X"  # no AI keyword -> filtered out, cold start still works


def test_is_finance_title_blocks_securities_keywords():
    for t in ["SK그룹주 ETF의 귀환, AI 확장 기대감", "[특징주] 로봇株 급등", "AI 반도체 목표가 상향",
              "공모주 청약 AI 기업 몰려", "AI 펀드 수익률", "증시 AI 랠리", "AI 종목 추천", "주주환원 확대",
              "AI 스타트업 코스닥 상장 추진", "현대차 주가 AI 기대"]:
        assert classify.is_finance_title(t), t


def test_is_finance_title_ulsan_ai_exception_and_non_finance():
    assert not classify.is_finance_title("울산 AI 기업 코스닥 상장 추진")
    assert not classify.is_finance_title("울산 AX 펀드 조성")
    assert classify.is_finance_title("울산 기업 코스닥 상장 추진")  # 울산 without AI/AX
    assert not classify.is_finance_title("현대차 울산공장 AI 자율제조 도입")


def test_has_ai_term_word_boundaries():
    assert classify.has_ai_term("AI팩토리 구축")
    assert classify.has_ai_term("M.AX 얼라이언스")
    assert classify.has_ai_term("AIDC 유치")
    assert classify.has_ai_term("인공지능 전환")
    assert not classify.has_ai_term("MAX 성능 SAIL 프로젝트")
    assert not classify.has_ai_term("발전엔진 공장 신설")


def test_apply_rule_caps():
    base = {"grade": "S", "evidence": "e"}
    # AI in title -> S kept
    assert classify.apply_rule_caps({**base, "title": "현대차 울산공장 AI 도입", "summary": ""})["grade"] == "S"
    # AI only in summary's first sentence -> S kept
    assert classify.apply_rule_caps({**base, "title": "현대차 울산공장 혁신", "summary": "AI를 도입했다. 끝."})["grade"] == "S"
    # AI only deeper in summary -> A
    r = classify.apply_rule_caps({**base, "title": "국가반도체연구소 설립", "summary": "2조 투입. 향후 AI 반도체도."})
    assert r["grade"] == "A" and r["evidence"] == "e" and r["rule_capped_from"] == "S"
    # no AI at all -> B, evidence dropped
    r = classify.apply_rule_caps({**base, "title": "발전엔진·SMR 공장 신설 맞손", "summary": "울산 온산에 공장."})
    assert r["grade"] == "B" and r["evidence"] is None
    # data-center power infrastructure -> A even with AI
    assert classify.apply_rule_caps({**base, "title": "AI 데이터센터 전력망 확충", "summary": ""})["grade"] == "A"
    # non-S untouched
    a = {"grade": "A", "title": "무관", "summary": ""}
    assert classify.apply_rule_caps(a) is a


def _run_main(tmp_path, monkeypatch, candidates, cache=None):
    candidates_path = tmp_path / "candidates.json"
    cache_path = tmp_path / "data" / "classify_cache.json"
    cache_path.parent.mkdir(exist_ok=True)
    candidates_path.write_text(json.dumps(candidates), encoding="utf-8")
    if cache is not None:
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
    monkeypatch.setattr(classify, "CANDIDATES_PATH", str(candidates_path))
    monkeypatch.setattr(classify, "CLASSIFY_CACHE_PATH", str(cache_path))
    monkeypatch.setattr(classify, "CLASSIFIED_PATH", str(tmp_path / "classified.json"))
    monkeypatch.setattr(classify, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    classify.main()
    return json.loads((tmp_path / "classified.json").read_text(encoding="utf-8"))


def test_main_finance_title_forced_x_even_when_cached_as_s(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    link = "https://example.com/etf"
    cache = {classify.cache_key(link): {"g": "S", "m": "주제", "s": 1, "r": "울산", "loc": None, "u": 10,
                                        "ind": "에너지", "t": [], "core": 0, "e": "e",
                                        "pub": now.isoformat(), "v": classify.PROMPT_VERSION}}
    result = _run_main(tmp_path, monkeypatch, [{"title": "SK그룹주 ETF의 귀환, AI 확장 기대감", "summary": "",
                                                "link": link, "source": "매체", "published": now.isoformat()}], cache)
    assert result[0]["grade"] == "X" and result[0]["finance_filtered"] is True


def test_main_stale_prompt_version_cache_entry_is_regraded(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    link = "https://example.com/old"
    cache = {classify.cache_key(link): {"g": "S", "m": "주제", "s": 1, "r": "울산", "loc": None, "u": 10,
                                        "ind": "조선", "t": [], "core": 1, "e": "e", "pub": now.isoformat()}}
    # No API key: a version-less (stale) entry must not be reused -> left unclassified.
    result = _run_main(tmp_path, monkeypatch, [{"title": "HD현대중공업 AI 용접로봇 도입", "summary": "",
                                                "link": link, "source": "매체", "published": now.isoformat()}], cache)
    assert result[0]["grade"] is None and result[0]["unclassified"] is True
