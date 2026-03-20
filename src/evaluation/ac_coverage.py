"""AC coverage completeness check.

Parses acceptance criteria from a StoryContext and verifies that every AC
item has at least one generated test whose ``coverage_tag`` references it.

AC extraction heuristics (ordered by specificity):
  1. Explicit labels   — lines/segments starting with "AC-<n>", "AC <n>",
                         "Criterion <n>", "Acceptance Criterion <n>"
  2. Numbered bullets  — "1.", "1)", "i.", "(a)" prefixes
  3. Dash / asterisk   — markdown list items
  4. Whole block       — if no structure found, treat the entire AC field as
                         one criterion labelled "AC-1"

Matching strategy (coverage_tag → AC):
  A test's coverage_tag is considered to cover an AC when ANY of:
    • exact case-insensitive match between tag and ac_label  (e.g. "AC-1")
    • tag contains the AC label as a substring               (e.g. "AC-1: login")
    • AC label appears anywhere in the tag                   (flexible enough
      for tags like "login-flow-AC1")

Verdict policy:
    coverage_ratio == 1.0  → PASS
    0 < coverage_ratio < 1 → WARN
    total_ac > 0 and 0 AC covered (or story has no AC) → PASS  (can't evaluate)
"""

from __future__ import annotations

import re

from src.models.schemas import (
    AcCoverageItem,
    AcCoverageReport,
    FailureCategory,
    GeneratedTestSuite,
    StoryContext,
    Verdict,
)

# ── AC text extraction from description fallback ─────────────────────────────

# Matches "Acceptance Criteria", "Acceptance Criterion", "AC:" as a section
# header inside a description field, capturing everything after it.
_AC_HEADER_RE = re.compile(
    r"(?:acceptance\s+criteri(?:a|on)|^ac\s*:)\s*\n?(.*)",
    re.I | re.S,
)


def _resolve_ac_text(story: StoryContext) -> str:
    """Return the best available AC text from the story.

    Priority:
      1. ``story.acceptance_criteria`` if non-empty.
      2. Text after an "Acceptance Criteria" header in ``story.description``.
      3. Empty string (no AC available).
    """
    ac = (story.acceptance_criteria or "").strip()
    if ac:
        return ac

    desc = (story.description or "").strip()
    if desc:
        m = _AC_HEADER_RE.search(desc)
        if m:
            return m.group(1).strip()

    return ""

# Matches leading labels like "AC-1", "AC 1", "AC1", "Criterion 2",
# "Acceptance Criterion 3"
_LABEL_RE = re.compile(
    r"^\s*(?P<label>"
    r"AC[-\s]?\d+"
    r"|Criterion\s+\d+"
    r"|Acceptance\s+Criterion\s+\d+"
    r")\s*[:\-–]?\s*",
    re.I,
)

# Numbered list: "1. ", "1) ", "(1) ", "i. ", "(a) "
_NUMBERED_RE = re.compile(
    r"^\s*(?P<label>\(?[0-9ivxIVXa-zA-Z]{1,3}[\)\.]\s*)",
)

# Dash/star/plus markdown list
_BULLET_RE = re.compile(r"^\s*[-*+]\s+")


def _split_ac_text(ac_text: str) -> list[tuple[str, str]]:
    """Return list of (label, text) tuples extracted from raw AC string.

    If no list structure is detected the whole text becomes a single item.
    """
    if not ac_text or not ac_text.strip():
        return []

    lines = [line for line in ac_text.splitlines() if line.strip()]
    items: list[tuple[str, str]] = []

    # ── Fast-path: all lines have explicit AC labels ──────────────────────────
    # If every non-empty line starts with an AC/Criterion label, use that directly.
    if all(_LABEL_RE.match(l) for l in lines):
        for line in lines:
            m = _LABEL_RE.match(line)
            label = m.group("label").strip().upper().replace(" ", "-")
            text = line[m.end():].strip()
            items.append((label, text or line.strip()))
        return items

    # ── Paragraph-block detection ─────────────────────────────────────────────
    # When the AC text contains double-newline separated paragraphs (and none
    # have explicit labels/bullets), each paragraph is a distinct criterion.
    # Split on blank lines first; if we get multiple non-empty blocks, treat
    # each block as a separate AC item.
    raw_paragraphs = [b.strip() for b in re.split(r"\n{2,}", ac_text) if b.strip()]
    if len(raw_paragraphs) > 1:
        # Check whether any paragraph itself starts with an explicit label.
        # If so, fall through to line-by-line parsing so labels are preserved.
        has_any_label = any(_LABEL_RE.match(p) for p in raw_paragraphs)
        if not has_any_label:
            for i, para in enumerate(raw_paragraphs, 1):
                # Collapse internal newlines in multi-line paragraphs
                text = " ".join(l.strip() for l in para.splitlines() if l.strip())
                # Strip leading bullet/number markers within a paragraph
                m_bullet = _BULLET_RE.match(text)
                if m_bullet:
                    text = text[m_bullet.end():].strip()
                else:
                    m_num = _NUMBERED_RE.match(text)
                    if m_num:
                        text = text[m_num.end():].strip()
                items.append((f"AC-{i}", text))
            return items

    # ── Line-by-line parse (bullets, numbers, labels, plain) ─────────────────
    for line in lines:
        # Try explicit AC label first
        m = _LABEL_RE.match(line)
        if m:
            label = m.group("label").strip().upper().replace(" ", "-")
            text = line[m.end():].strip()
            items.append((label, text or line.strip()))
            continue

        # Try numbered list
        m = _NUMBERED_RE.match(line)
        if m:
            raw_label = m.group("label").strip().rstrip(".)")
            auto_label = f"AC-{raw_label}"
            text = line[m.end():].strip()
            items.append((auto_label, text or line.strip()))
            continue

        # Bullet list item — generate sequential label
        if _BULLET_RE.match(line):
            text = _BULLET_RE.sub("", line).strip()
            label = f"AC-{len(items) + 1}"
            items.append((label, text))
            continue

        # Plain non-empty line — treat as continuation or standalone
        if items:
            # Append to previous item's text (multi-line AC)
            prev_label, prev_text = items[-1]
            items[-1] = (prev_label, f"{prev_text} {line.strip()}")
        else:
            items.append(("AC-1", line.strip()))

    if not items and ac_text.strip():
        items = [("AC-1", ac_text.strip())]

    return items


# ── Coverage matching ──────────────────────────────────────────────────────────

def _tag_covers_label(coverage_tag: str, ac_label: str) -> bool:
    """Return True if *coverage_tag* references *ac_label*."""
    tag = coverage_tag.strip().lower()
    label = ac_label.strip().lower()
    if not tag or not label:
        return False
    # Exact match
    if tag == label:
        return True
    # Tag contains label or vice versa as substring
    if label in tag or tag in label:
        return True
    # Normalised: remove hyphens/spaces and compare
    tag_norm = re.sub(r"[\s\-_]", "", tag)
    label_norm = re.sub(r"[\s\-_]", "", label)
    return label_norm in tag_norm


# ── Public API ────────────────────────────────────────────────────────────────

def check_ac_coverage(
    suite: GeneratedTestSuite,
    story: StoryContext,
) -> AcCoverageReport:
    """Check that every AC item in *story* is covered by at least one test.

    Args:
        suite:  The generated test suite to inspect.
        story:  The source StoryContext (acceptance_criteria field is parsed).

    Returns:
        AcCoverageReport with per-AC coverage items and an overall verdict.
    """
    report = AcCoverageReport(story_key=suite.story_key)

    ac_text = _resolve_ac_text(story)
    if not ac_text:
        # No AC to check — cannot evaluate, pass by default
        report.verdict = Verdict.PASS
        report.summary = "No acceptance criteria found in story — coverage check skipped."
        report.coverage_ratio = 1.0
        return report

    ac_pairs = _split_ac_text(ac_text)
    if not ac_pairs:
        report.verdict = Verdict.PASS
        report.summary = "AC field present but could not be parsed into items."
        report.coverage_ratio = 1.0
        return report

    items: list[AcCoverageItem] = []
    for label, text in ac_pairs:
        covering_tests: list[str] = []
        for tc in suite.tests:
            if _tag_covers_label(tc.coverage_tag or "", label):
                covering_tests.append(tc.title)
        items.append(
            AcCoverageItem(
                ac_label=label,
                ac_text=text[:200],  # truncate for storage
                covered=bool(covering_tests),
                covering_tests=covering_tests,
            )
        )

    total = len(items)
    covered = sum(1 for item in items if item.covered)
    uncovered = total - covered
    ratio = covered / total if total else 1.0

    # ── Phantom tag detection ─────────────────────────────────────────────────
    # Flag tests whose coverage_tag references an AC label that doesn't exist
    # in the story (e.g. model hallucinated "AC-4" when only AC-1 exists).
    known_labels = {label.lower() for label, _ in ac_pairs}
    phantom_tags: list[dict] = []
    for tc in suite.tests:
        tag = (tc.coverage_tag or "").strip()
        if not tag:
            continue
        # Only flag tags that look like AC references but don't match any known label
        if re.match(r"^ac[-\s]?\d+", tag, re.I):
            if not any(_tag_covers_label(tag, label) for label in known_labels):
                phantom_tags.append({"test": tc.title, "tag": tag})

    # Verdict — also WARN if any phantom tags detected
    if ratio == 1.0 and not phantom_tags:
        verdict = Verdict.PASS
    elif covered == 0:
        verdict = Verdict.BLOCK
    else:
        verdict = Verdict.WARN

    uncovered_labels = [item.ac_label for item in items if not item.covered]
    summary = (
        f"{covered}/{total} AC item(s) covered "
        f"(ratio {ratio:.0%})"
        + (
            f" — uncovered: {', '.join(uncovered_labels)}"
            if uncovered_labels
            else ""
        )
        + (
            f" — {len(phantom_tags)} test(s) reference phantom AC tag(s): "
            + ", ".join(f"'{p['tag']}'" for p in phantom_tags)
            if phantom_tags
            else ""
        )
    )

    report.total_ac = total
    report.covered_ac = covered
    report.uncovered_ac = uncovered
    report.coverage_ratio = round(ratio, 4)
    report.items = items
    report.phantom_tags = phantom_tags
    report.verdict = verdict
    report.summary = summary
    return report
