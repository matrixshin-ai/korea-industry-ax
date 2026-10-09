import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_vault_export as cve


def kst(y, m, d, h=15):
    return datetime(y, m, d, h, 0, tzinfo=cve.KST)


def run(day, conclusion="success", created=10, rid=1, event="workflow_run"):
    return {"id": rid, "kst_date": day, "event": event, "conclusion": conclusion, "created": created}


def test_parse_created_reads_only_export_full_job():
    log = ("export-summary\tExport\t2026-10-09T01:33Z Export done (mode=summary): 5 created, 0 skipped\n"
           "export-full\tExport\t2026-10-09T01:33Z Export done (mode=full): 78 created, 27 skipped (already exported)\n")
    assert cve.parse_created(log) == 78


def test_parse_created_missing_returns_none():
    assert cve.parse_created("export-full\tCheckout\tThe process '/usr/bin/git' failed with exit code 2\n") is None


def test_two_healthy_days_no_alert():
    # 2026-10-09 is a Friday
    alert, judged, _ = cve.evaluate([run("2026-10-09"), run("2026-10-08")], kst(2026, 10, 9))
    assert not alert
    assert judged == ["2026-10-09", "2026-10-08"]


def test_two_consecutive_zero_or_failed_days_alert():
    runs = [run("2026-10-09", created=0), run("2026-10-08", conclusion="failure", created=None)]
    alert, _, _ = cve.evaluate(runs, kst(2026, 10, 9))
    assert alert


def test_skipped_and_missing_days_count_as_bad():
    # update.yml failed on 10-09 (export skipped), nothing ran on 10-08
    alert, _, _ = cve.evaluate([run("2026-10-09", conclusion="skipped", created=None)], kst(2026, 10, 9))
    assert alert


def test_only_one_bad_day_no_alert():
    alert, _, _ = cve.evaluate([run("2026-10-09", created=0), run("2026-10-08")], kst(2026, 10, 9))
    assert not alert


def test_same_day_rerun_with_files_makes_day_healthy():
    # 2026-09-28 incident: morning run created 0 (stale checkout), manual rerun created 84
    runs = [run("2026-09-28", created=0, rid=1), run("2026-09-28", created=84, rid=2, event="workflow_dispatch"),
            run("2026-09-27", created=0, rid=3)]
    alert, _, status = cve.evaluate(runs, kst(2026, 9, 28))
    assert status["2026-09-28"] is True
    assert not alert


def test_sunday_is_skipped_when_judging():
    # 2026-10-05 is Monday: judged days are Mon 10-05 and Sat 10-03 (Sun 10-04 has no collection)
    alert, judged, _ = cve.evaluate([run("2026-10-05"), run("2026-10-03")], kst(2026, 10, 5))
    assert judged == ["2026-10-05", "2026-10-03"]
    assert not alert


def test_today_excluded_before_cutoff_when_not_yet_exported():
    # 08:00 KST Friday - today's run may still be pending, so judge Thu + Wed instead
    alert, judged, _ = cve.evaluate([run("2026-10-08"), run("2026-10-07")], kst(2026, 10, 9, 8))
    assert judged == ["2026-10-08", "2026-10-07"]
    assert not alert


def test_today_included_before_cutoff_once_it_already_succeeded():
    _, judged, _ = cve.evaluate([run("2026-10-09"), run("2026-10-08")], kst(2026, 10, 9, 11))
    assert judged[0] == "2026-10-09"
