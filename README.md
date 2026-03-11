# RAG AI Eval Testcase Generator

This project turns Jira story data into structured context for AI-generated test cases, then validates those outputs with lightweight gates and an offline evaluation pipeline.

The repository currently has the Jira ingestion layer, domain schemas, sample data, a health-check API, and evaluation scaffolding in place. The actual test-case generation route and Gemini integration are not wired yet, even though the config already reserves the required environment variables.

## Current Status

- Implemented:
  - Jira issue fetcher via the Jira REST API
  - Jira payload to `StoryContext` parsing
  - Pydantic schemas for story context and generated test suites
  - Inline structural quality gate for generated suites
  - Offline evaluation pipeline scaffold
  - FastAPI app with `/health`
  - Unit tests for ingestion, gate logic, and API health
- Not implemented yet:
  - `POST /generate` route
  - Gemini prompt / generation pipeline
  - Real `deepeval` metrics inside the offline pipeline

## Workflow

This is the intended end-to-end flow, with the current implementation state called out at each step.

1. Configure secrets and runtime settings.
   Tool: `pydantic-settings`
   Purpose: Load Jira and Gemini credentials from `.env` through [`src/config.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/config.py).

2. Fetch a Jira issue.
   Tool: `httpx`
   Purpose: [`src/jira/client.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/jira/client.py) calls Jira REST API v3 and requests only the fields needed for test generation.

3. Normalize the Jira payload into a domain model.
   Tool: custom ingestor + `pydantic`
   Purpose: [`src/jira/ingestor.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/jira/ingestor.py) converts Atlassian Document Format (ADF) into plain text, extracts acceptance criteria, and builds a validated `StoryContext` defined in [`src/models/schemas.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/models/schemas.py).

4. Save sample input for offline work.
   Tool: local script + JSON files
   Purpose: [`scripts/fetch_issue.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/fetch_issue.py) fetches a live Jira issue, stores the raw payload under [`data/sample_stories/`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/data/sample_stories), and prints the parsed model for repeatable local development.

5. Generate test cases from the normalized story.
   Tool: Gemini via `google-genai`
   Purpose: Planned step. The config already includes `GEMINI_API_KEY`, but there is no generation module or API route wired yet.

6. Validate generated output before returning it.
   Tool: inline gate
   Purpose: [`src/evaluation/gate.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/evaluation/gate.py) enforces minimum structural quality such as non-empty titles, steps, expected results, and source story references.

7. Run offline evaluation across suites.
   Tool: `deepeval` scaffold
   Purpose: [`src/evaluation/pipeline.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/evaluation/pipeline.py) is the batch-evaluation entry point used by [`scripts/run_eval.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/run_eval.py). It currently returns placeholder metadata and is ready for real `deepeval` metrics to be added.

8. Expose the workflow through an API.
   Tool: `FastAPI` + `uvicorn`
   Purpose: [`main.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/main.py) exports the app, and [`src/api/app.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/api/app.py) currently serves `/health`. Route registration for generation still needs to be implemented in [`src/api/routes.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/api/routes.py).

## Tools Used

| Tool / Library | Used For | Where |
| --- | --- | --- |
| `FastAPI` | API app and health endpoint | [`src/api/app.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/api/app.py) |
| `uvicorn` | Local ASGI server | [`main.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/main.py) |
| `httpx` | Async Jira API calls | [`src/jira/client.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/jira/client.py) |
| `pydantic` | Strongly typed schemas for input/output contracts | [`src/models/schemas.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/models/schemas.py) |
| `pydantic-settings` | `.env`-driven configuration | [`src/config.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/config.py) |
| Jira REST API v3 | Source of story data | [`src/jira/client.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/jira/client.py) |
| Atlassian Document Format parsing | Converts Jira rich text to plain text | [`src/jira/ingestor.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/jira/ingestor.py) |
| `pytest` | Unit tests for parser, gate, and API health | [`tests/jira/test_ingestor.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/tests/jira/test_ingestor.py), [`tests/evaluation/test_gate.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/tests/evaluation/test_gate.py), [`tests/api/test_health.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/tests/api/test_health.py) |
| `deepeval` | Planned offline quality metrics | [`src/evaluation/pipeline.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/evaluation/pipeline.py), [`conftest.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/conftest.py) |
| Gemini / `google-genai` | Planned LLM-based test generation | [`src/config.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/config.py), [`.env.example`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/.env.example) |

## Repository Layout

```text
.
├── data/
│   ├── sample_responses/   # Reference generated suites for offline evaluation
│   └── sample_stories/     # Raw Jira payloads and parsed story examples
├── scripts/
│   ├── fetch_issue.py      # Pull one Jira issue and save sample data
│   └── run_eval.py         # Run offline evaluation over sample suites
├── src/
│   ├── api/                # FastAPI app and future routes
│   ├── evaluation/         # Inline gate and offline evaluation pipeline
│   ├── jira/               # Jira API client and issue ingestor
│   ├── models/             # Pydantic schemas shared across layers
│   └── config.py           # Environment-backed settings
├── tests/                  # Unit tests
└── main.py                 # Uvicorn entry point
```

## Setup

There is no dependency lockfile or package manifest checked into the repository yet, so install the libraries used by the current codebase manually in your preferred environment manager.

Minimum libraries inferred from imports:

- `fastapi`
- `uvicorn`
- `httpx`
- `pydantic`
- `pydantic-settings`
- `pytest`
- `deepeval`

If you plan to add the missing generation layer, you will also need the Gemini client library that matches your implementation choice.

Create your environment file from [`.env.example`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/.env.example):

```bash
cp .env.example .env
```

Required values:

- `JIRA_BASE_URL`
- `JIRA_EMAIL`
- `JIRA_API_TOKEN`
- `GEMINI_API_KEY` for the planned generation step

## Running Locally

Start the API:

```bash
uvicorn main:app --reload
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

Fetch and save a Jira issue:

```bash
python scripts/fetch_issue.py PROJ-123
```

Run the offline evaluation scaffold:

```bash
python scripts/run_eval.py
```

Run tests:

```bash
pytest
```

## Sample Data

- [`data/sample_stories/PROJ-1-raw.json`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/data/sample_stories/PROJ-1-raw.json) is a raw Jira payload fixture.
- [`data/sample_stories/PROJ-1.json`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/data/sample_stories/PROJ-1.json) is the parsed `StoryContext` form.
- [`data/sample_responses/PROJ-1.json`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/data/sample_responses/PROJ-1.json) is a reference generated test suite.

These fixtures support local parser work and future offline evaluation without repeatedly calling Jira.

## Next Steps

- Implement the generation module that turns `StoryContext` into `GeneratedTestSuite`.
- Add `POST /generate` and register routes in the FastAPI app.
- Replace the placeholder evaluation pipeline with real `deepeval` metrics.
- Add a dependency manifest such as `pyproject.toml` so setup is reproducible.
