"""Offline evaluation runner.

Usage:
    python scripts/run_eval.py

Loads sample test suites and runs the full deepeval pipeline.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evaluation.pipeline import run_pipeline
from src.models.schemas import GeneratedTestCase, GeneratedTestSuite, Priority, CaseType


def main() -> None:
    # TODO: replace with real suites loaded from data/ or a database
    sample = [
        GeneratedTestSuite(
            story_key="PROJ-1",
            tests=[
                GeneratedTestCase(
                    title="User can log in with valid credentials",
                    preconditions=["User is registered", "User is on the login page"],
                    steps=[
                        "Enter a valid email address",
                        "Enter the correct password",
                        "Click the Login button",
                    ],
                    expected_result="User is redirected to the dashboard",
                    priority=Priority.HIGH,
                    test_type=CaseType.FUNCTIONAL,
                    coverage_tag="AC-1",
                    source_story="PROJ-1",
                )
            ],
            notes="Generated from sample data.",
        )
    ]

    summary = run_pipeline(sample)
    print(summary)


if __name__ == "__main__":
    main()
