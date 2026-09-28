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

### Vault export (`scripts/export_to_vault.py`, `.github/workflows/export-vault.yml`)
- **흐름**: `Update industry AX briefing`(update.yml) 완료 → `workflow_run`(completed,
  conclusion=success)로 `export-vault.yml` 트리거, `korea-industry-ax`는 **`ref` 고정 없이**
  그냥 체크아웃(= 트리거 시점의 `main` 최신 — update.yml이 자기 데이터 커밋까지 다 끝낸
  뒤에 이 이벤트가 발동하므로 항상 최신 `public/data.json`을 읽음) + `workflow_dispatch`로
  수동 실행도 가능. (2026-09-28: `ref: github.event.workflow_run.head_sha`로 고정했다가
  그 값이 "소스 워크플로가 시작한 시점의 커밋"이라 실제로는 하루 전 데이터를 계속
  읽는 버그가 있었음 — 아래 진행 상태 참고, `ref` 고정을 없애 수정.)
- **두 개의 독립 job**(같은 트리거에서 병렬 실행, 서로 의존 없음):
  - `export-summary`: **`matrixshin-ai/ax-vault`(public)**, `summary` 모드(data.json의
    title/source/summary만 사용, 원문 fetch 안 함). `vars.VAULT_REPO`(저장소 지정) +
    `secrets.VAULT_DEPLOY_KEY`(SSH 인증)로 체크아웃. `EXPORT_MODE`는 `vars.EXPORT_MODE`
    (없으면 summary 기본값).
  - `export-full`: **`matrixshin-ai/ax-vault-full`(**private**)**, 항상 `EXPORT_MODE=full`
    (trafilatura로 원문 전문 추출, 이미지·댓글·링크 제외). 저장소명은 이 job에 하드코딩
    (VAULT_REPO 변수 아님, 이 job 전용) + `secrets.VAULT_FULL_DEPLOY_KEY`(SSH 인증).
  - 출력 경로 동일: `AX뉴스/YYYY/YYYY-MM-DD/<제목(80자, Windows·Obsidian 금지문자 제거)>_<sha1(url)
    앞8자>.md`. 이미 존재하는 sha1 ID는 건너뜀(재실행해도 중복 생성 안 됨).
- **🚫 금지사항 — public 저장소(`ax-vault`)에는 절대 `full` 모드로 내보내지 말 것.**
  언론사 원문 전문을 public 저장소에 올리면 저작권 문제가 됨. `vars.EXPORT_MODE`를
  `ax-vault`가 public인 동안 `full`로 바꾸지 말 것 — private 전환 후에만 검토.
  (`ax-vault-full`은 이미 private이라 full이 안전함.)
- **인증은 PAT가 아니라 SSH deploy key**: PAT 발급이 GitHub 화면 오류로 안 돼서 이 방식으로
  전환. `ssh-keygen`으로 키 쌍 생성 → `gh repo deploy-key add --allow-write`로 해당 vault
  저장소에 공개키 등록 → 개인키를 이 저장소의 Secret(`VAULT_DEPLOY_KEY`/`VAULT_FULL_DEPLOY_KEY`)
  으로 등록 → 로컬 키 파일 삭제. 워크플로의 `actions/checkout`에는 `token` 대신
  `ssh-key: ${{ secrets.VAULT_DEPLOY_KEY }}` 사용.
- **빈 저장소는 checkout이 실패함**: `git ls-remote --symref ... HEAD`가 커밋이 하나도 없는
  저장소에서는 참조할 HEAD가 없어 exit code 2로 실패(`gh repo create`만으로는 안 됨).
  새 vault 저장소를 만들 때는 반드시 README 등으로 **초기 커밋을 먼저 만들어야** 워크플로가
  체크아웃할 수 있음(ax-vault, ax-vault-full 둘 다 이 순서로 부트스트랩했음).
- **오래된 체크아웃 감지**: 각 job이 체크아웃한 커밋 SHA와 `public/data.json`의 최신 게시일을
  로그에 출력하고, 그 최신 게시일이 오늘(KST)인데 이번 실행에서 생성된 파일이 0건이면
  job을 실패(`exit 1`) 처리 — "성공"인데 실제로는 아무것도 안 한 상태(위 2026-09-28 버그)가
  조용히 넘어가지 않도록 함. 단, 같은 날 재실행해서 이미 다 내보낸 뒤라면 정상적으로도
  0건일 수 있어 그 경우엔 오탐(false positive)이 뜰 수 있음 — 의도된 트레이드오프.
- **`public/data.json`은 최근 게시분만 있음** — 과거 아카이브가 없어 이전에 게시됐던 기사를
  소급해서 vault에 채워 넣을 수 없음(그 시점 이후로 `data.json`이 매일 덮어써짐). 새 vault를
  만들 때 backfill은 그 시점에 `data.json`에 남아있는 것까지만 가능.
- **Obsidian 쪽 설정**: 두 PC 모두 `ax-vault-full`(private)을 clone해서 Obsidian vault로 사용.
  `ax-vault`(public, summary)는 별도로 열지 않음. 각 PC의 obsidian-git 플러그인은
  **pull 전용으로 설정(자동 커밋 끔)** — vault 쪽에서 로컬 편집·커밋을 만들지 않고
  GitHub Actions가 쓴 내용만 받아오는 단방향 흐름 유지.
- **향후 검토 (2~3주 후, 대략 2026-10 중순)**: `ax-vault`(public, summary)를 계속 유지할지
  결정. 계속 쓸모가 없다고 판단되면 `export-summary` job과 저장소를 정리.

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
- 2026-09-26 (네 번째, 같은 날): **Vault export 파이프라인 추가**
  (`scripts/export_to_vault.py` + `.github/workflows/export-vault.yml`, 기존
  수집·분류·병합·게시 코드는 미변경). `matrixshin-ai/ax-vault`(public, summary)와
  `matrixshin-ai/ax-vault-full`(private, full/trafilatura) 두 저장소를 SSH deploy key로
  인증해 매일 게시된 S·A 기사를 기사당 md 1개로 자동 커밋. 상세 설계는 위
  "Vault export" 절 참고. 수동 실행으로 두 저장소 모두 검증 완료(ax-vault 27건,
  ax-vault-full 27건 — 후자는 trafilatura 원문 추출 확인). 신규 테스트 16개
  (`tests/test_export_to_vault.py`) 포함 전체 pytest 통과.
- 2026-09-28: **export-vault.yml이 이틀째 조용히 실패**하고 있었음을 발견 — `ref:
  github.event.workflow_run.head_sha`가 "소스 워크플로가 시작할 때의 커밋"(자기 데이터
  커밋 이전)을 가리켜 매번 하루 전 `public/data.json`을 읽었고, 그래서 매일 "성공"인데
  실제로는 0건 생성. `ref` 고정 제거로 수정, 재발 방지로 체크아웃 커밋 SHA·data.json
  최신 게시일 로그 출력 + "최신 게시일=오늘인데 생성 0건"이면 job 실패 처리 추가.
  수정 직후 검증 실행에서 두 번째 버그 발견(`latest_published()`가 naive/aware
  datetime을 비교하다 `TypeError`로 두 job 모두 크래시 — 일부 RSS 피드의 `published`
  값에 UTC 오프셋이 없어 발생) → naive면 KST로 간주하도록 수정. 재검증 결과 두 vault
  모두 정상: 오늘 게시 93건 중 84건 신규 생성 + 9건은 이미 있던 것(48h 윈도우 중복,
  정상) → skip, `Fail if...` 가드 스텝도 통과(생성>0이라 실패 안 함). pytest 117개 통과.
- 남은 일 / 관찰 포인트:
  - **다음 실행에서 확인**: (1) llm_dedup이 이제 실제로 호출되는지
    (`stats.llm_dedup.ran`=true), (2) S=0이 반복되는지 — 반복되면 v4 프롬프트의
    S 기준이 지나치게 보수적인 것으로 보고 재검토, (3) 채널별 통계값.
  - 1주 운영 후 `logs/yield_*.json`으로 저수율 RSS·검색어 정리 ("산업 AI" 검색어는 1,000건 상한 포화).
  - 규칙 상한(`rule_capped`)과 금융 필터 탈락 건수를 로그로 보며 오탈락 여부 점검.
  - 2026-09-26: GitHub 공개 저장소 생성·push 완료. Actions Secrets 등록·Vercel 연결은 아직 (README 참고).
  - **2~3주 후 (대략 2026-10 중순)**: `ax-vault`(public, summary) 유지 여부 재검토 (위
    "Vault export" 절의 향후 검토 참고).
