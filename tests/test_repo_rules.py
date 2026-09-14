"""Repo hygiene rules that are cheap to enforce mechanically rather than by review.

No em dashes in authored text or artifacts (evidence is exempt); no stable selectors in markup.
"""

from __future__ import annotations

import ast
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


def test_only_the_gated_surface_calls_surface_act() -> None:
    """The policy gate is the only path to Surface.act (kickoff test matrix, policy row).

    Walks the syntax tree, so aliases and getattr(obj, "act") count too, and raw element handles
    may only be touched inside the surface package that produced them.
    """
    enforce = ROOT / "src" / "cua" / "policy" / "enforce.py"
    surface_dir = ROOT / "src" / "cua" / "surface"
    offenders = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            where = f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', 0)}"
            names_act = (isinstance(node, ast.Attribute) and node.attr == "act") or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and any(isinstance(a, ast.Constant) and a.value == "act" for a in node.args)
            )
            if names_act and path != enforce:
                offenders.append(f"act at {where}")
            touches_handle = isinstance(node, ast.Attribute) and node.attr == "handle"
            if touches_handle and surface_dir not in path.parents:
                offenders.append(f"element handle at {where}")
    assert not offenders, f"gate bypass: {offenders}"


def test_replay_never_imports_a_model_client() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "src" / "cua" / "replay").rglob("*.py"))
        if re.search(r"^\s*(import|from)\s+(anthropic|cua\.discover)", path.read_text(), re.M)
    ]
    assert not offenders
