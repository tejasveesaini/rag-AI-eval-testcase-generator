import httpx
from fastapi import APIRouter, HTTPException, Path

from src.jira.client import JiraClient
from src.jira.ingestor import parse_issue
from src.models.schemas import StoryContext

router = APIRouter(prefix="/stories", tags=["stories"])


@router.get(
    "/{issue_key}",
    response_model=StoryContext,
    summary="Fetch and normalize a Jira story",
    description=(
        "Fetches a Jira issue by key, normalizes it into a StoryContext, "
        "and returns the structured result. "
        "This is the required first step before test-case generation."
    ),
)
async def get_story(
    issue_key: str = Path(
        ...,
        description="Jira issue key, e.g. AIP-2",
        pattern=r"^[A-Z]+-\d+$",
    ),
) -> StoryContext:
    client = JiraClient()
    try:
        raw = await client.get_issue(issue_key)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 404:
            raise HTTPException(status_code=404, detail=f"Issue {issue_key} not found in Jira.")
        if status == 401:
            raise HTTPException(status_code=401, detail="Jira authentication failed. Check JIRA_EMAIL and JIRA_API_TOKEN.")
        raise HTTPException(status_code=502, detail=f"Jira returned {status}: {e.response.text[:200]}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Could not reach Jira: {e}")

    return parse_issue(raw)
