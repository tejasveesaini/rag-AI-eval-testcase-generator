# UI

This folder contains a standalone Python UI for the RAG AI Eval Testcase Generator.

## Run

```bash
python3 ui/server.py
```

Then open `http://127.0.0.1:8090`.

## What It Does

- shows saved issue artifacts from `data/`
- loads normalized story and context data
- previews baseline and enriched generated suites
- runs structural evaluation on saved suites
- can fetch stories, collect context, and generate suites when the project dependencies and `.env` are configured

## Notes

- the UI itself uses only the Python standard library
- fetch, context collection, and generation still depend on the main project modules and credentials
