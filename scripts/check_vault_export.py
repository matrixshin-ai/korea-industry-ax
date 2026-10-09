"""
ax-vault-full export 감시: 최근 2 수집일 연속으로 export가 0건이거나 실패하면 GitHub Issue를 연다.

배경 (2026-10-10): "vault로 전송이 안 됐다"는 의심이 들었을 때 사람이 run 목록과 로그를
하나씩 열어 봐야만 확인할 수 있었다. 이 스크립트가 그 점검을 매일 자동으로 한다.

판정
- 수집일 = 월~토 (KST). 일요일은 update.yml이 실행되지 않으므로 판정에서 뺀다.
- 하루가 "정상"이려면 그날(KST) 시작된 "Export published articles to AX vault" 실행 중
  export-full job 로그에 "Export done (mode=full): N created"가 있고 N > 0인 실행이 하나
  이상 있어야 한다. 실패·skipped(앞단 update 실패)·0건·실행 없음은 모두 "비정상"이다.
- 최근 2 수집일이 모두 비정상이면 알림. 오늘은 KST TODAY_CUTOFF_HOUR시 이후이거나 이미
  정상 실행이 있을 때만 판정에 넣는다 (아침 실행이 아직 안 끝났을 수 있으므로).
- 알림: 'vault-export-alert' 라벨이 붙은 열린 Issue가 있으면 댓글을 달고, 없으면 새로 연다.
  정상으로 돌아오면 열린 알림 Issue에 복구 댓글을 달고 닫는다.

실행: GitHub Actions(.github/workflows/vault-export-monitor.yml)에서 GH_TOKEN으로 gh CLI 사용.
로컬 확인: python scripts/check_vault_export.py --dry-run  (Issue를 만들지 않고 판정만 출력)
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
EXPORT_WORKFLOW = "export-vault.yml"
ALERT_LABEL = "vault-export-alert"
CONSECUTIVE_DAYS = 2
TODAY_CUTOFF_HOUR = 14
LOOKBACK_DAYS = 8
COLLECTION_WEEKDAYS = {0, 1, 2, 3, 4, 5}  # Mon-Sat (update.yml cron 33 21 * * 0-5 UTC = 월~토 KST 06:33)

_CREATED_RE = re.compile(r"Export done \(mode=full\): (\d+) created")


def gh(*args, repo=None) -> str:
    cmd = ["gh", *args] + (["-R", repo] if repo else [])
    return subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8").stdout


def parse_created(log_text: str):
    """export-full job 로그에서 created 건수. 없으면 None (job이 그 단계까지 못 감)."""
    for line in log_text.splitlines():
        if not line.startswith("export-full"):
            continue
        m = _CREATED_RE.search(line)
        if m:
            return int(m.group(1))
    return None


def fetch_runs(repo: str, now: datetime) -> list:
    """최근 LOOKBACK_DAYS일의 export 실행 [{id, kst_date, event, conclusion, created}]."""
    raw = gh("run", "list", "--workflow", EXPORT_WORKFLOW, "--limit", "40",
             "--json", "databaseId,createdAt,event,status,conclusion", repo=repo)
    since = now - timedelta(days=LOOKBACK_DAYS)
    runs = []
    for r in json.loads(raw):
        started = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")).astimezone(KST)
        if started < since or r["status"] != "completed":
            continue
        created = None
        if r["conclusion"] in ("success", "failure"):
            try:
                created = parse_created(gh("run", "view", str(r["databaseId"]), "--log", repo=repo))
            except subprocess.CalledProcessError:
                created = None  # 로그 만료·권한 문제 - 0건과 같게 취급
        runs.append({"id": r["databaseId"], "kst_date": started.date().isoformat(),
                     "event": r["event"], "conclusion": r["conclusion"], "created": created})
    return runs


def day_status(runs: list) -> dict:
    """{kst_date: True(정상)/False(비정상)} - 실행이 있었던 날만."""
    status = {}
    for r in runs:
        ok = r["conclusion"] == "success" and (r["created"] or 0) > 0
        status[r["kst_date"]] = status.get(r["kst_date"], False) or ok
    return status


def days_to_judge(now: datetime, status: dict) -> list:
    """판정 대상 수집일(최신순) CONSECUTIVE_DAYS개."""
    days = []
    d = now.date()
    include_today = now.hour >= TODAY_CUTOFF_HOUR or status.get(d.isoformat(), False)
    if not include_today:
        d -= timedelta(days=1)
    while len(days) < CONSECUTIVE_DAYS:
        if d.weekday() in COLLECTION_WEEKDAYS:
            days.append(d.isoformat())
        d -= timedelta(days=1)
    return days


def evaluate(runs: list, now: datetime):
    """(alert: bool, judged_days, status)"""
    status = day_status(runs)
    judged = days_to_judge(now, status)
    alert = all(not status.get(day, False) for day in judged)
    return alert, judged, status


def report_body(runs: list, judged: list, status: dict, server: str, repo: str) -> str:
    lines = [f"최근 {CONSECUTIVE_DAYS} 수집일({', '.join(sorted(judged))}) 연속으로 ax-vault-full export가 "
             "0건이거나 실패했습니다.", "",
             "| KST 날짜 | run | event | 결과 | created |", "|---|---|---|---|---:|"]
    for r in sorted(runs, key=lambda r: (r["kst_date"], r["id"]), reverse=True):
        url = f"{server}/{repo}/actions/runs/{r['id']}"
        created = "-" if r["created"] is None else r["created"]
        lines.append(f"| {r['kst_date']} | [{r['id']}]({url}) | {r['event']} | {r['conclusion']} | {created} |")
    if not runs:
        lines.append("| - | (최근 실행 없음) | | | |")
    lines += ["", "점검 순서: CLAUDE.md의 'Vault export' 절 참고 (update.yml 실패 → export skipped, "
              "stale checkout, deploy key, vault 저장소 상태).",
              "", f"_이 Issue는 `.github/workflows/vault-export-monitor.yml`이 자동으로 만들었습니다._"]
    return "\n".join(lines)


def open_alert_issues(repo: str) -> list:
    raw = gh("issue", "list", "--label", ALERT_LABEL, "--state", "open", "--json", "number", repo=repo)
    return [i["number"] for i in json.loads(raw)]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "matrixshin-ai/korea-industry-ax"))
    parser.add_argument("--dry-run", action="store_true", help="Issue를 만들거나 닫지 않고 판정만 출력")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    now = datetime.now(KST)
    runs = fetch_runs(args.repo, now)
    alert, judged, status = evaluate(runs, now)
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")

    print(f"now (KST): {now:%Y-%m-%d %H:%M}")
    for day in sorted(status):
        print(f"  {day}: {'정상' if status[day] else '비정상'}  "
              f"runs={[(r['id'], r['conclusion'], r['created']) for r in runs if r['kst_date'] == day]}")
    print(f"판정 대상 수집일: {judged} -> {'알림' if alert else '정상'}")

    if args.dry_run:
        print("--dry-run: Issue를 만들거나 닫지 않았습니다.")
        return

    existing = open_alert_issues(args.repo)
    if alert:
        body = report_body(runs, judged, status, server, args.repo)
        if existing:
            gh("issue", "comment", str(existing[0]), "--body", body, repo=args.repo)
            print(f"열린 알림 Issue #{existing[0]}에 댓글을 달았습니다.")
        else:
            subprocess.run(["gh", "label", "create", ALERT_LABEL, "--color", "D93F0B",
                            "--description", "ax-vault-full export 감시 알림", "-R", args.repo],
                           capture_output=True)  # 이미 있으면 실패해도 무시
            title = f"[자동] ax-vault-full export {CONSECUTIVE_DAYS}일 연속 0건/실패 ({max(judged)})"
            url = gh("issue", "create", "--title", title, "--body", body, "--label", ALERT_LABEL,
                     repo=args.repo).strip()
            print(f"알림 Issue를 만들었습니다: {url}")
    elif existing:
        for number in existing:
            gh("issue", "close", str(number), "--comment",
               f"최근 수집일({', '.join(sorted(judged))}) export가 정상으로 확인되어 자동으로 닫습니다.",
               repo=args.repo)
            print(f"복구 확인 - 알림 Issue #{number}을 닫았습니다.")


if __name__ == "__main__":
    main()
