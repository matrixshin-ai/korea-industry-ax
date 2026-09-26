# korea-industry-ax

독자: 중앙정부·울산시 정책 담당자, 울산 산업 AX 자문 전문가.

울산·전국·해외 산업 AX(AI 전환) 한국어 뉴스를 하루 1회(월~토 KST 06:33) 수집해, 화~토는
최근 24시간, 월요일은 최근 48시간(토요일 이후 공백 포함) 기사만 보여주는 공개 웹앱.
경계는 KST 06:30에 고정(실제 실행 시각이 약간 늦어져도 매일 같은 기준으로 창이 잡힘).

- 수집: 언론사 RSS + 네이버 뉴스 검색 API
- 분류: Claude Haiku 4.5가 S/A/B/C/X 등급을 매김 (제목·요약·매체명만 사용, 본문 수집 안 함).
  기준은 "울산의 정책 판단에 쓸모가 있는가". 주제성 m(주제/부분/언급: 부분은 최대 B,
  언급은 X), S·A는 근거 e("주체-AI 내용-대상/장소") 필수. **B는 게시되지 않는다** - 강등
  목적지(판정용)일 뿐이며 게시는 S·A만 (아래 "게시" 참고).
  - Haiku 전 규칙 필터: 제목에 금융·증권 키워드(ETF, 펀드, 주가, 목표가, 특징주, 증시, 종목,
    주주환원, 상장, 공모주)가 있으면 X (제목에 "울산"+AI/AX가 함께 있으면 예외).
    이어서 키워드 사전 필터(AI·로봇·자율 등 미포함 시 X).
  - Haiku 후 규칙 상한: S는 주제(제목 또는 요약 첫 문장)에 AI·AX가 명시된 경우만. AI가 요약
    뒷부분에만 있으면 A, 전혀 없으면(반도체·설비·연구시설 투자 등) B. 데이터센터 전력
    인프라는 최대 A.
  - 울산점수 u: AX 내용 자체가 울산 소재 기업·기관·현장과 직접 연결될 때만 부여(region이
    "울산"이 아니면 무조건 0, 코드에서도 region!="울산"이면 u=0으로 강제). 울산이 기사에
    등장해도 AX 내용과 무관하면(사건·사고 기사 등) 0.
  - Haiku 호출은 Message Batches API(표준가 50%)로 제출하고 90분 내 미완료분만
  일반 API로 재처리. 파싱 실패·응답 절단 시 배치를 절반씩 나눠 재시도(최소 10건까지).
  분류 결과는 `data/classify_cache.json`에 정규화 URL 해시로 캐시(48시간 경과 시 자동
  삭제, X 포함 모든 등급 저장, 본문/요약은 저장 안 함). 캐시 항목에는 프롬프트 버전
  (`PROMPT_VERSION`)이 붙어 있어 분류 기준을 바꾸면 다음 실행에서 전부 재분류된다. 이 캐시 파일은 git에
  커밋하지 않고 GitHub Actions의 `actions/cache`로 실행 간 보관한다 - 캐시가 없어도
  콜드 스타트로 정상 동작한다.
- 중복 병합: dedup.py(제목·숫자·기관명 알고리즘 병합) → llm_dedup.py(게시 후보 - S·A 등급이
  하나라도 있는 그룹 - 전체의 제목 전체를 Haiku 1회 호출로 동일 사건 그룹핑, 후보가
  200건을 넘으면 200건 단위로 분할 호출; 발표·선정·착공·실증·성과처럼 사건 단계가 다르면
  병합하지 않음, 연쇄 병합 방지). 병합 그룹은 최고 등급·최고 점수 기사가 대표 - 단, 대표가
  S·A인데 제목에 AX 내용이 없으면(예: 서밋 AX 발표 그룹에서 "군함 건조" 제목) 그룹 내
  다른 S·A 기사 중 제목에 AX 내용이 있는 기사로 대표를 교체.
- 점수: 등급 기본점(S100/A70) + u(울산 10/8/0) + core×10.
- 게시: **S·A만** (B는 등급과 무관하게 게시되지 않음). 하루 200건 상한(전 섹션 통합
  점수순, 동점은 최신순). 섹션 3개(기업·현장 / 기술·인프라 / 정책·생태계·인재).
- 저장: DB 없음. `public/data.json` 정적 파일 하나
- 화면: 프레임워크 없는 정적 `public/index.html` (좌측 사이드바, 첫 화면은 섹션별 점수 상위
  10건 + 더보기)

## 폴더 구조

```
config/sources.yaml     RSS 목록 + 도메인→매체명 표
config/queries.yaml     네이버 검색어
jobs/collect.py         RSS + 네이버 수집 → candidates.json
jobs/classify.py        금융 제목 필터·키워드 필터 + Haiku 분류(Batches API, 캐시) + S 규칙 상한 → classified.json
jobs/build.py           중복 제거·기간 필터·게시 규칙(S·A만)·200건 상한·점수 → public/data.json
jobs/dedup.py           동일 사건 병합 (알고리즘)
jobs/llm_dedup.py       동일 사건 병합 (게시 후보 전체 제목 Haiku 판정, 200건 단위 청크)
jobs/timewindow.py      24h/48h KST 06:30 기준 수집 기간 계산 (단일 정의)
jobs/urlnorm.py         URL 정규화
data/classify_cache.json  분류 결과 캐시 (커밋 안 함 - actions/cache로 보관, .gitignore 처리)
public/index.html       화면
tests/                  pytest
logs/                   실행 로그 (run_ 수집, classify_ 분류·비용, yield_ 피드/검색어별 S·A 수율)
CLAUDE.md               설계·진행 상태 (Claude Code 새 세션용)
```

## 로컬 실행

```bash
pip install -r requirements.txt

# 환경변수 (셸에서 export 또는 .env 로더 사용 - 이 프로젝트는 .env 파일을 직접 읽지 않음)
export NAVER_CLIENT_ID=...
export NAVER_CLIENT_SECRET=...
export ANTHROPIC_API_KEY=...

python jobs/collect.py    # -> candidates.json
python jobs/classify.py   # -> classified.json (candidates.json 필요)
python jobs/build.py      # -> public/data.json (classified.json 필요)

# 화면 확인 (정적 서버 아무거나 가능)
python -m http.server 8000 --directory public
# http://localhost:8000 접속
```

테스트:

```bash
python -m pytest tests/ -q
```

## 필요한 비밀키 (이름만 - 값은 여기 적지 않음)

| 이름 | 용도 | 없을 때 동작 |
|---|---|---|
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 네이버 뉴스 검색 API | 네이버 검색 건너뛰고 RSS만 사용 |
| `ANTHROPIC_API_KEY` | Haiku 분류 | 새 후보를 전부 미분류로 남기고 다음 실행에서 재시도 |

## 사용자가 직접 할 일

이 저장소는 로컬 git 커밋까지만 되어 있습니다(원격 없음). 배포까지 아래 순서로 직접 진행해주세요.

1. **GitHub 저장소 생성 후 push**: `korea-industry-ax` 이름으로 새 저장소를 만든 뒤 push.
   로컬 브랜치가 `master`이고 워크플로는 `main`에 push하므로 브랜치 이름을 먼저 바꿔야 합니다.
   ```bash
   git branch -M main
   git remote add origin <새 저장소 URL>
   git push -u origin main
   ```
   `public/data.json`과 `logs/`는 커밋 대상이고, `candidates.json`·`classified.json`·
   `data/classify_cache.json`은 `.gitignore`로 제외됩니다.
2. **GitHub Actions 비밀키 등록**: 저장소 Settings → Secrets and variables → Actions에서
   위 표의 세 개(`NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`, `ANTHROPIC_API_KEY`) 등록.
3. **Actions 쓰기 권한 확인**: Settings → Actions → General → Workflow permissions가
   "Read and write permissions"인지 확인 (워크플로가 `public/data.json`을 커밋·push함).
4. **첫 실행 수동 확인**: Actions 탭에서 "Update industry AX briefing"을 `Run workflow`로 한 번
   실행해 성공하는지 확인. 이후 월~토 KST 06:33에 자동 실행됩니다. 첫 실행은 캐시가 없어
   전체를 분류하므로 비용이 평소보다 큽니다(2026-09-25 로컬 전체 재분류 약 2,000건 기준 $0.32,
   `logs/classify_*.json`의 `estimated_cost_usd`로 확인).
5. **Vercel 연결**: Vercel 대시보드에서 이 GitHub 저장소를 Import (Git 연동, 자동 배포).
   Root Directory는 `public/`로 지정, Framework Preset은 "Other", Build Command는 비움.
   워크플로에서 Vercel CLI는 쓰지 않으므로 별도 토큰 설정은 필요 없습니다.
6. **운영 점검(주기적)**:
   - API 키 사용량 이상 여부는 Anthropic Console / 네이버 개발자센터에서 직접 확인.
   - 1주일쯤 운영 후 `logs/yield_*.json`에서 S·A를 거의 못 건지는 RSS·검색어를 정리하세요.

## 하지 않는 것

본문 수집·저장, DB, 로그인, 관리자 화면, 기사별 AI 요약, 경제지표, 영상·음성, 장기 아카이브, 임베딩 기반 중복 제거.
