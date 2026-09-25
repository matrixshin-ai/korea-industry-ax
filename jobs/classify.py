"""
Haiku classification: candidates.json -> classified.json

Grades each candidate S/A/B/C/X by usefulness to a 울산 industrial-AX policy
audience (not by "how real is the AI adoption"). Only S/A - plus B with
ulsan_score >= 8 - get published (see build.py).

- Sends title/description/source only (no article body fetched).
- Cache: data/classify_cache.json, keyed by sha256(normalized URL), stores
  every grade including X (classification fields + published date only -
  never title/summary/body). Entries older than 48h are pruned every run.
  The cache file itself is NOT committed - CI restores/saves it via
  actions/cache (see .github/workflows/update.yml); a cold start with no cache
  at all still works correctly, just classifies everything fresh.
- Finance/securities title filter (is_finance_title) forces X before the
  cache lookup or any Haiku call; then the keyword prefilter
  (passes_keyword_prefilter).
- Deterministic S caps (apply_rule_caps) run on top of every Haiku/cache
  grade. The cache stores the raw model grade tagged with PROMPT_VERSION;
  entries from another version are re-graded.
- Classification goes through the Message Batches API (50% cheaper) in groups
  of up to BATCH_SIZE items per request. If the whole batch job hasn't reached
  "ended" within BATCH_TIMEOUT_SECONDS, it's canceled and whatever groups
  didn't finish are classified through the synchronous API instead.
- A group that fails to parse, or hits max_tokens (truncated JSON), is split
  in half and retried, down to a floor of MIN_SPLIT_SIZE items; a group that
  still fails at the floor is left unclassified (grade=None) rather than
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

# Bump whenever SYSTEM_PROMPT's grading criteria change. Cache entries written
# under a different version are treated as misses and re-graded, so a prompt
# change takes effect on the very next run instead of after 48h of cache aging.
PROMPT_VERSION = 3

IND_OPTIONS = [
    "에너지", "석유화학", "자동차", "조선", "배터리", "반도체", "철강·기계",
    "물류", "건설", "금융", "의료", "공공", "IT·통신·데이터센터", "기타",
]
TECH_OPTIONS = ["피지컬AI", "로봇", "데이터센터", "디지털트윈", "자율제조", "LLM·에이전트", "기타"]
CORE_INDUSTRIES = {"에너지", "석유화학", "자동차", "조선"}
GRADES = ("S", "A", "B", "C", "X")
GRADE_POINTS = {"S": 100, "A": 70, "B": 40}  # C/X are never published, no score needed
M_VALUES = ("주제", "부분", "언급")

SYSTEM_PROMPT = """당신은 한국어 산업 AI 전환(AX) 뉴스를 등급 매기는 분류기입니다.
독자는 울산을 중심으로 전국 산업 AX를 판단하는 사람들입니다 - 중앙정부·울산시 등 지자체
정책 담당자, 그리고 울산 산업 AX 자문 전문가 그룹. 기준은 "AI 도입이 실질적인가"가
아니라 "울산의 정책 판단에 쓸모가 있는가"입니다.

입력은 JSON 배열이며, 각 원소는 {"i": id, "t": 제목, "d": 설명(네이버/RSS 요약), "s": 매체명}입니다.
각 기사를 분류해 JSON 배열만 반환하세요. 설명, 코드블록, 다른 텍스트를 절대 추가하지 마세요.
입력과 정확히 같은 개수만 반환하면 되고 순서는 상관없습니다(각 원소에 입력의 i를 그대로 포함).

판정은 두 단계입니다: 먼저 m(주제성)을 판정하고, 그다음 등급을 매기세요. m이 등급의
상한을 결정합니다.

1단계 - m(주제성), 등급보다 먼저 판정:
- "주제": 제목 또는 요약 첫 문장이 AX(AI 도입·지원·투자·정책·인재) 자체를 다룬다 ->
  등급 정상 부여 (S~C 모두 가능)
- "부분": AX가 기사의 한 축이지만 기사의 주제는 아니다(다른 주제 기사 속에 AX 내용이
  섞여 있음) -> 등급 상한 B (아무리 내용이 좋아도 B를 넘지 못함)
- "언급": 나열 속 한 항목, 지나가는 발언, 배경 설명 수준일 뿐이다 -> 무조건 X

2단계 - 출력 스키마 (키 이름을 반드시 그대로 사용):
- m이 "언급"이거나 X등급이면 딱 이 세 필드만 반환: {"i": id, "m": m값, "g": "X"}
- 그 외: {"i": id, "m": m값, "g": "S"|"A"|"B"|"C", "s": section, "r": region,
  "loc": 시도명(또는 null), "u": ulsan_score, "ind": industry, "tech": [tech, ...],
  "core": 0또는1, "e": 근거(S·A만, 아래 설명) 또는 null}
  - s(section): 1/2/3 중 하나
    1=기업·현장(기업 AX 전략, AI 드라이브, 공장 도입, 자율제조·다크팩토리, 생산성 개선,
       산업 AI 사업화, FDE 현장 투입 실적)
    2=기술·인프라(피지컬 AI, 휴머노이드, 산업용 LLM, 제조 파운데이션 모델, 데이터센터,
       실증센터·테스트베드)
    3=정책·생태계·인재(중앙/지방정부 지원, M.AX, 예산, 규제, 산학연 협력, 시장 동향,
       FDE 교육 프로그램, 해커톤, 대학 교육, 재교육, 채용)
  - r(region): "울산" / "타지자체" / "전국" / "해외" 중 하나
    - 울산: 울산 소재 기업·기관·현장이 주체이거나 울산이 핵심 무대인 기사
    - 타지자체: 특정 다른 시·도가 주체이거나 핵심 무대인 기사 (loc에 시도명 필수)
    - 전국: 특정 지자체로 한정되지 않는 중앙정부·전국 단위 기사
    - 해외: 해외가 주체이거나 핵심 무대인 기사
  - loc: r이 "타지자체"일 때만 시·도명(서울/부산/대구/인천/광주/대전/세종/경기/강원/
    충북/충남/전북/전남/경북/경남/제주 중 하나). r이 타지자체가 아니면 null.
  - u(ulsan_score): 10(울산 현장 AX) / 8(울산 기업·대학·기관의 AX 활동) /
    4(울산 주력산업과 직결된 사례) / 0(울산과 무관) 중 하나
  - ind(업종, 아래 고정 목록에서 가장 가까운 것 1개만 선택):
    에너지, 석유화학, 자동차, 조선, 배터리, 반도체, 철강·기계, 물류, 건설, 금융, 의료,
    공공, IT·통신·데이터센터, 기타
  - tech(기술, 아래 고정 목록에서 최대 2개, 목록에 없는 것은 쓰지 말고 "기타" 사용):
    피지컬AI, 로봇, 데이터센터, 디지털트윈, 자율제조, LLM·에이전트, 기타
  - core: ind가 에너지/석유화학/자동차/조선 중 하나이면서 "공장·현장" 수준의 실제 AI
    도입(등급 S 수준)이면 1, 그 외에는 0
  - e(근거): 등급이 S 또는 A일 때만 작성. "주체-AI 내용-대상/장소" 형식으로 20자
    내외(예: "현대차-휴머노이드 학습-울산공장", "HD현대重-발전엔진공장 신설-울산").
    구체적인 주체·행위·장소를 채워 넣을 수 없다면(기사에 그 정보가 없다면) 등급을
    한 단계 낮추세요(S->A, A->B) - 등급이 B 이하가 되면 e는 null입니다.

등급 판정 순서: m이 "언급"이면 바로 X. 아니면 X(제외) 해당 여부를 먼저 확인하고,
X가 아니면 S -> A -> B -> C 순서로 어디에 해당하는지 확인하세요(더 높은 등급부터 검토,
m="부분"이면 B에서 멈춤).

X(제외) - 아래 중 하나라도 해당하면 다른 조건과 상관없이 무조건 X:
- 금융권 AI 협약·금융상품·ETF·펀드·주가·목표가·특징주·증시·종목·주주환원·상장·공모주·실적
- 빅테크 모델 경쟁·해외 빅테크 투자 담론 (특정 산업 현장 적용이 명시되지 않은 경우)
- 행사·공연·선언적 비전·축사
- 사회공헌·상생 활동
- 소비자 대상 AI (개인용 앱, 돌봄 챗봇 등)
- 금융·의료 "업무" AI (내부 업무 효율화 등 - 금융/의료 산업 자체의 AX가 아닌 경우)
- AI 요소가 명시되지 않은 공장 신설·설비 투자 (예: 원전설비 공장 신설 자체는 AI 언급이
  없으면 X)
- 단체장·총수·정치인의 동정·일정 기사 (누가 어디를 방문했다/무엇을 했다는 일정 소개.
  단, 제목 자체가 AX 내용을 주제로 다루면 예외 - "S등급" 판단 기준으로 감)
- 종합 인터뷰, "OO대 이슈" 모음, 주간·월간 정리 기사 (여러 주제를 나열하는 기사)
- 칼럼·사설 (단, 제목 자체가 AX를 주제로 다루면 예외)
- AI 요소가 없는 투자 유치·M&A·외교·방산·원자재 수급 기사

S등급 - 전제 조건: 제목 또는 요약 첫 문장(=기사의 주제)에 AI·AX(인공지능)가 명시돼
있어야 합니다. 이 조건을 못 채우면 아래 항목에 해당해도 S가 아닙니다. 특히:
- AI 요소가 명시되지 않은 반도체·설비·공장·연구시설 투자·신설·유치 -> 최대 B
- 데이터센터 전력 인프라(전력망·송전·변전·발전·전력 공급) 기사 -> AI 데이터센터라도 최대 A
전제 조건은 S를 아껴 쓰라는 뜻이 아닙니다 - 전제 조건을 채운 기사가 아래 항목 중 하나에
해당하면 망설이지 말고 S를 주세요:
- 중앙정부 AX 정책·예산·공모사업·선정 결과
- 타 지자체의 AX 전략·실증거점·유치 성과
- 울산 소재 기업·기관(현대차 울산공장, HD현대중공업, SK이노베이션, S-OIL, 고려아연,
  UNIST 등)의 AX 활동
- 울산 주력산업(자동차·조선·석유화학·에너지)의 현장 AX 도입 사례

A등급 (아래 중 하나):
- 해외 주력산업 선도 사례(다크팩토리, 자율운항·스마트 조선소, 화학공정 AI 등)
- AI 데이터센터·전력 인프라 입지 경쟁
- 중소 협력사 AX 지원·사례
- AX 인재양성(FDE, 대학 과정)
- 산업 데이터·안전 규제 변화

B등급 (아래 중 하나, 또는 m="부분"으로 상한이 걸린 경우):
- 주력산업(자동차·조선·석유화학·에너지) 외 업종의 공장·현장 AI 사례
- 산업 AI 기술개발 발표
- 대기업 AX 계획·MOU

C등급: 그 외 산업 AX 관련 기사 (X는 아니지만 S/A/B 어디에도 뚜렷이 해당하지 않는 경우)

실제 오판 사례로 배우는 기준 (잘못 높은 등급을 받았던 사례들, 마지막 하나만 정답 사례):
- "김상욱 울산시장, 추석 앞두고 시립요양원·노동 현장 찾아" -> 시장의 동정·일정
  소개일 뿐 AX가 제목의 주제가 아님 -> m="언급" -> X
- "전북권 올 추석 밥상머리 최대 화두는?...10대 이슈 톺아보기" -> 여러 이슈를 나열하는
  모음 기사, AX는 그중 한 항목일 뿐 -> m="언급" -> X
- "李대통령 '트럼프와 군함 건조 포함 조선 협력 논의'" -> 제목의 주제는 외교·방산
  협력이지 AX가 아님(AI 언급이 기사 속 다른 발언에 섞여 있을 뿐) -> m="언급"~"부분"
  -> X (조선 협력 자체에는 AI 요소 없음)
- "전직원 스톡옵션, 글로벌 선박AS 개척…현대마솔 3배 키운 KKR" -> 제목의 주제는
  KKR의 투자·M&A 성과이지 AX가 아님 -> X
- "울산시-HD현대중공업, 발전엔진·SMR 공장 신설 맞손" -> 울산 핵심 기업 기사지만 AI
  요소가 없는 공장 신설 -> S 아님. AI 언급이 전혀 없으면 X, 있더라도 최대 B
- "SK그룹주 ETF의 귀환, 하이닉스발 주주환원·AI 확장 기대감" -> ETF·주주환원 등 증권
  기사 -> X
- "김상욱 '울산시정 기준은 시민 삶…공개행정은 보완하며 계속'" -> 단체장 종합 인터뷰,
  AX는 여러 시정 주제 중 하나 -> m="언급" -> X
- "경북·경남·전북, 피지컬AI 지역 거점 육성 협약" -> 제목에 AI 명시 + 타 지자체의 AX
  전략·거점 -> S (e: "경북·경남·전북-피지컬AI 거점 협약-3개 도") (A로 낮췄던 오판)
- "현대차 '아틀라스', 공장 학습 본격 시작…2028년 투입 목표로 훈련" -> 제목 자체가
  "휴머노이드 로봇의 공장 학습"이라는 AX 내용을 주제로 다룸 -> m="주제" -> A 이상
  (e: "현대차-휴머노이드 아틀라스 공장학습-생산현장")
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


# Finance/securities title filter: runs before Haiku (and before the cache
# lookup), forcing X with no API call. These are stock-market stories that
# merely mention AI (ETF, 목표가, 특징주...) - the prompt already says X for
# them, but Haiku still let some through (an ETF story once graded S).
# Exception: a title naming 울산 together with AI/AX is left for Haiku to judge.
FINANCE_TITLE_KEYWORDS = ["ETF", "펀드", "주가", "목표가", "특징주", "증시", "종목", "주주환원", "상장", "공모주"]

# "AI/AX explicitly named" - the Latin token must not be glued to other Latin
# letters on the left or lowercase on the right (so "MAX"/"SAIL" don't count,
# while "AI팩토리"/"M.AX"/"AIDC" do).
_AI_TERM_RE = re.compile(r"(?<![A-Za-z])(AI|AX)(?![a-z])|인공지능|에이아이")
_DATACENTER_RE = re.compile(r"데이터\s*센터|AIDC")
_POWER_RE = re.compile(r"전력|송전|변전|발전소|전기\s*공급")


def has_ai_term(text: str) -> bool:
    return bool(_AI_TERM_RE.search(text or ""))


def is_finance_title(title: str) -> bool:
    title = title or ""
    upper = title.upper()
    if not any(kw.upper() in upper for kw in FINANCE_TITLE_KEYWORDS):
        return False
    return not ("울산" in title and has_ai_term(title))


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s|\n", (text or "").strip(), maxsplit=1)[0]


def apply_rule_caps(item: dict) -> dict:
    """Deterministic caps layered on top of Haiku's S grade (fresh and cached
    results alike - the cache keeps the raw model grade):
    - S requires AI/AX named in the topic (title or the summary's first
      sentence). Otherwise S -> A if AI appears somewhere in the summary,
      else S -> B (no AI element at all = plain facility/semiconductor/lab).
    - A data-center power-infrastructure title is capped at A."""
    if item.get("grade") != "S":
        return item
    title = item.get("title", "") or ""
    summary = item.get("summary", "") or ""
    new_grade = "S"
    if not has_ai_term(f"{title} {_first_sentence(summary)}"):
        new_grade = "A" if has_ai_term(summary) else "B"
    elif _DATACENTER_RE.search(title) and _POWER_RE.search(title):
        new_grade = "A"
    if new_grade == "S":
        return item
    out = {**item, "grade": new_grade, "rule_capped_from": "S"}
    if new_grade == "B":
        out["evidence"] = None
    return out


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


LOC_OPTIONS = [
    "서울", "부산", "대구", "인천", "광주", "대전", "세종", "경기", "강원",
    "충북", "충남", "전북", "전남", "경북", "경남", "제주",
]


def _sanitize_ind(value) -> str:
    return value if value in IND_OPTIONS else "기타"


def _sanitize_tech(value) -> list:
    if not isinstance(value, list):
        return []
    return [t for t in value if t in TECH_OPTIONS][:2]


def _sanitize_core(core, ind: str, section) -> int:
    return 1 if (core and ind in CORE_INDUSTRIES and section == 1) else 0


def _sanitize_grade(value) -> str:
    return value if value in GRADES else None


def _sanitize_m(value) -> str:
    return value if value in M_VALUES else None


def _sanitize_region(value) -> str:
    return value if value in ("울산", "타지자체", "전국", "해외") else None


def _sanitize_loc(value, region) -> str:
    return value if (region == "타지자체" and value in LOC_OPTIONS) else None


def _empty_fields(grade=None, m=None):
    return {"grade": grade, "m": m, "section": None, "region": None, "loc": None,
            "ulsan_score": 0, "industry": None, "tech": [], "core": 0, "evidence": None}


def _apply_topicality_cap(grade: str, m: str) -> str:
    """m="언급" always forces X; m="부분" caps S/A down to B (requirement 1)."""
    if m == "언급":
        return "X"
    if m == "부분" and grade in ("S", "A"):
        return "B"
    return grade


def _sanitize_section(value):
    return value if value in (1, 2, 3) else None


def _expand(d: dict) -> dict:
    """Shared expansion for both a Haiku compact result and a cache entry -
    both use the same key names (i/g/m/s/r/loc/u/ind/t or tech/core/e)."""
    m = _sanitize_m(d.get("m"))
    grade = _sanitize_grade(d.get("g"))
    if grade is None:
        return _empty_fields(grade=None, m=m)

    grade = _apply_topicality_cap(grade, m)
    if grade == "X":
        return _empty_fields(grade="X", m=m)

    section = _sanitize_section(d.get("s"))
    if section is None:
        # Haiku omitted/malformed the section for a non-X grade - can't tell
        # which of the 3 sections this belongs under, so treat it the same as
        # any other incomplete response rather than crash or guess: drop it.
        return _empty_fields(grade="X", m=m)

    ind = _sanitize_ind(d.get("ind"))
    region = _sanitize_region(d.get("r"))

    # Requirement 2: S/A needs a concrete "subject-action-place" evidence string;
    # if the model couldn't fill it in, downgrade one level (S->A, A->B) rather
    # than trust an unsupported top grade. B and below never carry evidence.
    evidence = (d.get("e") or "").strip() or None
    if grade in ("S", "A") and not evidence:
        grade = {"S": "A", "A": "B"}.get(grade, grade)

    return {
        "grade": grade,
        "m": m,
        "section": section,
        "region": region,
        "loc": _sanitize_loc(d.get("loc"), region),
        "ulsan_score": d.get("u", 0),
        "industry": ind,
        "tech": _sanitize_tech(d.get("t") if "t" in d else d.get("tech")),
        "core": _sanitize_core(d.get("core"), ind, section),
        "evidence": evidence if grade in ("S", "A") else None,
    }


def cache_entry_to_fields(entry: dict) -> dict:
    return _expand(entry)


def fields_to_cache_entry(fields: dict, published: str) -> dict:
    grade = fields.get("grade")
    entry = {"g": grade, "m": fields.get("m"), "pub": published, "v": PROMPT_VERSION}
    if grade and grade != "X":
        entry.update({
            "s": fields.get("section"), "r": fields.get("region"), "loc": fields.get("loc"),
            "u": fields.get("ulsan_score", 0), "ind": fields.get("industry"),
            "t": fields.get("tech", []), "core": fields.get("core", 0),
            "e": fields.get("evidence"),
        })
    return entry


def build_batch_input(items):
    return [{"i": it["id"], "t": it["title"], "d": it.get("summary", ""), "s": it.get("source", "")} for it in items]


def expand_compact_result(p: dict) -> dict:
    return _expand(p)


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
            results[it["id"]] = expand_compact_result(p) if p else {**_empty_fields(), "unclassified": True}
        return results
    except Exception as e:  # noqa: BLE001 - a bad group must not stop the run
        if len(group) > MIN_SPLIT_SIZE:
            mid = len(group) // 2
            log.setdefault("split_retries", []).append({"original_size": len(group), "error": str(e)})
            results = _classify_one_group_sync(client, group[:mid], log)
            results.update(_classify_one_group_sync(client, group[mid:], log))
            return results
        log["failed_batches"].append({"group_size": len(group), "error": str(e), "item_ids": [it["id"] for it in group]})
        return {it["id"]: {**_empty_fields(), "unclassified": True} for it in group}


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
                    id_to_result[it["id"]] = expand_compact_result(p) if p else {**_empty_fields(), "unclassified": True}
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
    finance_filtered = []
    for c in candidates:
        if is_finance_title(c.get("title", "")):
            finance_filtered.append(c)
            continue
        entry = cache.get(cache_key(c.get("link", "")))
        if entry and entry.get("v") == PROMPT_VERSION:
            log["cache_hits"] += 1
            classified.append(apply_rule_caps({**c, **cache_entry_to_fields(entry)}))
        else:
            to_classify.append(c)

    for c in finance_filtered:
        classified.append({**c, **_empty_fields(grade="X"), "finance_filtered": True})
    log["finance_title_filtered_out"] = len(finance_filtered)

    keyword_pass = [c for c in to_classify if passes_keyword_prefilter(c)]
    keyword_filtered_out = [c for c in to_classify if not passes_keyword_prefilter(c)]
    for c in keyword_filtered_out:
        classified.append({**c, **_empty_fields(grade="X"), "prefiltered": True})

    log["to_classify"] = len(to_classify)
    log["keyword_prefilter_pass"] = len(keyword_pass)
    log["keyword_prefilter_filtered_out"] = len(keyword_filtered_out)
    print(f"Candidates: {len(candidates)} total, {len(finance_filtered)} finance-title filtered, "
          f"{log['cache_hits']} cache hits, {len(to_classify)} to classify "
          f"({len(keyword_pass)} pass keyword prefilter, {len(keyword_filtered_out)} filtered out)")

    new_results = {}
    if keyword_pass:
        if not api_key:
            print(f"ANTHROPIC_API_KEY not set - {len(keyword_pass)} candidates left unclassified (will retry next run)")
            log["skipped_no_api_key"] = True
            for c in keyword_pass:
                classified.append({**c, **_empty_fields(), "unclassified": True})
        else:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            new_results = classify_via_batches_api(client, keyword_pass, log)
            for c in keyword_pass:
                r = new_results.get(c["id"], {**_empty_fields(), "unclassified": True})
                classified.append(apply_rule_caps({**c, **r}))

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
    log["rule_capped"] = sum(1 for c in classified if c.get("rule_capped_from"))
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
