"""
public/data.json -> per-article Markdown files in a separate Obsidian vault repo.

Run after the main pipeline (jobs/collect.py -> classify.py -> build.py) has
produced public/data.json. This script only reads that file - it never
touches candidates.json/classified.json or any jobs/ code.

- Reads every item under data["sections"]["1"|"2"|"3"]["items"] (build.py only
  ever publishes grade S/A - see jobs/build.py's is_publishable - but the
  grade is still checked here defensively rather than trusted blindly).
- One .md file per article at:
    AX뉴스/YYYY/YYYY-MM-DD/<sanitized title>_<sha1(url)[:8]>.md
  under --vault-dir (a separate git checkout, not this repo). YYYY/YYYY-MM-DD
  come from the article's own `published` field (already KST, see
  jobs/timewindow.py).
- Skip-if-already-exported is ID-based (the sha1(url)[:8] suffix), not
  content-based: collect_existing_files() walks every .md file already under
  <vault-dir>/AX뉴스 and extracts that suffix, so an article already exported
  on a previous run (even from a different day's data.json - the collection
  window can re-surface the same article) is never written twice, regardless
  of file path or title changes.
- Absorbed-article cleanup (2026-09-30): an article previously exported as
  its own top-level card can later get merged into another article's
  `related` list (jobs/build.py's dedup - e.g. a same-event duplicate that
  a fixed/relaxed merge rule now catches, as happened with the 2026-09-29
  "삼성 6개사 ... 헬릭스" duplication). When that happens, this run's
  data.json no longer carries it as its own item - it's a
  {"source","link"} entry nested under someone else's `related`. Every run
  collects every such related link's id and deletes any existing file whose
  id matches (collect_absorbed_ids()), so the vault never keeps a stale
  standalone card for an article that's since been folded into another
  one's related list. An id absent from data.json entirely (neither
  top-level nor related - e.g. simply aged out of the collection window) is
  left alone; only a confirmed "now it's related elsewhere" is removed.
- EXPORT_MODE env var: "summary" (default) writes the title/source-link/
  summary already in data.json. "full" additionally fetches the article and
  extracts its body text via trafilatura (import is lazy - only needed, and
  only required to be installed, in full mode).
- If $GITHUB_OUTPUT is set (i.e. running as a GitHub Actions step), writes
  created/skipped/latest_published so the workflow can fail the job when
  data.json's newest article is from today but nothing was created - see
  export-vault.yml's "Fail if today's articles weren't exported" step. This
  is what catches a stale checkout (e.g. the 2026-09-28 incident: the workflow
  pinned korea-industry-ax to a commit from before that run's own data
  update, so it kept re-reading 2-day-old data and reported a false "success").
"""
import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from dateutil import parser as dtparser

KST = timezone(timedelta(hours=9))

VAULT_SUBDIR = "AX뉴스"
TITLE_MAX_LEN = 80
# Windows-forbidden filename characters, plus Obsidian-special # ^ [ ] (headings/
# block refs/wikilinks) - all removed from the title before it's used as a filename.
_FORBIDDEN_CHARS = '<>:"/\\|?*#^[]'
_FORBIDDEN_RE = re.compile("[" + re.escape(_FORBIDDEN_CHARS) + "]")
_CONTROL_RE = re.compile(r"[\x00-\x1f]")
_EXISTING_ID_RE = re.compile(r"_([0-9a-f]{8})\.md$")


def article_id(url: str) -> str:
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:8]


def sanitize_title(title: str) -> str:
    t = _CONTROL_RE.sub("", title or "")
    t = _FORBIDDEN_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = t[:TITLE_MAX_LEN].rstrip(" .")  # Windows also disallows a trailing space/dot
    return t or "untitled"


def published_date_parts(published: str):
    """(YYYY, YYYY-MM-DD) from the article's own `published` field, which is
    already KST (see jobs/timewindow.py) - no timezone conversion needed. A
    missing/unparseable value falls back to "now" so a malformed row still
    gets filed somewhere instead of crashing the export."""
    try:
        dt = dtparser.parse(published) if published else None
    except (ValueError, TypeError, OverflowError):
        dt = None
    dt = dt or datetime.now(KST)
    return dt.strftime("%Y"), dt.strftime("%Y-%m-%d")


def latest_published(data: dict) -> str:
    """The most recent `published` value (raw ISO string, already KST) across
    every item in data["sections"] - used by the CI workflow to check whether
    today's articles were actually exported (see main()'s GITHUB_OUTPUT)."""
    latest_dt = None
    latest_raw = None
    for section in data.get("sections", {}).values():
        for item in section.get("items", []):
            raw = item.get("published")
            if not raw:
                continue
            try:
                dt = dtparser.parse(raw)
            except (ValueError, TypeError, OverflowError):
                continue
            if dt.tzinfo is None:
                # A naive value (no offset in the string) would otherwise
                # blow up the `>` comparison below against an aware one
                # (TypeError: can't compare offset-naive and offset-aware
                # datetimes) - every `published` value is meant to be KST
                # already (see jobs/timewindow.py), so that's the safe
                # assumption for the rare naive one.
                dt = dt.replace(tzinfo=KST)
            if latest_dt is None or dt > latest_dt:
                latest_dt = dt
                latest_raw = raw
    return latest_raw


def collect_existing_files(ax_root: Path) -> dict:
    """{id: Path} for every already-exported .md file under ax_root, keyed by
    the sha1(url)[:8] suffix embedded in its filename."""
    files = {}
    if not ax_root.exists():
        return files
    for p in ax_root.rglob("*.md"):
        m = _EXISTING_ID_RE.search(p.name)
        if m:
            files[m.group(1)] = p
    return files


def collect_absorbed_ids(data: dict) -> set:
    """ids of every article that appears in *someone else's* `related` list
    in this data.json - i.e. no longer published as its own top-level card.
    Used to delete any existing file for one of these (see module docstring)."""
    ids = set()
    for section in data.get("sections", {}).values():
        for item in section.get("items", []):
            for r in item.get("related", []) or []:
                link = r.get("link", "")
                if link:
                    ids.add(article_id(link))
    return ids


def build_frontmatter(item: dict, section_label: str) -> str:
    industry = (item.get("industry") or "").strip()
    tags = ["AX뉴스"]
    if section_label:
        tags.append(section_label.replace(" ", "_"))
    if industry:
        tags.append(industry.replace(" ", "_"))
    fm = {
        "title": item.get("title", ""),
        "date": item.get("published", ""),
        "source": item.get("source", ""),
        "section": section_label,
        "grade": item.get("grade", ""),
        "industries": [industry] if industry else [],
        "url": item.get("link", ""),
        "tags": tags,
    }
    return yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)


def extract_full_text(url: str):
    """Lazy-imports trafilatura so summary mode never needs it installed.
    Returns None (not raises) on any fetch/extract failure - one article's
    network hiccup must not fail the whole export."""
    import trafilatura

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None
    return trafilatura.extract(
        downloaded,
        include_images=False,
        include_comments=False,
        include_links=False,
    )


def build_body(item: dict, mode: str) -> str:
    lines = [f"# {item.get('title', '')}", "", f"출처: [{item.get('source', '')}]({item.get('link', '')})", ""]
    if mode == "full":
        text = None
        try:
            text = extract_full_text(item.get("link", ""))
        except Exception as e:  # noqa: BLE001 - one article's extraction failing must not stop the export
            print(f"    full-text extraction failed for {item.get('link')}: {e}")
        if text and text.strip():
            lines.append(text.strip())
        else:
            lines.append("> ⚠️ 본문 추출 실패 - 요약으로 대체")
            lines.append("")
            lines.append(item.get("summary") or "")
    else:
        lines.append(item.get("summary") or "")
    return "\n".join(lines) + "\n"


def export_item(item: dict, section_label: str, ax_root: Path, mode: str) -> Path:
    year, day = published_date_parts(item.get("published"))
    title_part = sanitize_title(item.get("title", ""))
    aid = article_id(item.get("link", ""))
    out_dir = ax_root / year / day
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{title_part}_{aid}.md"

    content = "---\n" + build_frontmatter(item, section_label) + "---\n\n" + build_body(item, mode)
    out_path.write_text(content, encoding="utf-8")
    return out_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="public/data.json", help="Path to this repo's public/data.json")
    parser.add_argument("--vault-dir", default="vault", help="Path to the AX vault repo checkout")
    args = parser.parse_args()

    mode = os.environ.get("EXPORT_MODE", "summary").strip().lower()
    if mode not in ("summary", "full"):
        print(f"Unknown EXPORT_MODE={mode!r} - falling back to summary")
        mode = "summary"

    with open(args.data, "r", encoding="utf-8") as f:
        data = json.load(f)

    latest = latest_published(data)
    print(f"data.json generated_at: {data.get('generated_at', 'N/A')}")
    print(f"data.json latest published article: {latest or 'N/A'}")

    ax_root = Path(args.vault_dir) / VAULT_SUBDIR
    existing_files = collect_existing_files(ax_root)
    existing_ids = set(existing_files.keys())

    removed = 0
    for aid in collect_absorbed_ids(data) & existing_ids:
        path = existing_files[aid]
        path.unlink()
        existing_ids.discard(aid)
        removed += 1
        print(f"  removed (absorbed into another article's related): {path}")

    created = skipped = 0
    for section in data.get("sections", {}).values():
        label = section.get("label", "")
        for item in section.get("items", []):
            if item.get("grade") not in ("S", "A"):
                # Defensive only - build.py never publishes anything else
                # (see jobs/build.py's is_publishable).
                continue
            aid = article_id(item.get("link", ""))
            if aid in existing_ids:
                skipped += 1
                continue
            out_path = export_item(item, label, ax_root, mode)
            existing_ids.add(aid)
            created += 1
            print(f"  wrote {out_path}")

    print(f"Export done (mode={mode}): {created} created, {skipped} skipped (already exported), "
          f"{removed} removed (absorbed into another article's related)")

    # Lets the CI workflow fail loudly instead of a silently-green "success"
    # when this run actually did nothing (see .github/workflows/export-vault.yml's
    # staleness check - this is what caught export-vault.yml reading a stale
    # korea-industry-ax checkout on 2026-09-28: both jobs reported success
    # while actually re-processing 2-day-old data, 0 created).
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"created={created}\n")
            f.write(f"skipped={skipped}\n")
            f.write(f"removed={removed}\n")
            f.write(f"latest_published={latest or ''}\n")


if __name__ == "__main__":
    main()
