# Evaluation — shared evaluation logic.
#
# Four modules:
#   gate.py        — decision-policy gate: per-case pass/warn/block rules
#   duplication.py — token-Jaccard duplicate detection (history + intra-suite)
#   pipeline.py    — offline pipeline wiring both layers together
#   input_guard.py — pre-generation input quality guard (reject weak inputs)

from src.evaluation.gate import (
    DECISION_POLICY,
    CaseGateResult,
    SuiteGateReport,
    Verdict,
    evaluate_case,
    evaluate_suite,
    passes_gate,
)
from src.evaluation.duplication import (
    CaseDupResult,
    DuplicationReport,
    detect_duplicates,
    jaccard,
)
from src.evaluation.input_guard import (
    InputRejectedError,
    check_input,
)
from src.evaluation.pipeline import run_pipeline

__all__ = [
    # gate
    "DECISION_POLICY",
    "CaseGateResult",
    "SuiteGateReport",
    "Verdict",
    "evaluate_case",
    "evaluate_suite",
    "passes_gate",
    # duplication
    "CaseDupResult",
    "DuplicationReport",
    "detect_duplicates",
    "jaccard",
    # input guard
    "InputRejectedError",
    "check_input",
    # pipeline
    "run_pipeline",
]
