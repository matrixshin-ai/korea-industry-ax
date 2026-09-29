import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import export_to_vault as evt


def test_sanitize_title_strips_windows_and_obsidian_special_chars():
    assert evt.sanitize_title('제목: "AI" [특집] #1 <속보> ^메모') == "제목 AI 특집 1 속보 메모"


def test_sanitize_title_truncates_to_80_chars_and_no_trailing_dot_or_space():
    long_title = "가" * 90 + ". "
    result = evt.sanitize_title(long_title)
    assert len(result) <= evt.TITLE_MAX_LEN
    assert not result.endswith(".")
    assert not result.endswith(" ")


def test_sanitize_title_empty_falls_back_to_untitled():
    assert evt.sanitize_title("") == "untitled"
    assert evt.sanitize_title(None) == "untitled"


def test_article_id_stable_and_8_chars():
    a = evt.article_id("https://example.com/a/1")
    b = evt.article_id("https://example.com/a/1")
    c = evt.article_id("https://example.com/a/2")
    assert a == b
    assert a != c
    assert len(a) == 8


def test_published_date_parts_uses_articles_own_kst_timestamp():
    year, day = evt.published_date_parts("2026-09-26T10:02:00+09:00")
    assert year == "2026"
    assert day == "2026-09-26"


def test_published_date_parts_missing_value_falls_back_to_now():
    year, day = evt.published_date_parts(None)
    assert len(year) == 4
    assert len(day) == 10


def test_collect_existing_files_finds_ids_from_nested_files(tmp_path):
    ax_root = tmp_path / "AX뉴스"
    (ax_root / "2026" / "2026-09-26").mkdir(parents=True)
    p1 = ax_root / "2026" / "2026-09-26" / "기사_a6046ebe.md"
    p1.write_text("x", encoding="utf-8")
    (ax_root / "2026" / "2026-09-25").mkdir(parents=True, exist_ok=True)
    p2 = ax_root / "2026" / "2026-09-25" / "다른기사_14e0c0d1.md"
    p2.write_text("y", encoding="utf-8")
    result = evt.collect_existing_files(ax_root)
    assert result == {"a6046ebe": p1, "14e0c0d1": p2}


def test_collect_existing_files_missing_dir_returns_empty_dict():
    assert evt.collect_existing_files(Path("/does/not/exist")) == {}


def test_collect_absorbed_ids_gathers_every_related_links_id():
    data = {"sections": {
        "1": {"items": [
            {"link": "https://a.example.com/1", "related": [
                {"source": "매체", "link": "https://b.example.com/2"},
                {"source": "매체", "link": "https://c.example.com/3"},
            ]},
        ]},
        "2": {"items": [
            {"link": "https://d.example.com/4", "related": []},
        ]},
    }}
    result = evt.collect_absorbed_ids(data)
    assert result == {evt.article_id("https://b.example.com/2"), evt.article_id("https://c.example.com/3")}
    # The top-level representative's own id, and an id from an item with no
    # related, must not be swept in.
    assert evt.article_id("https://a.example.com/1") not in result
    assert evt.article_id("https://d.example.com/4") not in result


def test_collect_absorbed_ids_no_related_anywhere_returns_empty_set():
    data = {"sections": {"1": {"items": [{"link": "https://a.example.com/1", "related": []}]}}}
    assert evt.collect_absorbed_ids(data) == set()


def test_build_frontmatter_includes_requested_fields_and_replaces_spaces_in_tags():
    item = {"title": "제목", "published": "2026-09-26T10:00:00+09:00", "source": "매체",
             "grade": "A", "industry": "IT 통신", "link": "https://example.com/1"}
    fm_text = evt.build_frontmatter(item, "기업 현장")
    import yaml
    fm = yaml.safe_load(fm_text)
    assert fm["title"] == "제목"
    assert fm["date"] == "2026-09-26T10:00:00+09:00"
    assert fm["source"] == "매체"
    assert fm["section"] == "기업 현장"
    assert fm["grade"] == "A"
    assert fm["industries"] == ["IT 통신"]
    assert fm["url"] == "https://example.com/1"
    assert fm["tags"] == ["AX뉴스", "기업_현장", "IT_통신"]


def test_build_frontmatter_empty_industry_yields_empty_industries_and_no_extra_tag():
    item = {"title": "t", "published": "", "source": "s", "grade": "S", "industry": "", "link": "l"}
    fm_text = evt.build_frontmatter(item, "정책")
    import yaml
    fm = yaml.safe_load(fm_text)
    assert fm["industries"] == []
    assert fm["tags"] == ["AX뉴스", "정책"]


def test_build_body_summary_mode_uses_title_source_link_and_summary():
    item = {"title": "제목", "source": "매체", "link": "https://example.com/1", "summary": "요약 내용"}
    body = evt.build_body(item, "summary")
    assert body.startswith("# 제목")
    assert "[매체](https://example.com/1)" in body
    assert "요약 내용" in body


def test_build_body_full_mode_falls_back_to_summary_when_extraction_fails(monkeypatch):
    def fake_extract(url):
        return None
    monkeypatch.setattr(evt, "extract_full_text", fake_extract)
    item = {"title": "제목", "source": "매체", "link": "https://example.com/1", "summary": "요약"}
    body = evt.build_body(item, "full")
    assert "본문 추출 실패" in body
    assert "요약" in body


def test_build_body_full_mode_uses_extracted_text_when_available(monkeypatch):
    monkeypatch.setattr(evt, "extract_full_text", lambda url: "전체 본문 텍스트")
    item = {"title": "제목", "source": "매체", "link": "https://example.com/1", "summary": "요약"}
    body = evt.build_body(item, "full")
    assert "전체 본문 텍스트" in body
    assert "본문 추출 실패" not in body


def test_export_item_writes_expected_path(tmp_path):
    ax_root = tmp_path / "AX뉴스"
    item = {"title": "테스트 기사", "published": "2026-09-26T10:00:00+09:00", "source": "매체",
             "grade": "S", "industry": "조선", "link": "https://example.com/1", "summary": "요약"}
    out_path = evt.export_item(item, "기업·현장", ax_root, "summary")
    assert out_path.exists()
    assert out_path.parent == ax_root / "2026" / "2026-09-26"
    assert out_path.name.startswith("테스트 기사_")
    assert out_path.name.endswith(".md")


def test_main_skips_items_not_graded_s_or_a_and_writes_only_publishable(tmp_path, monkeypatch, capsys):
    data = {
        "sections": {
            "1": {"label": "기업·현장", "items": [
                {"title": "S등급", "published": "2026-09-26T10:00:00+09:00", "source": "매체",
                 "grade": "S", "industry": "조선", "link": "https://example.com/1", "summary": "요약1"},
                {"title": "B등급(원래 게시 안 됨)", "published": "2026-09-26T10:00:00+09:00", "source": "매체",
                 "grade": "B", "industry": "조선", "link": "https://example.com/2", "summary": "요약2"},
            ]},
            "2": {"label": "기술·인프라", "items": []},
            "3": {"label": "정책·생태계·인재", "items": []},
        }
    }
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    vault_dir = tmp_path / "vault"

    monkeypatch.setattr(sys, "argv", ["export_to_vault.py", "--data", str(data_path), "--vault-dir", str(vault_dir)])
    monkeypatch.delenv("EXPORT_MODE", raising=False)
    evt.main()

    written = list((vault_dir / "AX뉴스").rglob("*.md"))
    assert len(written) == 1
    assert "S등급" in written[0].name
    out = capsys.readouterr().out
    assert "1 created, 0 skipped" in out


def test_main_removes_file_for_article_absorbed_into_related(tmp_path, monkeypatch, capsys):
    # The article at https://b.example.com/2 was exported on a previous run
    # as its own card; today's data.json shows it folded into
    # https://a.example.com/1's `related` (e.g. a merge-rule fix caught it
    # after the fact) - its old file must be deleted, not left stale.
    absorbed_link = "https://b.example.com/2"
    absorbed_id = evt.article_id(absorbed_link)
    data = {
        "sections": {
            "1": {"label": "기업·현장", "items": [
                {"title": "대표 기사", "published": "2026-09-30T09:00:00+09:00", "source": "매체A",
                 "grade": "A", "industry": "조선", "link": "https://a.example.com/1", "summary": "요약",
                 "related": [{"source": "매체B", "link": absorbed_link}]},
            ]},
        }
    }
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    vault_dir = tmp_path / "vault"
    stale_dir = vault_dir / "AX뉴스" / "2026" / "2026-09-29"
    stale_dir.mkdir(parents=True)
    stale_path = stale_dir / f"흡수될 기사_{absorbed_id}.md"
    stale_path.write_text("old content", encoding="utf-8")
    # An unrelated existing file (different id) must survive untouched.
    keep_path = stale_dir / "무관한 기사_ffffffff.md"
    keep_path.write_text("keep me", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["export_to_vault.py", "--data", str(data_path), "--vault-dir", str(vault_dir)])
    monkeypatch.delenv("EXPORT_MODE", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    evt.main()

    assert not stale_path.exists()
    assert keep_path.exists()
    out = capsys.readouterr().out
    assert "1 removed" in out
    assert "removed (absorbed into another article's related)" in out


def test_main_writes_removed_count_to_github_output(tmp_path, monkeypatch):
    absorbed_link = "https://b.example.com/2"
    absorbed_id = evt.article_id(absorbed_link)
    data = {"sections": {"1": {"label": "기업·현장", "items": [
        {"title": "대표 기사", "published": "2026-09-30T09:00:00+09:00", "source": "매체A",
         "grade": "A", "industry": "조선", "link": "https://a.example.com/1", "summary": "요약",
         "related": [{"source": "매체B", "link": absorbed_link}]},
    ]}}}
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    vault_dir = tmp_path / "vault"
    stale_dir = vault_dir / "AX뉴스" / "2026" / "2026-09-29"
    stale_dir.mkdir(parents=True)
    (stale_dir / f"흡수될 기사_{absorbed_id}.md").write_text("old", encoding="utf-8")
    output_path = tmp_path / "gh_output.txt"

    monkeypatch.setattr(sys, "argv", ["export_to_vault.py", "--data", str(data_path), "--vault-dir", str(vault_dir)])
    monkeypatch.delenv("EXPORT_MODE", raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    evt.main()

    assert "removed=1" in output_path.read_text(encoding="utf-8")


def test_main_leaves_files_alone_when_id_not_in_data_json_at_all(tmp_path, monkeypatch):
    # An id that's neither top-level nor related in today's data.json (e.g.
    # it simply aged out of the collection window) must be left untouched -
    # only a confirmed "now related elsewhere" triggers deletion.
    data = {"sections": {"1": {"label": "기업·현장", "items": []}}}
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(data), encoding="utf-8")
    vault_dir = tmp_path / "vault"
    stale_dir = vault_dir / "AX뉴스" / "2026" / "2026-09-20"
    stale_dir.mkdir(parents=True)
    old_path = stale_dir / "오래된 기사_deadbeef.md"
    old_path.write_text("old", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["export_to_vault.py", "--data", str(data_path), "--vault-dir", str(vault_dir)])
    monkeypatch.delenv("EXPORT_MODE", raising=False)
    evt.main()

    assert old_path.exists()


def test_main_unknown_export_mode_falls_back_to_summary(tmp_path, monkeypatch, capsys):
    data = {"sections": {"1": {"label": "기업·현장", "items": []}}}
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["export_to_vault.py", "--data", str(data_path), "--vault-dir", str(tmp_path / "vault")])
    monkeypatch.setenv("EXPORT_MODE", "nonsense")
    evt.main()
    out = capsys.readouterr().out
    assert "falling back to summary" in out
    assert "mode=summary" in out


def test_latest_published_picks_max_across_all_sections():
    data = {
        "sections": {
            "1": {"items": [{"published": "2026-09-26T10:00:00+09:00"}]},
            "2": {"items": [{"published": "2026-09-28T09:00:00+09:00"},
                             {"published": "2026-09-27T08:00:00+09:00"}]},
        }
    }
    assert evt.latest_published(data) == "2026-09-28T09:00:00+09:00"


def test_latest_published_ignores_missing_or_unparseable_values():
    data = {"sections": {"1": {"items": [
        {"published": None}, {}, {"published": "not-a-date"},
        {"published": "2026-09-27T08:00:00+09:00"},
    ]}}}
    assert evt.latest_published(data) == "2026-09-27T08:00:00+09:00"


def test_latest_published_no_items_returns_none():
    assert evt.latest_published({"sections": {"1": {"items": []}}}) is None


def test_latest_published_handles_mixed_naive_and_aware_timestamps():
    # A `published` value with no UTC offset (dateutil parses it as naive) must
    # not crash the comparison against an aware one - this is what broke the
    # 2026-09-28 verification run (TypeError: can't compare offset-naive and
    # offset-aware datetimes), coming from a real RSS feed's date format.
    data = {"sections": {"1": {"items": [
        {"published": "2026-09-27T08:00:00"},  # naive - no offset
        {"published": "2026-09-28T09:00:00+09:00"},  # aware
    ]}}}
    assert evt.latest_published(data) == "2026-09-28T09:00:00+09:00"


def test_latest_published_naive_timestamp_alone_is_still_returned():
    data = {"sections": {"1": {"items": [{"published": "2026-09-27T08:00:00"}]}}}
    assert evt.latest_published(data) == "2026-09-27T08:00:00"


def test_main_writes_github_output_when_env_var_set(tmp_path, monkeypatch):
    data = {"sections": {"1": {"label": "기업·현장", "items": [
        {"title": "기사", "published": "2026-09-28T09:00:00+09:00", "source": "매체",
         "grade": "S", "industry": "조선", "link": "https://example.com/1", "summary": "요약"},
    ]}}}
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(data), encoding="utf-8")
    output_path = tmp_path / "gh_output.txt"

    monkeypatch.setattr(sys, "argv", ["export_to_vault.py", "--data", str(data_path), "--vault-dir", str(tmp_path / "vault")])
    monkeypatch.delenv("EXPORT_MODE", raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    evt.main()

    content = output_path.read_text(encoding="utf-8")
    assert "created=1" in content
    assert "skipped=0" in content
    assert "latest_published=2026-09-28T09:00:00+09:00" in content


def test_main_no_github_output_env_does_not_create_file(tmp_path, monkeypatch):
    data = {"sections": {"1": {"label": "기업·현장", "items": []}}}
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["export_to_vault.py", "--data", str(data_path), "--vault-dir", str(tmp_path / "vault")])
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    evt.main()  # must not raise despite no GITHUB_OUTPUT being set (local/manual runs)
