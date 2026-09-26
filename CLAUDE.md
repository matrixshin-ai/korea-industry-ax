# CLAUDE.md — korea-industry-ax

Claude Code가 새 세션을 시작할 때 자동으로 읽는 맥락 파일. 설계가 바뀌거나 작업이 끝나면
이 파일의 "진행 상태"를 갱신할 것. **비밀키 값은 절대 적지 않는다** (이름만).

## 프로젝트 개요

- 울산 중심 전국 산업 AX(AI 전환) 뉴스 공개 웹앱.
- 독자: 중앙정부·울산시 정책 담당자, 울산 산업 AX 자문 전문가.
- 판단 기준: "AI 도입이 실질적인가"가 아니라 "울산의 정책 판단에 쓸모가 있는가".
- 로컬 폴더 `%USERPROFILE%\korea-industry-ax`, 원격 `origin` =
  https://github.com/matrixshin-ai/korea-industry-ax (**공개 저장소**, 브랜치 `main`).
  push는 사용자가 지시할 때만. 공개 저장소이므로 비밀키·.env·캐시가 커밋되지 않게 주의.
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
  ·loc(타지자체일 때 시도명), 울산점수 u(10/8/0), 업종 ind, 기술 tech, core, 근거 e.
  - m=언급 → X, m=부분 → 최대 B.
  - S·A는 근거 e("주체-AI 내용-대상/장소") 필수, 없으면 한 단계 강등.
  - core=1: ind가 에너지·석유화학·자동차·조선 + 섹션 1(현장 AI 도입).
  - **B는 게시되지 않는다** — 강등 목적지(판정용)일 뿐, `build.py`가 절대 게시하지 않음.
- **울산점수 u**: AX 내용 자체가 울산 소재 기업·기관·현장과 직접 연결될 때만 부여.
  region이 "울산"이 아니면(타지자체/전국/해외) 무조건 0 — 프롬프트뿐 아니라
  `classify.py`의 `_sanitize_ulsan_score`가 region≠"울산"이면 u를 0으로 강제(방어 코드).
  울산이 기사에 등장해도 AX 내용과 무관하면(사건·사고 등) 0.
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
  git 커밋 안 함, CI에서는 `actions/cache`로 보관. 2026-09-26 u 기준 변경으로 v3→v4.

### 병합·게시 (`jobs/build.py`)
- 중복 병합 순서: URL 정규화 → dedup.py(알고리즘: 제목·숫자·기관명) → llm_dedup.py(게시
  후보 — S·A 등급이 하나라도 있는 그룹 — 전체의 제목 전체를 Haiku 1회 호출로 동일 사건
  그룹핑; 후보가 200건(`MAX_CHUNK_SIZE`)을 넘으면 200건 단위로 분할 호출, 청크 간 겹치는
  항목이 없으므로 구조적으로 연쇄 병합 불가 + 한 청크 응답 내에서도 먼저 배정된 그룹만
  인정하는 방어 로직 유지). 발표·선정·착공·실증·성과처럼 사건 단계가 다르면 병합 안 함.
  `config/event_merge.yaml` 수동 규칙은 2026-09-26 폐기 — llm_dedup의 전체-제목 패스가 대체.
- 병합 그룹은 최고 등급·최고 점수 멤버가 대표, 나머지는 `related`. 단, 대표가 S·A인데
  제목에 AI/AX가 없으면(예: 서밋 AX 발표 그룹에서 대표가 "군함 건조" 제목) 그룹 내 다른
  S·A 멤버 중 제목에 AI/AX가 있는 것으로 대표를 교체.
- 점수 = 등급 기본점(S100/A70) + u(10/8/0) + core×10.
- 게시: **S·A만** (B는 등급과 무관하게 게시 안 됨 — `B_MIN_ULSAN_SCORE` 경로 삭제).
  하루 200건 상한(`DAILY_CAP`, 전 섹션 통합 점수순 → 최신순).
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
- 2026-09-26: B 등급 폐기(게시는 S·A만, `B_MIN_ULSAN_SCORE`/`is_publishable`의 B 경로 삭제,
  화면·README·CLAUDE.md에서 B 표시 제거), 울산점수 u 기준 보완(AX 내용이 울산 소재
  기업·기관·현장과 직접 연결될 때만 u>0, region≠"울산"이면 코드에서 u=0 강제,
  `PROMPT_VERSION` v3→v4), 사건 병합 단순화(llm_dedup.py를 기관명 클러스터 방식에서
  게시 후보 전체 제목 Haiku 1회 호출 방식으로 교체, 200건 단위 청크, 연쇄 병합 방지 로직
  유지, `config/event_merge.yaml`과 그 로딩 코드 삭제), 대표 기사 선정 로직 개선
  (최고 등급·점수 대표의 제목에 AI/AX가 없으면 그룹 내 다른 S·A 기사로 대표 교체).
- 2026-09-26 (같은 날, GitHub Actions run #2 = commit `55039c1` 검증):
  - **버그 발견·수정**: `.github/workflows/update.yml`의 "Build public/data.json" 스텝에
    `ANTHROPIC_API_KEY`가 초기 커밋부터 누락돼 있었음 — build.py의 llm_dedup(전체-제목
    Haiku 병합) 패스가 실제로는 **한 번도 실행된 적이 없었음** (classify 스텝에는 키가
    있어 분류는 정상 작동, 병합만 항상 알고리즘 dedup.py로만 처리됨). 워크플로에
    env 추가로 수정, 다음 실행부터 반영.
  - **로그 보완**: `stats.llm_dedup.ran`(패스가 실제로 호출됐는지 여부 — 이번 버그처럼
    "실행됐지만 병합 0건"과 "애초에 스킵됨"을 구분하기 위함), `estimated_cost_usd`
    (llm_dedup 자체 비용, 이전엔 토큰 수만 있고 비용 환산이 없었음),
    `stats.published_merged_group_count`/`published_related_item_count`(게시된
    병합 그룹 수·흡수된 관련기사 수 — 이전엔 `related` 배열을 직접 훑어야만 알 수 있었음),
    `stats.classify_cost_usd`/`llm_dedup_cost_usd`/`total_estimated_cost_usd`(classify
    로그를 build.py가 읽어와 이번 실행 총비용을 data.json에 직접 기록) 추가.
  - **run #2 (55039c1, PROMPT_VERSION v4 콜드스타트) 분석 결과**:
    - 게시 43건 (상한 전 43건, 상한 미적용 — 200건 여유): 섹션1(기업·현장) 19,
      섹션2(기술·인프라) 19, 섹션3(정책·생태계·인재) 5. 등급: S=0, A=43.
    - **S=0건은 위 버그와 무관** — Haiku 원본 분류 단계(`rule_capped`=0, 즉 S→강등도
      0건)부터 이미 S가 없었음. v4 프롬프트가 과도하게 보수적인지, 그날 후보군에
      정말 S급이 없었는지는 이번 한 번으로는 판단 불가 — 다음 며칠 결과와 비교 필요.
    - 병합 그룹 3건 (전부 dedup.py 알고리즘 병합, llm_dedup은 버그로 미실행이라 0건
      기여): "SK하이닉스 솔리다임 IPO"(관련 2건), "전북 현대차 새만금 투자"(관련 1건),
      "오픈AI·앤트로픽 피지컬AI 인재영입"(관련 1건).
    - 비용: classify $0.2851(1,912건 콜드스타트 전량 재분류, batches API), llm_dedup
      $0(미실행) → 이번 실행 총 $0.2851. 워크플로 수정 후 다음 실행부터는
      `total_estimated_cost_usd`로 매번 자동 기록됨.
    - pytest: 92개 전부 통과(신규 커버리지 2건 포함).
- 2026-09-26 (같은 날, 세 번째 점검 — 네이버 수집 검증 + 시크릿 전수 점검):
  - **네이버 수집 확인**: run #2에서 정상 작동. `logs/run_20260926_1521.json`:
    `naver.attempted=true`, 쿼리 56개·126콜, 후보 1,428건(RSS 530건, URL 중복 제거 후
    합계 1,912건). RSS 피드 26개 전부 `status: ok`(설정된 26개 전부), 네이버 쿼리
    에러 0건, "울산 AI" 1건만 1,000건 상한 포화. 수집 자체는 문제 없었음.
  - **게시 기사의 출처별(RSS/네이버) 건수는 run #2에 대해 재구성 불가**: `collected_via`
    필드가 build.py에서 최종 게시 직전에 제거되고, 그 중간 산출물(`classified.json`)은
    커밋 대상이 아니라서 이미 사라짐. `public/data.json`이나 로그 어디에도 남아있지
    않음 — 이번 실행분은 정확한 수치를 보고할 수 없음(등급별 카운트만 있었음).
  - **수정**: `build.py`에 `stats.candidates_by_channel`/`stats.published_by_channel`
    ({"rss"/"naver"/"unknown": n}) 추가 — `_channel_counts()`가 최종 게시 직전(pop 전)
    `collected_via`를 집계. **다음 실행부터** data.json에서 바로 확인 가능.
  - **시크릿 전수 점검**: `os.environ.get(...)`을 쓰는 곳은 collect.py(NAVER_CLIENT_ID/
    SECRET), classify.py·build.py(ANTHROPIC_API_KEY)뿐(`dedup.py`의 `KEYWORD_RULES_JSON`은
    비밀키 아닌 선택적 경로 오버라이드). 세 스텝 모두 이제 필요한 env를 선언 —
    build 스텝 누락 건(위 참고) 외 다른 누락은 없었음.
  - **워크플로에 시크릿 사전검증 스텝 추가**("Verify required secrets are configured",
    Checkout 바로 다음): `NAVER_CLIENT_ID`/`NAVER_CLIENT_SECRET`/`ANTHROPIC_API_KEY` 중
    하나라도 비어 있으면 즉시 `exit 1`로 워크플로 실패 처리(이전엔 스크립트들이
    조용히 저하 모드로 동작해 이번 build 버그처럼 실패 신호 없이 넘어갈 수 있었음).
    로컬 개발 시 키 없이 부분 실행하는 것은 각 스크립트 자체의 우아한 저하 동작으로
    계속 지원됨(README "필요한 비밀키" 표) — 이 검증은 CI 워크플로에만 적용.
  - pytest 94개 전부 통과(신규 커버리지 2건 추가).
  - **수동 실행 권장**: 위 변경(사전검증 스텝, build 스텝 키, 채널별 통계) 확인을 위해
    Actions 탭에서 "Update industry AX briefing" `Run workflow` 1회 권장. 확인 포인트:
    (1) "Verify required secrets" 스텝 통과, (2) `stats.llm_dedup.ran=true`,
    (3) `stats.candidates_by_channel`/`published_by_channel` 값 존재.
- 남은 일 / 관찰 포인트:
  - **다음 실행에서 확인**: (1) llm_dedup이 이제 실제로 호출되는지
    (`stats.llm_dedup.ran`=true), (2) S=0이 반복되는지 — 반복되면 v4 프롬프트의
    S 기준이 지나치게 보수적인 것으로 보고 재검토, (3) 채널별 통계값.
  - 1주 운영 후 `logs/yield_*.json`으로 저수율 RSS·검색어 정리 ("산업 AI" 검색어는 1,000건 상한 포화).
  - 규칙 상한(`rule_capped`)과 금융 필터 탈락 건수를 로그로 보며 오탈락 여부 점검.
  - 2026-09-26: GitHub 공개 저장소 생성·push 완료. Actions Secrets 등록·Vercel 연결은 아직 (README 참고).
