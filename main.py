"""Entry point — run with:  uvicorn main:app --reload"""

from src.api.app import app  # noqa: F401  re-exported for uvicorn
