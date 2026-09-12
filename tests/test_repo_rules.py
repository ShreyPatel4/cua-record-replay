"""Repo hygiene rules that are cheap to enforce mechanically rather than by review.

No em dashes in authored text or artifacts (evidence is exempt); no stable selectors in markup.
"""

from __future__ import annotations

import re

from support import ROOT, candidate_files

TEXT_SUFFIXES = {".md", ".py", ".html", ".yaml", ".yml", ".toml", ".json", ".txt", ".cfg"}
EM_DASH = "\u2014"
# Evidence is recorded app output; rewriting it to satisfy a style rule is tampering. Artifacts
# are reviewed documents, so the recorder normalizes their prose and the rule applies to them.
RECORDED_DIRS = {"evidence"}


def test_no_em_dashes_in_human_readable_files() -> None:
    offenders = [
        str(p.relative_to(ROOT))
        for p in candidate_files()
        if (p.suffix in TEXT_SUFFIXES or p.name == ".env.example")
        and not (RECORDED_DIRS & set(p.relative_to(ROOT).parts[:1]))
        and EM_DASH in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert not offenders, f"em dashes found in: {offenders}"


def test_mock_templates_carry_no_stable_selectors() -> None:
    attr = re.compile(r"""\s(id|data-testid|data-test|data-qa)\s*=""", re.IGNORECASE)
    templates = sorted((ROOT / "mock_app" / "templates").glob("*.html"))
    assert templates
    offenders = [t.name for t in templates if attr.search(t.read_text())]
    assert not offenders, f"stable selector attributes in: {offenders}"
