# korea-industry-ax

울산·전국·해외 산업 AX(AI 전환) 한국어 뉴스를 하루 1회(월~토 KST 06:33) 수집해, 화~토는
최근 24시간, 월요일은 최근 48시간(토요일 이후 공백 포함) 기사만 보여주는 공개 웹앱.
경계는 KST 06:30에 고정(실제 실행 시각이 약간 늦어져도 매일 같은 기준으로 창이 잡힘).

- 수집: 언론사 RSS + 네이버 뉴스 검색 API
- 분류: Claude Haiku 분류 (제목·요약·매체명만 사용, 본문 수집 안 함). 키워드 사전 필터로
  1차 스크리닝 후, Message Batches API(표준가 50%)로 제출하고 90분 내 미완료분만
  일반 API로 재처리. 파싱 실패·응답 절단 시 배치를 절반씩 나눠 재시도(최소 10건까지).
  분류 결과는 `data/classify_cache.json`에 정규화 URL 해시로 캐시(48시간 경과 시 자동
  삭제, relevant true/false 모두 저장, 본문/요약은 저장 안 함). 이 캐시 파일은 git에
  커밋하지 않고 GitHub Actions의 `actions/cache`로 실행 간 보관한다 - 캐시가 없어도
  콜드 스타트로 정상 동작한다.
- 점수: `ax(AX 실질성 10~50) + ulsan_score(0~10) + core(0/1)×10`. 섹션 내 정렬 기준.
- 저장: DB 없음. `public/data.json` 정적 파일 하나
- 화면: 프레임워크 없는 정적 `public/index.html` (좌측 사이드바 + 섹션별 점수순 목록)

## 폴더 구조

```
config/sources.yaml     RSS 목록 + 도메인→매체명 표
config/queries.yaml     네이버 검색어
jobs/collect.py         RSS + 네이버 수집 → candidates.json
jobs/classify.py        키워드 필터 + Haiku 분류(Batches API, 캐시 적용) → classified.json
jobs/build.py           중복 제거·기간 필터·점수 계산 → public/data.json
jobs/dedup.py           동일 사건 병합
jobs/timewindow.py      24h/48h KST 06:30 기준 수집 기간 계산 (단일 정의)
jobs/urlnorm.py         URL 정규화
data/classify_cache.json  분류 결과 캐시 (커밋 안 함 - actions/cache로 보관, .gitignore 처리)
public/index.html       화면
tests/                  pytest
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

이 저장소는 로컬에서 git 커밋까지만 되어 있습니다. 배포까지 아래 순서로 직접 진행해주세요.

1. **GitHub 저장소 생성**: `korea-industry-ax` 이름으로 새 저장소를 만들고 이 폴더를 push.
   ```bash
   git remote add origin <새 저장소 URL>
   git push -u origin main
   ```
2. **GitHub Actions 비밀키 등록**: 저장소 Settings → Secrets and variables → Actions에서 위 표의 세 개 키를 등록.
3. **Vercel 연결**: Vercel 대시보드에서 이 GitHub 저장소를 Import (Git 연동, 자동 배포). Root Directory는 `public/`로 지정. 워크플로에서 Vercel CLI는 쓰지 않으므로 별도 토큰 설정은 필요 없음.
4. push 후 GitHub Actions "Update industry AX briefing" 워크플로가 월~토 KST 06:33에 자동 실행되며, `workflow_dispatch`로 수동 실행도 가능.

## 하지 않는 것

본문 수집·저장, DB, 로그인, 관리자 화면, 기사별 AI 요약, 경제지표, 영상·음성, 장기 아카이브, 임베딩 기반 중복 제거.
