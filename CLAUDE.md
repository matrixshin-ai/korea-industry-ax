# CLAUDE.md — korea-industry-ax

Claude Code가 새 세션을 시작할 때 자동으로 읽는 맥락 파일. 설계가 바뀌거나 작업이 끝나면
이 파일의 "진행 상태"를 갱신할 것. **비밀키 값은 절대 적지 않는다** (이름만).

## 프로젝트 개요

- 울산 중심 전국 산업 AX(AI 전환) 뉴스 공개 웹앱.
- 독자: 중앙정부·울산시 정책 담당자, 울산 산업 AX 자문 전문가.
- 판단 기준: "AI 도입이 실질적인가"가 아니라 "울산의 정책 판단에 쓸모가 있는가".
- 로컬 폴더 `%USERPROFILE%\korea-industry-ax`, **로컬 git만 사용, push 안 함** (원격 없음,
  브랜치 `master`. 워크플로는 `main`을 가정하므로 사용자가 push할 때 `git branch -M main`).
- 원본 `ulsan-economy-dashboard`는 **절대 수정 금지** (dedup.py 등은 그쪽 코드를 참고해 복사한 것).

## 확정된 설계

### 수집 (`jobs/collect.py`)
- 네이버 뉴스 검색 API(`config/queries.yaml`) + 언론사 RSS(`config/sources.yaml`, 공공기관 피드 제외).
- 월~토 KST 06:33 1회 실행: cron `33 21 * * 0-5` (UTC).
- 기간: 화~토 24시간, 월 48시간. 경계는 KST 06:30 고정 (`jobs/timewindow.py`가 유일한 정의).

### 분류 (`jobs/classify.py`)
- 모델 Haiku 4.5 (`claude-haiku-4-5-20251001`), Message Batches API, 배치 80건.
  90분 내 미완료분은 동기 API로, 파싱 실패·max_tokens 절단 시 절반씩 쪼개 재시도(최소 10).
- 입력은 제목·요약·매체명만 (본문 수집 안 함).
- 출력 필드: 등급 g(S/A/B/C/X), 주제성 m(주제/부분/언급), 섹션 s(1/2/3), 지역 r(울산/타지자체/전국/해외)
  ·loc(타지자체일 때 시도명), 울산점수 u(10/8/4/0), 업종 ind, 기술 tech, core, 근거 e.
  - m=언급 → X, m=부분 → 최대 B.
  - S·A는 근거 e("주체-AI 내용-대상/장소") 필수, 없으면 한 단계 강등.
  - core=1: ind가 에너지·석유화학·자동차·조선 + 섹션 1(현장 AI 도입).
- **Haiku 전 규칙 필터** (캐시 조회보다 먼저 적용):
  - 제목 금융·증권 필터: ETF, 펀드, 주가, 목표가, 특징주, 증시, 종목, 주주환원, 상장, 공모주 → X.
    단 제목에 "울산" + AI/AX(인공지능)가 함께 있으면 예외(Haiku가 판단).
  - 키워드 사전 필터(`PREFILTER_KEYWORDS`) 미포함 → X.
- **Haiku 후 규칙 상한** (`apply_rule_caps`, 캐시 결과에도 적용):
  - S는 주제(제목 또는 요약 첫 문장)에 AI·AX가 명시된 경우만. AI가 요약 뒷부분에만 있으면 A,
    전혀 없으면 B (AI 요소 없는 반도체·설비·연구시설은 최대 B).
  - 데이터센터 전력 인프라(제목에 데이터센터/AIDC + 전력·송전·변전·발전소) → 최대 A.
  - 프롬프트에도 같은 기준이 들어 있음.
- 캐시 `data/classify_cache.json`: 정규화 URL sha256 키, 원본 모델 등급 저장, 48h 경과 시 삭제,
  `PROMPT_VERSION` 태그 — **프롬프트 기준을 바꾸면 `PROMPT_VERSION`을 올릴 것** (다른 버전 항목은 재분류).
  git 커밋 안 함, CI에서는 `actions/cache`로 보관.

### 병합·게시 (`jobs/build.py`)
- 중복 병합 순서: URL 정규화 → dedup.py(알고리즘) → llm_dedup.py(같은 기관명 클러스터만 Haiku,
  한 기사는 처음 배정된 그룹에만 속함 = 클러스터 간 연쇄 병합 차단) →
  `config/event_merge.yaml`(사건별 제목 규칙, `until` 이후 자동 만료).
- 병합 그룹은 최고 등급 멤버가 대표, 나머지는 `related`.
- 점수 = 등급 기본점(S100/A70/B40) + u(10/8/4/0) + core×10.
- 게시: S·A 전부, B는 u≥8일 때만. 하루 200건 상한(`DAILY_CAP`, 전 섹션 통합 점수순 → 최신순).
- 섹션 3개: 1 기업·현장 / 2 기술·인프라 / 3 정책·생태계·인재.
- 출력: `public/data.json` 하나 (DB 없음). `stats`에 상한 전·후, 섹션별, 금융 필터 탈락 수 등.

### 화면 (`public/index.html`)
- 프레임워크 없는 정적 페이지. 첫 화면은 섹션별 점수 상위 10건 + 더보기, 섹션 화면은 20건씩 더보기.
- 필터: 지역(울산/타지자체+시도/전국/해외), 업종, 검색.

### 배포 (사용자가 직접)
- GitHub Actions `.github/workflows/update.yml` → `public/data.json`, `logs/` 커밋·push →
  Vercel Git 연동(Root `public/`). 비밀키 이름: `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`, `ANTHROPIC_API_KEY`.
- API 키 이상 징후는 사용자가 실시간으로 직접 감시함 — 키 교체·노출 문제를 다시 제기하지 말 것.

## 로컬 실행·검증

```bash
python jobs/collect.py && python jobs/classify.py && python jobs/build.py
python -m pytest tests/ -q
python -m http.server 8000 --directory public   # http://localhost:8000
```
환경변수는 셸에서 설정(이 프로젝트는 .env를 직접 읽지 않음). Windows에서는 `python -X utf8` 권장.

## 진행 상태

- 2026-09-24: 초기 파이프라인 → S/A/B/C/X 등급 체계 → 주제성 m 게이트·사건 단위 등급 통일 →
  피드/검색어별 수율 로그(logs/yield_*).
- 2026-09-25: 제목 금융·증권 필터, S 요건 강화(규칙 상한 + 프롬프트), `PROMPT_VERSION` 캐시 무효화(현재 v3: S 과소 부여 보정 예시 추가),
  S·A(+울산 B u≥8)만 게시, 하루 200건 상한, llm_dedup 연쇄 병합 차단(카드 1건이 related 240개를
  흡수하던 버그), 대통령 투자서밋 사건 병합 규칙(`until: 2026-09-30`), 첫 화면 섹션별 상위 10건,
  README "사용자가 직접 할 일" 갱신.
- 남은 일 / 관찰 포인트:
  - 1주 운영 후 `logs/yield_*.json`으로 저수율 RSS·검색어 정리 ("산업 AI" 검색어는 1,000건 상한 포화).
  - 규칙 상한(`rule_capped`)과 금융 필터 탈락 건수를 로그로 보며 오탈락 여부 점검.
  - GitHub push·Secrets·Vercel 연결은 아직 안 함 (README 참고).
