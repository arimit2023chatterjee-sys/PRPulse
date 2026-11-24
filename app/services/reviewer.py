import logging
import time
import uuid
from typing import List, Optional, Tuple
from sqlalchemy.orm import Session

from app.config import settings
from app.models.db_models import FindingRecord, ReviewRunRecord
from app.models.schemas import (
    FindingSeverity,
    PRReviewResult,
    ReviewFinding,
    ReviewMetrics,
    ReviewVerdict,
)
from app.services.diff_parser import ParsedFileDiff, parse_unified_diff
from app.services.github_service import GitHubService
from app.services.llm_service import LLMReviewer
from app.services.static_analyzer import StaticAnalyzer

logger = logging.getLogger(__name__)


class ReviewOrchestrator:
    """
    Coordinates the entire PR Review pipeline:
    1. Parse & filter diff (excluding binaries, lockfiles, images)
    2. Deterministic fast static pass (secrets, AST checks, ruff)
    3. Structured LLM review pass (bugs, edge cases, maintainability)
    4. Findings aggregation & verdict determination
    5. Database persistence (SQLite/PostgreSQL)
    6. Optional posting back to GitHub PR
    """

    def __init__(self, db: Optional[Session] = None):
        self.db = db
        self.static_analyzer = StaticAnalyzer(enable_ruff=settings.ENABLE_RUFF)
        self.llm_reviewer = LLMReviewer()
        self.github_service = GitHubService()

    async def review_diff_text(
        self,
        diff_text: str,
        repo: Optional[str] = None,
        pull_number: Optional[int] = None,
        commit_sha: Optional[str] = None,
        post_to_github: bool = False,
    ) -> PRReviewResult:
        start_time = time.perf_counter()

        # Step 1: Parse and filter diff
        all_files = parse_unified_diff(diff_text)
        total_files = len(all_files)

        active_files: List[ParsedFileDiff] = [
            f for f in all_files if not f.is_ignored and not f.is_binary and not f.is_deleted
        ]
        filtered_files = total_files - len(active_files)

        # Enforce size limits
        if len(active_files) > settings.MAX_DIFF_FILES:
            logger.warning(
                f"Diff contains {len(active_files)} files, exceeding limit of {settings.MAX_DIFF_FILES}. Truncating."
            )
            active_files = active_files[: settings.MAX_DIFF_FILES]

        # Step 2: Deterministic Static Pass (Fast & Free)
        static_findings: List[ReviewFinding] = []
        for file_diff in active_files:
            file_static_findings = self.static_analyzer.analyze_diff(file_diff)
            static_findings.extend(file_static_findings)

        # Step 3: Structured LLM Review Pass
        llm_summary, llm_verdict, llm_findings, tokens_used = await self.llm_reviewer.review_diff(
            active_files, static_findings
        )

        # Step 4: Merge and Deduplicate Findings
        merged_findings = self._merge_findings(static_findings, llm_findings)

        # Step 5: Compute Final Verdict
        final_verdict = self._compute_verdict(merged_findings, llm_verdict)

        duration_ms = (time.perf_counter() - start_time) * 1000.0

        metrics = ReviewMetrics(
            total_files=total_files,
            analyzed_files=len(active_files),
            filtered_files=filtered_files,
            static_findings_count=len(static_findings),
            llm_findings_count=len(llm_findings),
            total_findings=len(merged_findings),
            tokens_used=tokens_used,
            duration_ms=round(duration_ms, 2),
        )

        review_id = str(uuid.uuid4())
        result = PRReviewResult(
            id=review_id,
            repo=repo,
            pull_number=pull_number,
            commit_sha=commit_sha,
            verdict=final_verdict,
            summary=llm_summary,
            findings=merged_findings,
            metrics=metrics,
            posted_to_github=False,
        )

        # Step 6: Post back to GitHub if requested
        if post_to_github and repo and pull_number:
            try:
                owner, repo_name = repo.split("/", 1)
                await self.github_service.post_pr_review(
                    owner=owner,
                    repo=repo_name,
                    pull_number=pull_number,
                    commit_sha=commit_sha,
                    result=result,
                )
                result.posted_to_github = True
            except Exception as e:
                logger.error(f"Failed to post review to GitHub: {e}")

        # Step 7: Persist run and findings to SQLite/DB
        if self.db:
            try:
                self._persist_to_db(result)
            except Exception as e:
                logger.error(f"Failed to persist review run to database: {e}")

        return result

    async def review_github_pr(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        post_to_github: bool = False,
    ) -> PRReviewResult:
        """Fetches PR diff and metadata from GitHub and executes full review pipeline."""
        diff_text = await self.github_service.fetch_pr_diff(owner, repo, pull_number)
        metadata = await self.github_service.fetch_pr_metadata(owner, repo, pull_number)
        commit_sha = metadata.get("head", {}).get("sha")

        full_repo_name = f"{owner}/{repo}"
        return await self.review_diff_text(
            diff_text=diff_text,
            repo=full_repo_name,
            pull_number=pull_number,
            commit_sha=commit_sha,
            post_to_github=post_to_github,
        )

    def _merge_findings(
        self, static: List[ReviewFinding], llm: List[ReviewFinding]
    ) -> List[ReviewFinding]:
        """Merges static and LLM findings, deduplicating on file + line + category."""
        merged: List[ReviewFinding] = list(static)
        seen = {(f.file, f.line_hint, f.category) for f in static if f.line_hint is not None}

        for f in llm:
            key = (f.file, f.line_hint, f.category)
            if key not in seen:
                merged.append(f)
                if f.line_hint is not None:
                    seen.add(key)

        # Sort findings by severity priority: critical > error > warning > info
        priority = {
            FindingSeverity.CRITICAL: 0,
            FindingSeverity.ERROR: 1,
            FindingSeverity.WARNING: 2,
            FindingSeverity.INFO: 3,
        }
        merged.sort(key=lambda x: priority.get(x.severity, 4))
        return merged

    def _compute_verdict(
        self, findings: List[ReviewFinding], llm_verdict: ReviewVerdict
    ) -> ReviewVerdict:
        """Determines final review verdict based on issue severity and LLM opinion."""
        has_critical = any(f.severity == FindingSeverity.CRITICAL for f in findings)
        has_error = any(f.severity == FindingSeverity.ERROR for f in findings)

        if has_critical or has_error:
            return ReviewVerdict.REQUEST_CHANGES

        if any(f.severity == FindingSeverity.WARNING for f in findings):
            return ReviewVerdict.COMMENT

        return llm_verdict

    def _persist_to_db(self, result: PRReviewResult) -> None:
        if not self.db:
            return

        run_record = ReviewRunRecord(
            id=result.id,
            repo=result.repo,
            pull_number=result.pull_number,
            commit_sha=result.commit_sha,
            status="success",
            verdict=result.verdict.value,
            summary=result.summary,
            total_findings=result.metrics.total_findings,
            static_findings_count=result.metrics.static_findings_count,
            llm_findings_count=result.metrics.llm_findings_count,
            tokens_used=result.metrics.tokens_used,
            duration_ms=result.metrics.duration_ms,
            posted_to_github=1 if result.posted_to_github else 0,
        )
        self.db.add(run_record)

        for f in result.findings:
            finding_rec = FindingRecord(
                review_run_id=result.id,
                file=f.file,
                line_hint=f.line_hint,
                severity=f.severity.value,
                category=f.category.value,
                issue=f.issue,
                suggested_fix=f.suggested_fix,
                source=f.source,
            )
            self.db.add(finding_rec)

        self.db.commit()

