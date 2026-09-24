"""
Haiku classification: candidates.json -> classified.json

- Sends title/description/source only (no article body fetched).
- Cache: data/classify_cache.json, keyed by sha256(normalized URL), stores both
  relevant=true and relevant=false results (classification fields + published
  date only - never title/summary/body). Entries older than 48h are pruned
  every run. The cache file itself is NOT committed - CI restores/saves it via
  actions/cache (see .github/workflows/update.yml); a cold start with no cache
  at all still works correctly, just classifies everything fresh.
- Keyword prefilter runs before any Haiku call (see passes_keyword_prefilter).
- Classification goes through the Message Batches API (50% cheaper) in groups
  of up to BATCH_SIZE items per request. If the whole batch job hasn't reached
  "ended" within BATCH_TIMEOUT_SECONDS, it's canceled and whatever groups
  didn't finish are classified through the synchronous API instead.
- A group that fails to parse, or hits max_tokens (truncated JSON), is split
  in half and retried, down to a floor of MIN_SPLIT_SIZE items; a group that
  still fails at the floor is left unclassified (relevant=None) rather than
  aborting the run - it retries whenever it's re-collected in a future run.
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from dateutil import parser as dtparser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from urlnorm import normalize_url
from timewindow import within_window

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDIDATES_PATH = os.path.join(ROOT, "candidates.json")
CLASSIFY_CACHE_PATH = os.path.join(ROOT, "data", "classify_cache.json")
CLASSIFIED_PATH = os.path.join(ROOT, "classified.json")
LOGS_DIR = os.path.join(ROOT, "logs")

MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 80
MIN_SPLIT_SIZE = 10  # floor for the halve-and-retry fallback (requirement 5)
# Gate 2 found 4096 too low for 80-item groups (truncated mid-JSON on ~half of
# them). Haiku 4.5's real cap is 64,000 (checked via client.models.retrieve()
# on 2026-09-24); 16,000 gives a large safety margin for an 80-item batch.
MAX_TOKENS = 16000
CACHE_MAX_HOURS = 48  # the largest collection window (Monday); prune anything older on every run

BATCH_TIMEOUT_SECONDS = 90 * 60  # production default: 90 minutes, then cancel + sync fallback
BATCH_POLL_INTERVAL_SECONDS = 20
BATCH_CANCEL_WAIT_SECONDS = 120

# Claude Haiku 4.5 pricing, per 1M tokens (checked against the claude-api skill's
# cached pricing table on 2026-09-24). Update here if pricing changes.
INPUT_PRICE_PER_M = 1.00
OUTPUT_PRICE_PER_M = 5.00
BATCH_API_DISCOUNT = 0.5  # Message Batches API: 50% off standard token price

# Haiku 4.5's minimum cacheable prompt-caching prefix is 4,096 tokens (see the
# claude-api skill's shared/prompt-caching.md). Measured via count_tokens():
# SYSTEM_PROMPT is under that, so a cache_control marker here would create no
# cache entry (cache_creation_input_tokens: 0 every request, no error, no
# benefit). Deliberately not applying cache_control for that reason;
# re-measure if the prompt grows a lot.
PROMPT_CACHING_MIN_TOKENS_HAIKU_4_5 = 4096

IND_OPTIONS = [
    "에너지", "석유화학", "자동차", "조선", "배터리", "반도체", "철강·기계",
    "물류", "건설", "금융", "의료", "공공", "기타",
]
TECH_OPTIONS = ["피지컬AI", "로봇", "데이터센터", "디지털트윈", "자율제조", "LLM·에이전트", "기타"]
CORE_INDUSTRIES = {"에너지", "석유화학", "자동차", "조선"}

SYSTEM_PROMPT = """당신은 한국어 산업 AI 전환(AX) 뉴스 분류기입니다.
입력은 JSON 배열이며, 각 원소는 {"i": id, "t": 제목, "d": 설명(네이버/RSS 요약), "s": 매체명}입니다.
각 기사를 분류해 JSON 배열만 반환하세요. 설명, 코드블록, 다른 텍스트를 절대 추가하지 마세요.
입력과 정확히 같은 개수만 반환하면 되고 순서는 상관없습니다(각 원소에 입력의 i를 그대로 포함).

출력 스키마 (키 이름을 반드시 그대로 사용):
- 관련 없음(relevant=false)이면 딱 이 두 필드만 반환: {"i": id, "r": 0}
- 관련 있음(relevant=true)이면: {"i": id, "r": 1, "s": section, "g": region, "ax": ax점수,
  "u": ulsan_score, "ind": industry, "tech": [tech, ...], "core": 0또는1}
  - s(section): 1/2/3 중 하나
    1=기업·현장(기업 AX 전략, AI 드라이브, 공장 도입, 자율제조·다크팩토리, 생산성 개선,
       산업 AI 사업화, FDE 현장 투입 실적)
    2=기술·인프라(피지컬 AI, 휴머노이드, 산업용 LLM, 제조 파운데이션 모델, 데이터센터,
       실증센터·테스트베드)
    3=정책·생태계·인재(중앙/지방정부 지원, M.AX, 예산, 규제, 산학연 협력, 시장 동향,
       FDE 교육 프로그램, 해커톤, 대학 교육, 재교육, 채용)
  - g(region): "울산" / "국내" / "해외" 중 하나
  - ax(산업 AX 실질성, 아래 중 하나만 - 근거의 구체성 순서):
    50 = 공장·현장에 AI를 실제로 도입했거나 대규모 AX 투자가 확정된 경우
    40 = 실증·계약·협약이 체결된 경우
    30 = 기술개발·정부사업 발표
    20 = 계획·전망 수준
    10 = 단순 동향·언급 수준
  - u(ulsan_score): 10(울산 현장 AX) / 8(울산 기업·대학·기관의 AX 활동) /
    4(울산 주력산업과 직결된 사례) / 0(울산과 무관) 중 하나
  - ind(업종, 아래 고정 목록에서 가장 가까운 것 1개만 선택):
    에너지, 석유화학, 자동차, 조선, 배터리, 반도체, 철강·기계, 물류, 건설, 금융, 의료,
    공공, 기타
  - tech(기술, 아래 고정 목록에서 최대 2개, 목록에 없는 것은 쓰지 말고 "기타" 사용):
    피지컬AI, 로봇, 데이터센터, 디지털트윈, 자율제조, LLM·에이전트, 기타
  - core: ind가 에너지/석유화학/자동차/조선 중 하나이면서 "공장·현장" 수준(ax=50에
    해당하는 실제 도입 수준)의 AI 적용이면 1, 그 외에는 0

relevant 판정 기준 (r=1, 관련 있음):
핵심 규칙 하나: 업종과 관계없이 항상 같은 기준을 적용합니다 - "구체적인 AI/AX 도입·
투자·예산·실증 내용이 실제로 있는가?" 이 기준은 제조업이든 금융·보안·의료·공공행정
이든 동일합니다. 업종이 금융/보안/의료/공공이라는 이유만으로 기준을 낮추거나 높이지
마세요. 그 업종이라는 이유만으로는 관련 없음이고, 구체적 도입 내용이 있어야 관련
있음입니다.

포함(r=1) 예 - 아래 모두 "구체적 도입 내용"이 실제로 기사에 있는 경우만:
- 특정 기업·기관·현장의 AI 도입·실증·AX 전략 (업종 무관: 제조·에너지·조선·물류·
  건설·금융·보안·의료·공공행정 등 - 예: 은행의 AI 데이터센터 투자 발표, 병원의 AI
  진단시스템 도입, 정부기관의 AI 실증사업 착수)
- AI 데이터센터 건립·투자
- 정부의 AX 예산·지원사업·실증거점 조성 (구체적 예산액·대상 산업/지역이 명시된 경우)
- 산업 적용이 구체적으로 명시된 국가 AI 정책 (예: "OO산업단지를 AX 실증거점으로 조성",
  "제조 공정에 AI 적용 예산 편성" 등 - 대상 산업/현장이 특정되어야 함)

제외(r=0) 예 - 아래에 해당하면 AI라는 단어가 나와도 제외:
- 선언적 발언·축사·구호성 언급뿐인 기사(정치인·경영진의 연설/인터뷰에서 "AI 시대",
  "AI 기본사회", "AI 경쟁력" 같은 표현만 스치듯 나오고 구체적 도입·투자·예산·실증
  내용이 없는 경우 - 대통령 외교 연설, 기업 축사 등)
- 빅테크 실적·주가, 반도체 시황, AI 모델 경쟁 담론(단, 산업 현장 적용 사례가 기사에
  명시된 경우만 포함)
- 일반 소비자용 AI 서비스·앱 출시(기업/기관 대상 산업 도입이 아닌 일반 소비자 대상.
  예: 어르신 돌봄 AI 챗봇, 개인용 AI 비서 앱)
- AI와 무관한 산업 뉴스(원전·조선·배터리 등 산업 시설 신설이라도 AI/AX 요소가
  기사에 없으면 제외 - 산업이라고 해서 자동으로 포함되지 않음)
- AI가 아닌 기술(양자컴퓨터, 일반 IT 인프라·클라우드 전환 등)을 AI와 나란히
  언급했을 뿐 AI 자체의 구체적 도입 내용은 없는 경우
"""


# Keyword prefilter: a candidate that mentions none of these (case-insensitive for the
# Latin-script ones) is treated as not relevant without spending a Haiku call on it.
# Purely a cost cut - a genuine AX story that happens to avoid every one of these terms
# would be missed, which is exactly what the "filtered-out sample" audit in Gate 1 checks for.
PREFILTER_KEYWORDS = [
    "AI", "인공지능", "에이아이", "AX", "로봇", "자율", "스마트공장", "스마트팩토리",
    "디지털 트윈", "디지털트윈", "데이터센터", "자동화", "휴머노이드", "LLM", "에이전트",
    "FDE", "해커톤",
]


def passes_keyword_prefilter(candidate: dict) -> bool:
    # Collapse whitespace differences ("디지털 트윈" vs "디지털트윈") before matching.
    text = re.sub(r"\s+", "", f"{candidate.get('title', '')} {candidate.get('summary', '')}").upper()
    return any(re.sub(r"\s+", "", kw).upper() in text for kw in PREFILTER_KEYWORDS)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def cache_key(link: str) -> str:
    return hashlib.sha256(normalize_url(link).encode("utf-8")).hexdigest()


def load_and_prune_cache() -> dict:
    """Load data/classify_cache.json (absent is fine - cold start) and drop
    entries whose published date has aged out of the largest (48h) window."""
    cache = load_json(CLASSIFY_CACHE_PATH, {})
    now = datetime.now(timezone.utc)
    pruned = {}
    for key, entry in cache.items():
        pub = entry.get("pub")
        try:
            pub_dt = dtparser.parse(pub) if pub else None
        except (ValueError, TypeError, OverflowError):
            pub_dt = None
        if within_window(pub_dt, CACHE_MAX_HOURS, now_utc=now):
            pruned[key] = entry
    return pruned


def save_cache(cache: dict):
    os.makedirs(os.path.dirname(CLASSIFY_CACHE_PATH), exist_ok=True)
    with open(CLASSIFY_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))


def _sanitize_ind(value) -> str:
    return value if value in IND_OPTIONS else "기타"


def _sanitize_tech(value) -> list:
    if not isinstance(value, list):
        return []
    return [t for t in value if t in TECH_OPTIONS][:2]


def _sanitize_core(core, ind: str, section) -> int:
    return 1 if (core and ind in CORE_INDUSTRIES and section == 1) else 0


def cache_entry_to_fields(entry: dict) -> dict:
    if not entry.get("r"):
        return {"relevant": False, "section": None, "region": None, "ax": 0, "ulsan_score": 0,
                "industry": None, "tech": [], "core": 0}
    ind = _sanitize_ind(entry.get("ind"))
    section = entry.get("s")
    return {
        "relevant": True,
        "section": section,
        "region": entry.get("g"),
        "ax": entry.get("ax", 10),
        "ulsan_score": entry.get("u", 0),
        "industry": ind,
        "tech": _sanitize_tech(entry.get("t")),
        "core": _sanitize_core(entry.get("core"), ind, section),
    }


def fields_to_cache_entry(fields: dict, published: str) -> dict:
    entry = {"r": 1 if fields.get("relevant") else 0, "pub": published}
    if fields.get("relevant"):
        entry.update({
            "s": fields.get("section"), "g": fields.get("region"),
            "ax": fields.get("ax", 10), "u": fields.get("ulsan_score", 0),
            "ind": fields.get("industry"), "t": fields.get("tech", []),
            "core": fields.get("core", 0),
        })
    return entry


def build_batch_input(items):
    return [{"i": it["id"], "t": it["title"], "d": it.get("summary", ""), "s": it.get("source", "")} for it in items]


def expand_compact_result(p: dict) -> dict:
    if not p.get("r"):
        return {"relevant": False, "section": None, "region": None, "ax": 0, "ulsan_score": 0,
                "industry": None, "tech": [], "core": 0}
    ind = _sanitize_ind(p.get("ind"))
    section = p.get("s")
    return {
        "relevant": True,
        "section": section,
        "region": p.get("g"),
        "ax": p.get("ax", 10),
        "ulsan_score": p.get("u", 0),
        "industry": ind,
        "tech": _sanitize_tech(p.get("tech")),
        "core": _sanitize_core(p.get("core"), ind, section),
    }


def parse_json_array(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text)


def chunk(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def _classify_one_group_sync(client, group, log):
    """Classify a single group; on parse failure or a truncated (max_tokens)
    response, halve the group and retry recursively down to MIN_SPLIT_SIZE.
    A group that still fails at the floor is marked unclassified."""
    batch_input = build_batch_input(group)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(batch_input, ensure_ascii=False, separators=(",", ":"))}],
        )
        log["sync_input_tokens"] = log.get("sync_input_tokens", 0) + response.usage.input_tokens
        log["sync_output_tokens"] = log.get("sync_output_tokens", 0) + response.usage.output_tokens

        if response.stop_reason == "max_tokens":
            raise ValueError(f"response truncated at max_tokens ({MAX_TOKENS})")

        text = "".join(b.text for b in response.content if b.type == "text")
        parsed = parse_json_array(text)
        by_id = {p.get("i"): p for p in parsed if isinstance(p, dict)}
        results = {}
        for it in group:
            p = by_id.get(it["id"])
            results[it["id"]] = expand_compact_result(p) if p else {"relevant": None, "unclassified": True}
        return results
    except Exception as e:  # noqa: BLE001 - a bad group must not stop the run
        if len(group) > MIN_SPLIT_SIZE:
            mid = len(group) // 2
            log.setdefault("split_retries", []).append({"original_size": len(group), "error": str(e)})
            results = _classify_one_group_sync(client, group[:mid], log)
            results.update(_classify_one_group_sync(client, group[mid:], log))
            return results
        log["failed_batches"].append({"group_size": len(group), "error": str(e), "item_ids": [it["id"] for it in group]})
        return {it["id"]: {"relevant": None, "unclassified": True} for it in group}


def classify_groups_sync(client, groups, log):
    """Classify a list of item-groups through the synchronous Messages API."""
    results = {}
    for group in groups:
        results.update(_classify_one_group_sync(client, group, log))
    return results


def classify_via_batches_api(client, items, log, timeout_seconds=BATCH_TIMEOUT_SECONDS,
                              poll_interval=BATCH_POLL_INTERVAL_SECONDS,
                              cancel_wait_seconds=BATCH_CANCEL_WAIT_SECONDS):
    """Classify items via the Message Batches API (50% cheaper). Any request that
    hasn't finished within timeout_seconds is canceled; whatever didn't complete
    falls back to the synchronous API (with the halve-and-retry fallback).
    Returns id -> classification fields."""
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request as BatchRequest

    groups = chunk(items, BATCH_SIZE)
    requests = [
        BatchRequest(
            custom_id=f"g{gi}",
            params=MessageCreateParamsNonStreaming(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(build_batch_input(group), ensure_ascii=False, separators=(",", ":"))}],
            ),
        )
        for gi, group in enumerate(groups)
    ]

    message_batch = client.messages.batches.create(requests=requests)
    log["batches_api_id"] = message_batch.id
    log["batches_api_group_count"] = len(groups)

    start = time.monotonic()
    status = message_batch.processing_status
    while status != "ended" and (time.monotonic() - start) < timeout_seconds:
        time.sleep(poll_interval)
        message_batch = client.messages.batches.retrieve(message_batch.id)
        status = message_batch.processing_status

    timed_out = status != "ended"
    log["batches_api_timed_out"] = timed_out
    if timed_out:
        client.messages.batches.cancel(message_batch.id)
        cancel_deadline = time.monotonic() + cancel_wait_seconds
        while message_batch.processing_status != "ended" and time.monotonic() < cancel_deadline:
            time.sleep(min(5, poll_interval))
            message_batch = client.messages.batches.retrieve(message_batch.id)

    results_by_custom_id = {r.custom_id: r for r in client.messages.batches.results(message_batch.id)}

    id_to_result = {}
    fallback_groups = []
    total_in = total_out = 0
    for gi, group in enumerate(groups):
        r = results_by_custom_id.get(f"g{gi}")
        if r is not None and r.result.type == "succeeded":
            msg = r.result.message
            total_in += msg.usage.input_tokens
            total_out += msg.usage.output_tokens
            if msg.stop_reason == "max_tokens":
                fallback_groups.append(group)
                continue
            try:
                parsed = parse_json_array("".join(b.text for b in msg.content if b.type == "text"))
                by_id = {p.get("i"): p for p in parsed if isinstance(p, dict)}
                for it in group:
                    p = by_id.get(it["id"])
                    id_to_result[it["id"]] = expand_compact_result(p) if p else {"relevant": None, "unclassified": True}
            except Exception:  # noqa: BLE001
                fallback_groups.append(group)
        else:
            fallback_groups.append(group)

    log["batches_api_input_tokens"] = total_in
    log["batches_api_output_tokens"] = total_out
    log["batches_api_fallback_group_count"] = len(fallback_groups)
    log["batches_api_fallback_item_count"] = sum(len(g) for g in fallback_groups)

    if fallback_groups:
        id_to_result.update(classify_groups_sync(client, fallback_groups, log))

    return id_to_result


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    candidates = load_json(CANDIDATES_PATH, [])
    for idx, c in enumerate(candidates):
        c["id"] = idx

    cache = load_and_prune_cache()

    log = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total_candidates": len(candidates),
        "cache_hits": 0,
        "to_classify": 0,
        "failed_batches": [],
    }

    classified = []
    to_classify = []
    for c in candidates:
        entry = cache.get(cache_key(c.get("link", "")))
        if entry:
            log["cache_hits"] += 1
            classified.append({**c, **cache_entry_to_fields(entry)})
        else:
            to_classify.append(c)

    keyword_pass = [c for c in to_classify if passes_keyword_prefilter(c)]
    keyword_filtered_out = [c for c in to_classify if not passes_keyword_prefilter(c)]
    for c in keyword_filtered_out:
        classified.append({**c, "relevant": False, "section": None, "region": None,
                            "ax": 0, "ulsan_score": 0, "industry": None, "tech": [], "core": 0,
                            "prefiltered": True})

    log["to_classify"] = len(to_classify)
    log["keyword_prefilter_pass"] = len(keyword_pass)
    log["keyword_prefilter_filtered_out"] = len(keyword_filtered_out)
    print(f"Candidates: {len(candidates)} total, {log['cache_hits']} cache hits, {len(to_classify)} to classify "
          f"({len(keyword_pass)} pass keyword prefilter, {len(keyword_filtered_out)} filtered out)")

    new_results = {}
    if keyword_pass:
        if not api_key:
            print(f"ANTHROPIC_API_KEY not set - {len(keyword_pass)} candidates left unclassified (will retry next run)")
            log["skipped_no_api_key"] = True
            for c in keyword_pass:
                classified.append({**c, "relevant": None, "unclassified": True})
        else:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            new_results = classify_via_batches_api(client, keyword_pass, log)
            for c in keyword_pass:
                r = new_results.get(c["id"], {"relevant": None, "unclassified": True})
                classified.append({**c, **r})

    # Persist freshly-classified (non-cached, non-prefiltered, non-unclassified) results to the cache.
    for c in keyword_pass:
        r = new_results.get(c["id"])
        if r and not r.get("unclassified"):
            cache[cache_key(c.get("link", ""))] = fields_to_cache_entry(r, c.get("published"))
    save_cache(cache)

    with open(CLASSIFIED_PATH, "w", encoding="utf-8") as f:
        json.dump(classified, f, ensure_ascii=False, indent=2)

    total_in = log.get("batches_api_input_tokens", 0) + log.get("sync_input_tokens", 0)
    total_out = log.get("batches_api_output_tokens", 0) + log.get("sync_output_tokens", 0)
    # Cost: tokens routed through the Batches API bill at 50%; sync fallback bills at full price.
    batches_in = log.get("batches_api_input_tokens", 0)
    batches_out = log.get("batches_api_output_tokens", 0)
    sync_in = log.get("sync_input_tokens", 0)
    sync_out = log.get("sync_output_tokens", 0)
    est_cost = (
        batches_in / 1_000_000 * INPUT_PRICE_PER_M * BATCH_API_DISCOUNT
        + batches_out / 1_000_000 * OUTPUT_PRICE_PER_M * BATCH_API_DISCOUNT
        + sync_in / 1_000_000 * INPUT_PRICE_PER_M
        + sync_out / 1_000_000 * OUTPUT_PRICE_PER_M
    )
    log["input_tokens"] = total_in
    log["output_tokens"] = total_out
    log["estimated_cost_usd"] = round(est_cost, 4)

    os.makedirs(LOGS_DIR, exist_ok=True)
    log["finished_at"] = datetime.now(timezone.utc).isoformat()
    log_path = os.path.join(LOGS_DIR, f"classify_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.json")
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    print(f"Classified {len(classified)} total. Tokens in={total_in} out={total_out} "
          f"est.cost=${log['estimated_cost_usd']}")


if __name__ == "__main__":
    main()
