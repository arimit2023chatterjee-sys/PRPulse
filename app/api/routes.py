import logging
import re
from typing import List, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.db_models import FindingRecord, ReviewRunRecord
from app.models.schemas import (
    DiffReviewRequest,
    PRReviewRequest,
    PRReviewResult,
    WebhookEventResponse,
)
from app.services.github_service import GitHubService
from app.services.reviewer import ReviewOrchestrator

logger = logging.getLogger(__name__)

router = APIRouter()

PR_URL_REGEX = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<pull_number>\d+)/?$"
)


@router.post(
    "/webhooks/github",
    response_model=WebhookEventResponse,
    summary="GitHub PR Webhook Trigger",
    description="Receives GitHub pull_request webhook events, verifies HMAC-SHA256 signature, and initiates review.",
)
async def handle_github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: Optional[str] = Header(None, alias="X-GitHub-Event"),
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
    db: Session = Depends(get_db),
):
    body_bytes = await request.body()

    # 1. Webhook Signature Verification
    if not GitHubService.verify_webhook_signature(body_bytes, x_hub_signature_256):
        logger.warning("Rejected GitHub webhook with invalid or missing HMAC signature.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Hub-Signature-256 signature.",
        )

    # 2. Event Type Filtering
    if x_github_event != "pull_request":
        return WebhookEventResponse(
            status="ignored",
            detail=f"Event '{x_github_event}' is ignored. Only 'pull_request' events are processed.",
        )

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    action = payload.get("action")
    # Only review on opened, synchronize (new commits pushed), or reopened
    if action not in ("opened", "synchronize", "reopened"):
        return WebhookEventResponse(
            status="ignored",
            action=action,
            detail=f"Action '{action}' is ignored. Only 'opened', 'synchronize', 'reopened' trigger reviews.",
        )

    pr_data = payload.get("pull_request", {})
    repo_data = payload.get("repository", {})

    repo_full_name = repo_data.get("full_name")
    pull_number = pr_data.get("number")
    commit_sha = pr_data.get("head", {}).get("sha")

    if not repo_full_name or not pull_number:
        raise HTTPException(status_code=400, detail="Missing repository or PR number in webhook.")

    owner, repo_name = repo_full_name.split("/", 1)
    auto_post = settings.AUTO_POST_TO_GITHUB

    # Review PR in background or synchronous depending on setup
    orchestrator = ReviewOrchestrator(db=db)
    result = await orchestrator.review_github_pr(
        owner=owner,
        repo=repo_name,
        pull_number=pull_number,
        post_to_github=auto_post,
    )

    return WebhookEventResponse(
        status="processed",
        action=action,
        repo=repo_full_name,
        pull_number=pull_number,
        review_id=result.id,
        detail=f"Review completed with verdict: {result.verdict.value.upper()}",
    )


@router.post(
    "/review/pr",
    response_model=PRReviewResult,
    summary="Manual Pull Request Review",
    description="Fetches and reviews a PR given either a GitHub PR URL or repo + PR number.",
)
async def review_pr(
    request: PRReviewRequest,
    db: Session = Depends(get_db),
):
    owner: Optional[str] = None
    repo_name: Optional[str] = None
    pull_number: Optional[int] = request.pull_number

    if request.pr_url:
        match = PR_URL_REGEX.match(request.pr_url.strip())
        if not match:
            raise HTTPException(
                status_code=400,
                detail="Invalid GitHub PR URL format. Expected: https://github.com/owner/repo/pull/123",
            )
        owner = match.group("owner")
        repo_name = match.group("repo")
        pull_number = int(match.group("pull_number"))
    elif request.repo and request.pull_number:
        parts = request.repo.strip().split("/")
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail="Repo must be in 'owner/repo' format.")
        owner, repo_name = parts
    else:
        raise HTTPException(
            status_code=400,
            detail="Must provide either 'pr_url' or both 'repo' and 'pull_number'.",
        )

    orchestrator = ReviewOrchestrator(db=db)
    try:
        result = await orchestrator.review_github_pr(
            owner=owner,
            repo=repo_name,
            pull_number=pull_number,
            post_to_github=request.post_to_github,
        )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to review PR {owner}/{repo_name}#{pull_number}: {exc}")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch or review PR from GitHub: {str(exc)}",
        )


@router.post(
    "/review/diff",
    response_model=PRReviewResult,
    summary="Direct Diff Review",
    description="Inspects raw unified diff text directly without requiring GitHub credentials. Perfect for CLI, pre-commit, or CI checks.",
)
async def review_diff(
    request: DiffReviewRequest,
    db: Session = Depends(get_db),
):
    orchestrator = ReviewOrchestrator(db=db)
    result = await orchestrator.review_diff_text(
        diff_text=request.diff_text,
        repo=request.title or "manual-diff",
    )
    return result


@router.get(
    "/reviews",
    summary="List Past Review Runs",
    description="Retrieves persisted review run metadata and statistics.",
)
def list_reviews(
    skip: int = 0,
    limit: int = 20,
    repo: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = db.query(ReviewRunRecord)
    if repo:
        query = query.filter(ReviewRunRecord.repo == repo)
    records = (
        query.order_by(ReviewRunRecord.created_at.desc())
        .offset(skip)
        .limit(min(limit, 100))
        .all()
    )
    return records


@router.get(
    "/reviews/{review_id}",
    summary="Get Detailed Review Run",
    description="Retrieves a specific past review run by ID with all categorized findings.",
)
def get_review(
    review_id: str,
    db: Session = Depends(get_db),
):
    record = db.query(ReviewRunRecord).filter(ReviewRunRecord.id == review_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Review run not found.")

    findings = db.query(FindingRecord).filter(FindingRecord.review_run_id == review_id).all()
    return {
        "review": record,
        "findings": findings,
    }


@router.get(
    "/health",
    summary="Service Health Check",
    description="Reports the service operational status, LLM provider, and feature flags.",
)
def health_check(db: Session = Depends(get_db)):
    db_ok = False
    try:
        db.execute(ReviewRunRecord.__table__.select().limit(1))
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "status": "healthy" if db_ok else "degraded",
        "database_connected": db_ok,
        "llm_provider": settings.LLM_PROVIDER,
        "ruff_enabled": settings.ENABLE_RUFF,
        "github_token_configured": bool(settings.GITHUB_TOKEN),
        "webhook_secret_configured": bool(settings.GITHUB_WEBHOOK_SECRET),
    }

