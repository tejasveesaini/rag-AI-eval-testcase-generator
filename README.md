# RAG AI Eval Testcase Generator

This project pulls Jira stories, normalizes them into a controlled schema, generates structured QA test cases with Gemini, validates the JSON contract, and can push the generated tests back into Jira as TestCase subtasks.

The repository now includes the full local CLI pipeline for story retrieval, context collection, prompt construction, Gemini generation, JSON validation, and Jira write-back. The API currently exposes story retrieval only; generation is implemented in the service layer but not yet exposed as a `POST` route.

## Current Status

Implemented today:

- Jira story fetch + normalization
- Story retrieval API: `GET /stories/{issue_key}`
- Historical context collection pipeline
- Prompt builder for Gemini
- Gemini-based test generation
- Inline structural gate and JSON contract evaluation
- Jira push script for generated test cases
- Demo bug seeding / linking helper
- Unit tests for ingestion, gate logic, health, and story route behavior

Still incomplete:

- No `POST /generate` API route yet
- Historical `ContextPackage` is supported by the prompt builder but not yet injected by `generate_test_suite()`
- `src/evaluation/pipeline.py` / `scripts/run_eval.py` are still placeholders for deeper `deepeval` metrics
- No dependency manifest such as `pyproject.toml`

## Workflow

This is the current end-to-end workflow and the tool used at each stage.

1. Load configuration.
   Tool: `pydantic-settings`
   Purpose: [`src/config.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/config.py) reads Jira and Gemini credentials from [`.env.example`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/.env.example) / `.env`.

2. Fetch the story from Jira.
   Tool: `httpx` + Jira REST API v3
   Purpose: [`src/jira/client.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/jira/client.py) calls Jira with a reduced field set so only relevant story data is pulled.

3. Normalize the story into a stable schema.
   Tool: custom ingestor + `pydantic`
   Purpose: [`src/jira/ingestor.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/jira/ingestor.py) converts ADF rich text to plain text, extracts acceptance criteria, and returns `StoryContext` from [`src/models/schemas.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/models/schemas.py).

4. Expose normalized story data over the API.
   Tool: `FastAPI`
   Purpose: [`src/api/routes.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/api/routes.py) serves `GET /stories/{issue_key}` so the normalized `StoryContext` can be saved to [`data/normalized/`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/data/normalized) for generation.

5. Collect optional historical context from Jira.
   Tool: narrow Jira retrieval + normalization + packaging
   Purpose: [`scripts/collect_context.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/collect_context.py) orchestrates:
   [`src/context/collector.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/context/collector.py) to fetch linked issues and narrow JQL matches,
   [`src/context/normalizer.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/context/normalizer.py) to convert them into `ContextItem`s,
   and [`src/context/packager.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/context/packager.py) to build a `ContextPackage` in [`data/context/`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/data/context).

6. Build the Gemini prompt.
   Tool: prompt templating + strict schema instructions
   Purpose: [`src/generation/prompt.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/generation/prompt.py) renders the `StoryContext`, enum constraints, hard rules, and optional `ContextPackage` into a machine-parseable generation prompt.

7. Generate test cases.
   Tool: `google-genai` / Gemini
   Purpose: [`src/generation/generator.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/generation/generator.py) sends the prompt to Gemini, strips accidental markdown fences, parses the JSON, and validates it as `GeneratedTestSuite`.

8. Persist generated suites locally.
   Tool: CLI script + JSON
   Purpose: [`scripts/generate_tests.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/generate_tests.py) reads [`data/normalized/`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/data/normalized) and writes generated suites to [`data/generated/`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/data/generated).

9. Validate generated JSON.
   Tool: `pydantic` + inline gate + custom checks
   Purpose: [`scripts/run_json_eval.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/run_json_eval.py) checks JSON validity, schema compliance, enum correctness, negative-test presence, source-story consistency, and [`src/evaluation/gate.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/evaluation/gate.py) structural rules.

10. Push generated tests back to Jira.
    Tool: Jira issue creation + ADF rendering
    Purpose: [`scripts/push_tests.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/push_tests.py) converts each generated test case into Jira ADF and creates a TestCase subtask under the parent story.

11. Seed demo bugs for regression context.
    Tool: Jira issue creation + issue linking
    Purpose: [`scripts/push_bugs.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/push_bugs.py) creates example Bug issues and links them to the feature story and test cases. This script is demo-specific and currently hardcoded to AIP sample issues.

## Tools Used

| Tool / Library | Used For | Where |
| --- | --- | --- |
| `FastAPI` | API app and story retrieval endpoint | [`src/api/app.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/api/app.py), [`src/api/routes.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/api/routes.py) |
| `uvicorn` | Local ASGI server | [`main.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/main.py) |
| `httpx` | Jira GET/POST calls | [`src/jira/client.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/jira/client.py), [`src/context/collector.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/context/collector.py) |
| `pydantic` | Story, context, and generated-suite contracts | [`src/models/schemas.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/models/schemas.py) |
| `pydantic-settings` | `.env`-backed configuration | [`src/config.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/config.py) |
| Jira REST API v3 | Story retrieval, context retrieval, and issue creation | [`src/jira/client.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/jira/client.py), [`scripts/push_tests.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/push_tests.py), [`scripts/push_bugs.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/push_bugs.py) |
| Atlassian Document Format | Jira description parsing and write-back rendering | [`src/jira/ingestor.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/jira/ingestor.py), [`scripts/push_tests.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/push_tests.py), [`scripts/push_bugs.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/push_bugs.py) |
| `google-genai` / Gemini | Test-case generation | [`src/generation/generator.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/generation/generator.py), [`src/generation/prompt.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/generation/prompt.py) |
| `pytest` | API and unit tests | [`tests/api/test_health.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/tests/api/test_health.py), [`tests/api/test_stories.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/tests/api/test_stories.py), [`tests/jira/test_ingestor.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/tests/jira/test_ingestor.py), [`tests/evaluation/test_gate.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/tests/evaluation/test_gate.py) |
| `deepeval` | Planned deeper offline evaluation | [`src/evaluation/pipeline.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/evaluation/pipeline.py), [`scripts/run_eval.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/run_eval.py), [`conftest.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/conftest.py) |

## API Endpoints

- `GET /health`
  Returns a simple liveness payload.
- `GET /stories/{issue_key}`
  Fetches a Jira issue and returns normalized `StoryContext`.

Current API gap:

- No `POST /generate` route yet. Generation is available through [`scripts/generate_tests.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/generate_tests.py) and the generation modules directly.

## Repository Layout

```text
.
├── data/
│   ├── context/           # Saved ContextPackage JSON
│   ├── generated/         # Gemini-generated test suites
│   ├── normalized/        # Normalized StoryContext JSON used for generation
│   ├── sample_responses/  # Reference outputs
│   └── sample_stories/    # Raw and example story fixtures
├── scripts/
│   ├── collect_context.py
│   ├── fetch_issue.py
│   ├── generate_tests.py
│   ├── push_bugs.py
│   ├── push_tests.py
│   ├── run_eval.py
│   └── run_json_eval.py
├── src/
│   ├── api/
│   ├── context/
│   ├── evaluation/
│   ├── generation/
│   ├── jira/
│   ├── models/
│   └── config.py
├── tests/
├── main.py
└── LICENSE
```

## Setup

There is still no checked-in package manifest, so install the current dependencies manually in your preferred environment.

Minimum libraries inferred from the code:

- `fastapi`
- `uvicorn`
- `httpx`
- `pydantic`
- `pydantic-settings`
- `google-genai`
- `pytest`
- `deepeval`

Create `.env` from [`.env.example`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/.env.example):

```bash
cp .env.example .env
```

Required values:

- `JIRA_BASE_URL`
- `JIRA_EMAIL`
- `JIRA_API_TOKEN`
- `GEMINI_API_KEY`

## Running Locally

Start the API:

```bash
uvicorn main:app --reload
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

Fetch a normalized story and save it for generation:

```bash
mkdir -p data/normalized
curl http://127.0.0.1:8000/stories/AIP-2 > data/normalized/AIP-2.json
```

Collect historical context for the story:

```bash
python scripts/collect_context.py AIP-2
```

Generate test cases with Gemini:

```bash
python scripts/generate_tests.py AIP-2
```

Validate generated JSON:

```bash
python scripts/run_json_eval.py AIP-2
```

Push generated tests to Jira:

```bash
python scripts/push_tests.py AIP-2
```

Fetch and save a raw Jira payload fixture:

```bash
python scripts/fetch_issue.py AIP-2
```

Run the placeholder offline eval runner:

```bash
python scripts/run_eval.py
```

Run tests:

```bash
pytest
```

## Sample Artifacts

- [`data/normalized/AIP-2.json`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/data/normalized/AIP-2.json) is a normalized `StoryContext`.
- [`data/context/AIP-2.json`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/data/context/AIP-2.json) is a packaged retrieval context bundle.
- [`data/generated/AIP-2.json`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/data/generated/AIP-2.json) is a generated `GeneratedTestSuite`.
- [`data/sample_stories/PROJ-1-raw.json`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/data/sample_stories/PROJ-1-raw.json) is a raw Jira fixture used by tests.
- [`data/sample_responses/PROJ-1.json`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/data/sample_responses/PROJ-1.json) is a reference suite for offline work.

## Caveats

- [`scripts/generate_tests.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/generate_tests.py) requires `data/normalized/<ISSUE_KEY>.json`; [`scripts/fetch_issue.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/fetch_issue.py) does not create that file automatically.
- [`src/generation/prompt.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/generation/prompt.py) supports injecting `ContextPackage`, but [`src/generation/generator.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/src/generation/generator.py) currently calls it without context.
- [`scripts/push_tests.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/push_tests.py) assumes the Jira TestCase subtask issue-type ID is `10012`.
- [`scripts/push_bugs.py`](/Users/tejasveesaini/rag-AI-eval-testcase-generator/scripts/push_bugs.py) is hardcoded to `AIP`, `AIP-2`, and `AIP-4/AIP-5`; treat it as a demo helper, not a general-purpose script.

## Next Steps

- Add `POST /generate` and expose generation through the API.
- Wire `ContextPackage` into the live generation call path.
- Replace the placeholder deep-eval pipeline with real `deepeval` metrics.
- Add a reproducible dependency file such as `pyproject.toml`.
