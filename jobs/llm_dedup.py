"""
Second-pass event dedup (post algorithmic-merge, pre-cap).

dedup.py's algorithmic pass (exact-title match, then org+number/quote/policy
gated Jaccard similarity) runs first and catches most duplicates for free.
This module takes whatever is left - restricted to groups that could still
end up published (S or A grade somewhere in the group; see build.py's
_group_has_publishable_grade) - and asks Haiku to partition their FULL TITLES
into same-event groups. It no longer restricts itself to items sharing a
detected organization name: the whole day's publishable candidate set is sent,
split into chunks of at most MAX_CHUNK_SIZE items so one call's context never
has to hold more than that. Different stages of the same story
(발표/선정/착공/실증/성과 등) must never be merged - the prompt says so
explicitly, and "애매하면 묶지 말라"는 지시로 과병합보다 과소병합을 선호한다.

Anti-chain-merge guard: each candidate item belongs to exactly one chunk (the
chunking is a straight partition of the candidate list), so no item can be
placed in two different chunks and chain unrelated groups together across
calls. Within a single chunk's response, an id is honored only the first time
it appears across the response's groups - a defensive guard against a
malformed/duplicated response, mirroring the anti-chain protection the old
org-cluster version needed (that one could put the same article in several
clusters; this version can't structurally, but the guard costs nothing and
catches a hallucinated response that repeats an id).

Only call this on the deduped, still-graded (S/A/B/C/X) set - never before
dedup.py's algorithmic pass has already run.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dedup import _choose_better
from classify import INPUT_PRICE_PER_M, OUTPUT_PRICE_PER_M

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 8000
MAX_CHUNK_SIZE = 200  # requirement 3: split candidates into chunks of this size, one Haiku call per chunk

SYSTEM_PROMPT = """당신은 한국어 뉴스 동일 사건 판별기입니다.
입력은 JSON 배열이며, 각 원소는 {"i": id, "t": 제목, "s": 매체명}입니다. 이 기사
전체를 훑어, 진짜 같은 사건(같은 발표·같은 계약·같은 투자 건)을 다루는 기사들만 하나의
그룹으로 묶으세요. 발표·선정·착공·실증·성과처럼 사건의 단계가 다르면 절대 같은 그룹으로
묶지 마세요. 애매하면 묶지 말고 각자 별도 그룹으로 두세요.

출력은 JSON 배열만 반환하세요: [[id, id], [id], ...] - 입력에 있는 모든 id를 정확히
한 번씩만 포함하는 부분집합들이어야 합니다. 설명, 코드블록, 다른 텍스트를 절대 추가하지
마세요.
"""


def _chunk(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def _parse_json_array(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text)


def _merge_one_chunk(client, chunk_items: list, log: dict) -> dict:
    """Ask Haiku to partition one chunk (<= MAX_CHUNK_SIZE items) by full
    title. Returns {absorbed_link: representative_link} for this chunk only."""
    id_to_item = dict(enumerate(chunk_items))
    payload = [{"i": i, "t": it.get("title", ""), "s": it.get("source", "")} for i, it in id_to_item.items()]

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}],
        )
        log["llm_dedup_input_tokens"] = log.get("llm_dedup_input_tokens", 0) + response.usage.input_tokens
        log["llm_dedup_output_tokens"] = log.get("llm_dedup_output_tokens", 0) + response.usage.output_tokens
        text = "".join(b.text for b in response.content if b.type == "text")
        groups = _parse_json_array(text)
    except Exception as e:  # noqa: BLE001 - this pass failing must not break the run
        log.setdefault("llm_dedup_errors", []).append(str(e))
        return {}

    merge_map = {}
    assigned = set()
    for group in groups if isinstance(groups, list) else []:
        ids = group if isinstance(group, list) else []
        # An id already assigned from an earlier group in this same response is
        # skipped here (defensive guard against a malformed/duplicated response
        # - see module docstring).
        group_items = [id_to_item[i] for i in ids if i in id_to_item and i not in assigned]
        assigned.update(i for i in ids if i in id_to_item)
        if len(group_items) < 2:
            continue
        rep = group_items[0]
        for other in group_items[1:]:
            rep = _choose_better(rep, other)
        for it in group_items:
            if it is not rep:
                merge_map[it.get("link", "")] = rep.get("link", "")
    return merge_map


def llm_merge_candidates(client, items: list, log: dict) -> dict:
    """items: top-level (post algo-dedup) groups that carry a publishable
    (S/A) grade somewhere in the group (build.py filters this before calling).
    Splits into MAX_CHUNK_SIZE-item chunks and merges each chunk independently
    - no cross-chunk merging is possible since each item belongs to exactly
    one chunk. Returns a flat {absorbed_link: representative_link} merge map
    across every chunk.

    Always leaves log["chunk_count"]/["llm_dedup_input_tokens"]/
    ["llm_dedup_output_tokens"]/["estimated_cost_usd"] set (0 if this pass
    never actually called the API) - build.py pre-seeds the same defaults
    for the case this function isn't called at all (no candidates, or no
    ANTHROPIC_API_KEY), so a run's data.json always reports this pass's true
    cost instead of silently omitting the field."""
    log.setdefault("chunk_count", 0)
    log.setdefault("llm_dedup_input_tokens", 0)
    log.setdefault("llm_dedup_output_tokens", 0)
    if not items:
        log["estimated_cost_usd"] = 0.0
        return {}
    chunks = _chunk(items, MAX_CHUNK_SIZE)
    log["chunk_count"] = len(chunks)
    merge_map = {}
    for c in chunks:
        merge_map.update(_merge_one_chunk(client, c, log))
    # Synchronous Messages API only (no Message Batches API here - see module
    # docstring) - full price, no batch discount.
    log["estimated_cost_usd"] = round(
        log["llm_dedup_input_tokens"] / 1_000_000 * INPUT_PRICE_PER_M
        + log["llm_dedup_output_tokens"] / 1_000_000 * OUTPUT_PRICE_PER_M,
        4,
    )
    return merge_map


def apply_merge_map(items: list, merge_map: dict) -> list:
    """Fold absorbed items into their representative's `related` list and
    drop them from the top-level result."""
    by_link = {it.get("link", ""): it for it in items}
    absorbed_into = {}
    for absorbed_link, rep_link in merge_map.items():
        # Follow chains (A absorbed into B, B absorbed into C) to the final root.
        seen = {absorbed_link}
        root = rep_link
        while root in merge_map and root not in seen:
            seen.add(root)
            root = merge_map[root]
        absorbed_into.setdefault(root, []).append(absorbed_link)

    result = []
    for it in items:
        link = it.get("link", "")
        if link in merge_map:
            continue  # folded into another representative below
        merged = dict(it)
        # Keep the full absorbed item (not just source/link) - it may itself
        # carry a `related` list from dedup.py's earlier pass, and a later
        # grade-aware re-representation pass (build.py's regrade_representatives)
        # needs to see every absorbed candidate's own grade, nested or not.
        extra_related = [dict(by_link[l]) for l in absorbed_into.get(link, []) if l in by_link]
        if extra_related:
            merged["related"] = list(merged.get("related", [])) + extra_related
        result.append(merged)
    return result
