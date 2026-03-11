from contextlib import asynccontextmanager
from fastapi import FastAPI

from src.api.routes import router as stories_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("✅ RAG AI Eval Testcase Generator — startup complete")
    yield
    print("🛑 RAG AI Eval Testcase Generator — shutting down")


app = FastAPI(
    title="RAG AI Eval Testcase Generator",
    version="0.1.0",
    description="Generates and evaluates test cases from Jira stories using Gemini.",
    lifespan=lifespan,
)

app.include_router(stories_router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness check — returns ok when the app is running."""
    return {"status": "ok", "version": app.version}
