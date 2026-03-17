"""Full offline evaluation pipeline.

Combines two fast, synchronous evaluation layers:

  1. Gate (decision policy)   — src/evaluation/gate.py
     Applies explicit pass/warn/block rules for each known failure category.

  2. Duplication detection    — src/evaluation/duplication.py
     Flags near-duplicates against historical context and intra-suite twins.

Both layers are merged into a PipelineResult per suite.

Called by scripts/run_eval.py — never imported by the live API.

Usage:
    from src.evaluation.pipeline import run_pipeline

    results = run_pipeline(suites, stories, historical_items_map)
    for r in results:
        print(r["story_key"], r["suite_verdict"], r["duplication_summary"])
"""

from __future__ import annotations

from src.evaluation.duplication import DuplicationReport, detect_duplicates
from src.evaluation.gate import SuiteGateReport
from src.evaluation.gate import evaluate_suite as gate_evaluate_suite
from src.models.schemas import (
    ContextItem,
    GeneratedTestSuite,
    StoryContext,
    Verdict,
)


def run_pipeline(
    suites: list[GeneratedTestSuite],
    stories: list[StoryContext] | None = None,
    historical_items_map: dict[str, list[ContextItem]] | None = None,
    *,
    dup_threshold: float       = 0.55,
    intra_dup_threshold: float = 0.70,
) -> list[dict]:
    """Run gate + duplication evaluation over a batch of test suites.

    Args:
        suites:               List of generated test suites to evaluate.
        stories:              Corresponding StoryContext objects (matched by
                              suite.story_key == story.issue_key).  If None
                              or a story is missing, a blank StoryContext is
                              used (triggers should_refuse_generated for all tests).
        historical_items_map: Mapping of story_key → list[ContextItem] from
                              retrieval.  Used by both gate and duplication checks.
        dup_threshold:        Jaccard threshold for history duplicate (BLOCK).
        intra_dup_threshold:  Jaccard threshold for intra-suite duplicate (WARN).

    Returns:
        List of result dicts, one per suite, with keys:
            story_key, suite_verdict, gate_report, dup_report,
            pass_count, warn_count, block_count,
            gate_summary, duplication_summary
    """
    # Build story lookup
    story_lookup: dict[str, StoryContext] = {}
    for s in (stories or []):
        story_lookup[s.issue_key] = s

    results: list[dict] = []

    for suite in suites:
        story = story_lookup.get(suite.story_key) or StoryContext(
            issue_key=suite.story_key,
            summary="(unknown)",
        )
        hist_items = (historical_items_map or {}).get(suite.story_key, [])

        # ── Layer 1: decision-policy gate ─────────────────────────────────────
        gate_report: SuiteGateReport = gate_evaluate_suite(
            suite,
            story,
            hist_items,
            dup_threshold=dup_threshold,
            intra_dup_threshold=intra_dup_threshold,
        )

        # ── Layer 2: standalone duplication detection ─────────────────────────
        dup_report: DuplicationReport = detect_duplicates(
            suite,
            hist_items,
            history_threshold=dup_threshold,
            intra_threshold=intra_dup_threshold,
        )

        # ── Merge verdicts: worst across gate + dup ───────────────────────────
        _severity = {Verdict.PASS: 0, Verdict.WARN: 1, Verdict.BLOCK: 2}
        dup_verdict = (
            Verdict.BLOCK if dup_report.block_count
            else Verdict.WARN  if dup_report.warn_count
            else Verdict.PASS
        )
        merged_verdict = max(
            gate_report.suite_verdict,
            dup_verdict,
            key=lambda v: _severity[v],
        )

        results.append({
            "story_key":           suite.story_key,
            "suite_verdict":       merged_verdict.value,
            "gate_report":         gate_report,
            "dup_report":          dup_report,
            "pass_count":          gate_report.pass_count,
            "warn_count":          gate_report.warn_count,
            "block_count":         gate_report.block_count,
            "gate_summary":        gate_report.summary,
            "duplication_summary": dup_report.summary(),
            # Flat per-case breakdown for downstream scripts / scripts/run_eval.py
            "cases": [
                {
                    "title":         cr.title,
                    "verdict":       cr.verdict.value,
                    "failures":      [f.value for f in cr.failures],
                    "reasons":       cr.reasons,
                    "dup_of":        next(
                        (d.duplicate_of for d in dup_report.case_reports if d.title == cr.title),
                        None,
                    ),
                    "dup_similarity": next(
                        (d.similarity for d in dup_report.case_reports if d.title == cr.title),
                        0.0,
                    ),
                }
                for cr in gate_report.case_results
            ],
        })

    return results
