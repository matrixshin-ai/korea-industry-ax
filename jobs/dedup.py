"""
Enhanced article deduplication with strong signal gate conditions.

A. Applied after RSS collection, before scoring/quota
B. Representative selection: domain priority > score > published date
C. Stage 1: Exact normalized title dedupe
D. Stage 2: Similar article removal with gate conditions (org + number/quote/policy)
E. Bucketing for O(n) performance, no O(n²) comparison
"""
import re
import unicodedata
import json
import os
from urllib.parse import urlparse
from typing import List, Dict, Any, Set, Tuple, Optional

PREFERRED_DOMAINS = [
    "mk.co.kr",          # 매일경제
    "asiae.co.kr",       # 아시아경제
    "hankyung.com",      # 한국경제
    "yonhapnews.co.kr",  # 연합뉴스
    "kbs.co.kr",         # KBS
    "naver.com",         # 네이버 (포털/재전송)
]

TAIL_TAGS = [
    "종합", "속보", "단독", "인터뷰", "분석", "해설", "기획", "팩트체크",
    "영상", "포토", "오피니언", "사설", "칼럼", "1보", "2보", "3보"
]

STOPWORDS = {
    "올해", "전망", "한국", "경제", "시장", "관련", "기자", "종합", "속보",
    "뉴스", "보도", "발표", "예정", "결과", "분석", "기획", "특집", "단독",
    "최근", "현재", "오늘", "내년", "작년", "국내", "해외", "글로벌",
    "정부", "당국", "대책", "방안", "계획", "추진", "검토", "논의",
    "지난해", "이번", "연합뉴스", "뉴시스", "머니투데이", "파이낸셜뉴스",
    "인사이트", "조선비즈", "한경닷컴", "연합인포맥스", "아시아경제",
}

ORGANIZATIONS = {
    "한국은행", "기재부", "기획재정부", "금융위", "금융위원회", "금감원", "금융감독원",
    "KDI", "한국개발연구원", "OECD", "IMF", "연준", "Fed", "ECB", "BOJ",
    "통계청", "국세청", "관세청", "산업부", "산업통상자원부", "국토부", "국토교통부",
    "삼성전자", "SK하이닉스", "현대차", "LG전자", "포스코", "네이버", "카카오",
    # Bare/group-level conglomerate names, on top of the specific subsidiaries
    # above - "삼성 6개사, ... 헬릭스에 10억달러 투자"류 제목은 "삼성전자" 같은
    # 구체적 계열사명이 아니라 그룹명만 쓰는 경우가 많아 이 목록이 없으면
    # _check_gate_conditions의 org-match 자체가 성립하지 않았음(2026-09-29,
    # 같은 사건 기사 22건이 전혀 비교되지 않고 개별 카드로 남았던 원인).
    "삼성", "삼성그룹", "현대그룹", "현대차그룹", "한화그룹", "두산그룹", "롯데그룹",
    "신세계그룹", "코오롱그룹", "코오롱", "효성", "한진그룹", "포스코그룹",
    "SK그룹", "LG그룹", "GS그룹", "CJ그룹", "LS그룹",
    "한경연", "전경련", "대한상의", "중기중앙회", "무역협회", "은행연합회",
    "신한은행", "국민은행", "하나은행", "우리은행", "농협은행", "기업은행",
    "신한금융", "국민금융", "하나금융", "우리금융", "농협금융", "KB금융",
    "HD현대", "HD현대중공업", "HD현대일렉트릭", "두산에너빌리티", "SK이노베이션",
    "LG에너지솔루션", "현대로템", "현대중공업",
    "대통령", "대통령실", "국무총리", "울산시", "울산시청", "UNIST",
    "한국경제신문", "매일경제", "서울경제", "파이낸셜뉴스", "한국금융신문",
    "증권사", "자산운용사", "보험사", "저축은행", "카드사", "캐피탈",
    "경제학자", "경제학과", "교수", "전문가", "애널리스트", "연구원",
    "S&P", "무디스", "피치", "블룸버그", "로이터", "월스트리트저널",
}


def _load_section_keywords() -> Set[str]:
    """Load ALL section keywords from keyword_rules.json to exclude from similarity."""
    keywords = set()
    try:
        rules_path = os.environ.get("KEYWORD_RULES_JSON", "keyword_rules.json")
        with open(rules_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for section, rules in data.get("sections", {}).items():
            for kw_item in rules.get("keywords", []):
                if isinstance(kw_item, dict):
                    kw = kw_item.get("keyword", "").lower()
                    if kw:
                        keywords.add(kw)
                elif isinstance(kw_item, str):
                    keywords.add(kw_item.lower())
    except Exception:
        pass
    keywords.update([
        "gdp", "성장률", "금리", "환율", "반도체", "집값", "연준",
        "기준금리", "물가", "인플레이션", "수출", "수입", "무역수지",
        "부동산", "아파트", "주택", "분양", "전세", "월세",
        "주가", "코스피", "코스닥", "증시", "채권", "국채",
    ])
    return keywords

SECTION_KEYWORDS = _load_section_keywords()


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _is_naver(url: str) -> bool:
    return "naver.com" in _domain(url)


def _pref_rank(url: str) -> int:
    d = _domain(url)
    for i, dom in enumerate(PREFERRED_DOMAINS):
        if d.endswith(dom):
            return i
    return len(PREFERRED_DOMAINS) + 1


def normalize_text(text: str) -> str:
    """Normalize title: remove tags, standardize punctuation, lowercase."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"^\s*(HOT|속보|단독|Exclusive|긴급|특종|뉴스)\s*[:\-]?\s*", "", t, flags=re.IGNORECASE)
    t = t.replace("…", "...").replace(""", '"').replace(""", '"').replace("'", "'").replace("'", "'")
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"[\[\(（【].*?[\]\)）】]\s*$", "", t).strip()
    for tag in TAIL_TAGS:
        t = re.sub(rf"\s*['\"]?\(?{tag}\)?['\"]?\s*$", "", t, flags=re.IGNORECASE).strip()
    return t.lower()


def _get_content(item: dict) -> str:
    return (
        item.get("snippet", "") or 
        item.get("description", "") or 
        item.get("summary", "") or 
        ""
    )


def _get_combined_text(item: dict) -> str:
    title = item.get("title", "")
    content = _get_content(item)
    return f"{title} {content}".strip()


def _choose_better(a: dict, b: dict) -> dict:
    """Choose representative: domain priority > score > published date (newest)."""
    a_rank = _pref_rank(a.get("link", ""))
    b_rank = _pref_rank(b.get("link", ""))
    if a_rank != b_rank:
        return a if a_rank < b_rank else b
    a_score = a.get("score", 0)
    b_score = b.get("score", 0)
    if a_score != b_score:
        return a if a_score > b_score else b
    a_pub = a.get("published", "")
    b_pub = b.get("published", "")
    return b if b_pub > a_pub else a


def _extract_non_date_numbers(text: str) -> Set[str]:
    """
    Extract non-date numeric values: percentages, rates, amounts.
    Excludes: YYYY-MM-DD, "2026년 1월 25일", "오전 9시" date/time patterns.
    """
    text_clean = text
    text_clean = re.sub(r'\d{4}[-/년]\d{1,2}[-/월]\d{1,2}일?', '', text_clean)
    text_clean = re.sub(r'\d{4}년\s*\d{1,2}월\s*\d{1,2}일?', '', text_clean)
    text_clean = re.sub(r'\d{1,2}월\s*\d{1,2}일', '', text_clean)
    text_clean = re.sub(r'(오전|오후|새벽|밤|저녁|아침)?\s*\d{1,2}시\s*(\d{1,2}분)?', '', text_clean)
    text_clean = re.sub(r'\d{4}[-/]\d{2}[-/]\d{2}', '', text_clean)
    
    numbers = set()
    patterns = [
        (r'(\d+\.?\d*)%', 'pct'),
        (r'(\d+\.?\d*)\s*%대', 'pct'),
        (r'(\d{3,4})원대?', 'won'),
        (r'(\d+\.?\d*)조원?', 'tril'),
        (r'(\d+\.?\d*)억원?', 'bil'),
        (r'(\d+\.?\d*)만원?', 'man'),
        (r'(\d+\.?\d*)달러', 'usd'),
        (r'(\d+\.?\d*)포인트', 'pt'),
        (r'(\d+\.?\d*)\s*bp', 'bp'),
        (r'(\d+\.?\d*)\s*p(?![a-z])', 'pct'),
    ]
    for pattern, tag in patterns:
        matches = re.findall(pattern, text_clean, re.IGNORECASE)
        for m in matches:
            num = m.strip()
            if num:
                numbers.add(f"{num}:{tag}")
    
    return numbers


def _extract_organizations(text: str) -> Set[str]:
    """Extract organization names from text."""
    found = set()
    text_lower = text.lower()
    for org in ORGANIZATIONS:
        if org.lower() in text_lower:
            found.add(org.lower())
    return found


def _extract_quoted_phrases(text: str, min_len: int = 8, max_len: int = 30) -> Set[str]:
    """Extract quoted phrases (potential unique statements/quotes)."""
    phrases = set()
    quote_patterns = [
        r'"([^"]{8,30})"',
        r"'([^']{8,30})'",
        r'\u201c([^\u201d]{8,30})\u201d',
        r'\u2018([^\u2019]{8,30})\u2019',
    ]
    for pattern in quote_patterns:
        matches = re.findall(pattern, text)
        for m in matches:
            clean = m.strip()
            if len(clean) >= min_len:
                phrases.add(clean[:max_len].lower())
    return phrases


def _extract_policy_names(text: str) -> Set[str]:
    """Extract policy/program names (제도, 정책, 사업, 프로그램, etc.)."""
    policies = set()
    patterns = [
        r'([가-힣]{2,10}(제도|정책|법안|법|사업|프로그램|플랜|대책|특례|지원금|보조금))',
        r'([가-힣]{2,8}(대출|예금|채권|펀드|보험))',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for m in matches:
            if isinstance(m, tuple):
                policies.add(m[0].lower())
            else:
                policies.add(m.lower())
    return policies


def _check_gate_conditions(item_a: dict, item_b: dict) -> Tuple[bool, str]:
    """
    Check strong signal gate conditions (D-1).
    Returns (passed, reason) tuple.
    
    Gate requires: organization match + one of (number/quote/policy) match.
    """
    text_a = _get_combined_text(item_a)
    text_b = _get_combined_text(item_b)
    
    orgs_a = _extract_organizations(text_a)
    orgs_b = _extract_organizations(text_b)
    orgs_common = orgs_a & orgs_b
    
    if not orgs_common:
        return False, "no_org_match"
    
    nums_a = _extract_non_date_numbers(text_a)
    nums_b = _extract_non_date_numbers(text_b)
    if nums_a & nums_b:
        return True, "org+number"
    
    quotes_a = _extract_quoted_phrases(text_a)
    quotes_b = _extract_quoted_phrases(text_b)
    if quotes_a & quotes_b:
        return True, "org+quote"
    
    policies_a = _extract_policy_names(text_a)
    policies_b = _extract_policy_names(text_b)
    if policies_a & policies_b:
        return True, "org+policy"
    
    return False, "org_only"


def _tokenize_for_similarity(text: str) -> List[str]:
    """Tokenize text, excluding section keywords and stopwords."""
    text_lower = text.lower()
    toks = re.findall(r"[0-9a-zA-Z가-힣]+", text_lower)
    filtered = []
    for t in toks:
        if len(t) < 2:
            continue
        if t in STOPWORDS:
            continue
        if t in SECTION_KEYWORDS:
            continue
        t = re.sub(r'^(\d+\.?\d*)(원|까지|대|약|포인트|bp|달러|이상|미만|수준|선|내외)+$', r'\1', t)
        if t:
            filtered.append(t)
    return filtered


def _jaccard(a_tokens: List[str], b_tokens: List[str]) -> float:
    a, b = set(a_tokens), set(b_tokens)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _create_bucket_key(item: dict) -> str:
    """
    Create bucket key: normalized title prefix + representative number + representative org.
    Articles in the same bucket will be compared for similarity.
    """
    text = _get_combined_text(item)
    title_norm = normalize_text(item.get("title", ""))
    title_prefix = "".join(re.findall(r"[가-힣a-zA-Z]", title_norm)[:6])
    
    nums = sorted(_extract_non_date_numbers(text))
    orgs = sorted(_extract_organizations(text))
    
    num_part = nums[0] if nums else ""
    org_part = orgs[0] if orgs else ""
    
    return f"{title_prefix}|{num_part}|{org_part}"


def _create_multi_bucket_keys(item: dict) -> List[str]:
    """Create multiple bucket keys for cross-bucket matching."""
    text = _get_combined_text(item)
    title_norm = normalize_text(item.get("title", ""))
    title_prefix = "".join(re.findall(r"[가-힣a-zA-Z]", title_norm)[:5])
    
    nums = sorted(_extract_non_date_numbers(text))[:3]
    orgs = sorted(_extract_organizations(text))[:2]
    
    keys = []
    for num in nums:
        for org in orgs:
            keys.append(f"{num}|{org}")
    
    if not keys and (nums or orgs):
        for num in nums:
            keys.append(f"num:{num}")
        for org in orgs:
            keys.append(f"org:{org}")
    
    if not keys:
        keys.append(f"title:{title_prefix}")
    
    return keys


def _title_similarity(a: dict, b: dict) -> float:
    """Calculate similarity based on titles only."""
    title_a = a.get("title", "")
    title_b = b.get("title", "")
    toks_a = _tokenize_for_similarity(title_a)
    toks_b = _tokenize_for_similarity(title_b)
    return _jaccard(toks_a, toks_b)


def deduplicate_articles(
    items: List[Dict[str, Any]], 
    base_threshold: float = 0.30,
    portal_threshold: float = 0.20,
    title_threshold: float = 0.20
) -> List[Dict[str, Any]]:
    """
    Enhanced deduplication with strong signal gate conditions.
    
    Args:
        items: List of article dicts with title, link, score, published, summary/description
        base_threshold: Jaccard threshold for general comparison (default 0.35)
        portal_threshold: Relaxed threshold for portal retransmissions (default 0.25)
        title_threshold: Title similarity threshold as backup (default 0.30)
    
    Returns:
        Deduplicated list of articles
    
    Process:
        1) Stage 1: Exact normalized title match → keep 1
        2) Stage 2: Gate condition check → Jaccard similarity OR title similarity → merge
    """
    if not items:
        return items
    
    before_count = len(items)

    exact: Dict[str, dict] = {}
    for it in items:
        key = normalize_text(it.get("title", ""))
        if not key:
            continue
        if key not in exact:
            exact[key] = it
        else:
            exact[key] = _choose_better(exact[key], it)

    stage1 = list(exact.values())
    stage1_count = len(stage1)

    buckets: Dict[str, List[Tuple[int, dict, List[str]]]] = {}
    
    for idx, it in enumerate(stage1):
        bucket_keys = _create_multi_bucket_keys(it)
        combined = _get_combined_text(it)
        toks = _tokenize_for_similarity(combined)
        for bk in bucket_keys:
            buckets.setdefault(bk, []).append((idx, it, toks))

    merged_into: Dict[int, int] = {}
    representatives: Dict[int, dict] = {i: it for i, it in enumerate(stage1)}
    related_map: Dict[int, List[dict]] = {i: [] for i in range(len(stage1))}

    def _as_related(item: dict) -> dict:
        # Keep the full item (not just source/link) so a later grade-aware
        # re-representation pass (see build.py's regrade_representatives) can
        # still see every absorbed candidate's own grade. Reduced to
        # {source, link} only when the final published JSON is written.
        return dict(item)

    for bucket_key, group in buckets.items():
        kept: List[Tuple[int, dict, List[str]]] = []

        for idx, it, toks in group:
            if idx in merged_into:
                continue

            merged = False
            for kept_idx, kept_it, kept_toks in kept:
                if kept_idx in merged_into:
                    continue

                passed, reason = _check_gate_conditions(it, kept_it)
                if not passed:
                    continue

                is_portal_pair = _is_naver(it.get("link", "")) or _is_naver(kept_it.get("link", ""))
                threshold = portal_threshold if is_portal_pair else base_threshold

                sim = _jaccard(toks, kept_toks)
                title_sim = _title_similarity(it, kept_it)
                if sim >= threshold or title_sim >= title_threshold:
                    better = _choose_better(representatives.get(kept_idx, kept_it), it)
                    if better is it:
                        # idx (it) wins; kept_idx (and whatever it had already absorbed) is absorbed into idx
                        merged_into[kept_idx] = idx
                        related_map[idx].append(_as_related(kept_it))
                        related_map[idx].extend(related_map.get(kept_idx, []))
                        related_map[kept_idx] = []
                        representatives[idx] = it
                    else:
                        # kept_idx wins; idx (and whatever it had already absorbed) is absorbed into kept_idx
                        merged_into[idx] = kept_idx
                        related_map[kept_idx].append(_as_related(it))
                        related_map[kept_idx].extend(related_map.get(idx, []))
                        related_map[idx] = []
                        representatives[kept_idx] = better
                    merged = True
                    break

            if not merged:
                kept.append((idx, it, toks))

    result = []
    seen = set()
    for idx in range(len(stage1)):
        if idx in merged_into:
            continue
        if idx in seen:
            continue
        seen.add(idx)
        rep = dict(representatives[idx])
        rep["related"] = related_map.get(idx, [])
        result.append(rep)

    after_count = len(result)
    print(f"Dedupe: {before_count} -> {after_count} (removed {before_count - after_count} duplicates)")
    print(f"  Stage 1 (exact title): {before_count} -> {stage1_count} (removed {before_count - stage1_count})")
    print(f"  Stage 2 (similarity): {stage1_count} -> {after_count} (removed {stage1_count - after_count})")

    return result
