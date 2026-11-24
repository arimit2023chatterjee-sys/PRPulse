import hashlib
import hmac
import logging
from typing import Any, Dict, List, Optional
import httpx

from app.config import settings
from app.models.schemas import FindingSeverity, PRReviewResult, ReviewFinding, ReviewVerdict

logger = logging.getLogger(__name__)


class GitHubService:
    """Handles interactions with the GitHub REST API and webhook validation."""

    def __init__(self, token: Optional[str] = None):
        self.token = token or settings.GITHUB_TOKEN
        self.api_base = settings.GITHUB_API_BASE.rstrip("/")

    @staticmethod
    def verify_webhook_signature(
        payload_bytes: bytes,
        signature_header: Optional[str],
        secret: Optional[str] = None,
    ) -> bool:
        """
        Verifies that the webhook payload was sent by GitHub by validating
        the HMAC SHA-256 signature in the X-Hub-Signature-256 header.
        """
        configured_secret = secret or settings.GITHUB_WEBHOOK_SECRET
        if not configured_secret:
            logger.warning("No GITHUB_WEBHOOK_SECRET configured. Skipping HMAC signature check.")
            return True

        if not signature_header:
            logger.error("Missing X-Hub-Signature-256 header in webhook request.")
            return False

        if not signature_header.startswith("sha256="):
            logger.error("Invalid signature header format (expected sha256=...).")
            return False

        expected_sig = signature_header[7:].strip()
        computed_hmac = hmac.new(
            key=configured_secret.encode("utf-8"),
            msg=payload_bytes,
            digestmod=hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(computed_hmac, expected_sig)

    def _get_headers(self, accept_diff: bool = False) -> Dict[str, str]:
        headers = {
            "User-Agent": "FastAPI-PR-Reviewer-Bot/1.0",
        }
        if accept_diff:
            headers["Accept"] = "application/vnd.github.v3.diff"
        else:
            headers["Accept"] = "application/vnd.github.v3+json"

        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        return headers

    async def fetch_pr_diff(self, owner: str, repo: str, pull_number: int) -> str:
        """Fetches the raw unified diff for a pull request."""
        url = f"{self.api_base}/repos/{owner}/{repo}/pulls/{pull_number}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._get_headers(accept_diff=True))
            response.raise_for_status()
            return response.text

    async def fetch_pr_metadata(self, owner: str, repo: str, pull_number: int) -> Dict[str, Any]:
        """Fetches PR metadata including head commit SHA, title, author, and base branch."""
        url = f"{self.api_base}/repos/{owner}/{repo}/pulls/{pull_number}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._get_headers(accept_diff=False))
            response.raise_for_status()
            return response.json()

    def format_review_markdown(self, result: PRReviewResult) -> str:
        """Formats the structured review result into a clean, GitHub-flavored Markdown comment."""
        verdict_badge = {
            ReviewVerdict.APPROVE: "**APPROVE**",
            ReviewVerdict.REQUEST_CHANGES: "**REQUEST CHANGES**",
            ReviewVerdict.COMMENT: "**COMMENT**",
        }.get(result.verdict, "**COMMENT**")

        lines = [
            f"## PRPulse Code Quality & Security Inspection",
            f"",
            f"**Gate Verdict:** {verdict_badge}",
            f"",
            f"### Executive Summary",
            f"{result.summary}",
            f"",
            f"### Inspection Telemetry",
            f"- **Files Inspected:** {result.metrics.analyzed_files} (Filtered: {result.metrics.filtered_files})",
            f"- **Deterministic Static Findings:** {result.metrics.static_findings_count}",
            f"- **Rule & Semantic Findings:** {result.metrics.llm_findings_count}",
            f"- **Total Actionable Items:** {result.metrics.total_findings}",
            f"- **Latency:** {result.metrics.duration_ms:.1f}ms",
            f"",
        ]

        if result.findings:
            lines.append("### Findings Ledger")
            lines.append("| Severity | Category | Target File | Line | Issue | Remediation |")
            lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")

            for f in result.findings:
                line_str = str(f.line_hint) if f.line_hint else "N/A"
                issue_safe = f.issue.replace("|", "\\|").replace("\n", " ")
                fix_safe = f.suggested_fix.replace("|", "\\|").replace("\n", " ")
                sev = f.severity.value.upper()
                lines.append(
                    f"| `{sev}` | `{f.category.value}` | `{f.file}` | `{line_str}` | {issue_safe} | {fix_safe} |"
                )

            lines.append("")
            lines.append("<details><summary><b>Detailed Remediation Instructions</b></summary>")
            lines.append("")
            for idx, f in enumerate(result.findings, 1):
                lines.append(f"#### {idx}. [{f.severity.value.upper()}] `{f.file}` (Line {f.line_hint or 'Global'})")
                lines.append(f"- **Domain:** `{f.category.value}` ({f.source.upper()})")
                lines.append(f"- **Problem:** {f.issue}")
                lines.append(f"- **Suggested Remediation:**\n```\n{f.suggested_fix}\n```")
                lines.append("")
            lines.append("</details>")
        else:
            lines.append("**No defects, security issues, or anti-patterns detected.**")

        lines.append("")
        lines.append("---")
        lines.append("*Report generated by PRPulse Continuous Inspection Gateway*")
        return "\n".join(lines)

    async def post_pr_review(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        commit_sha: Optional[str],
        result: PRReviewResult,
    ) -> Dict[str, Any]:
        """
        Posts review notes back to the GitHub PR using the Pull Request Review API.
        """
        if not self.token:
            logger.warning("No GITHUB_TOKEN configured. Cannot post review to GitHub.")
            return {"status": "skipped", "reason": "No GITHUB_TOKEN configured"}

        url = f"{self.api_base}/repos/{owner}/{repo}/pulls/{pull_number}/reviews"
        body_markdown = self.format_review_markdown(result)

        event_map = {
            ReviewVerdict.APPROVE: "APPROVE",
            ReviewVerdict.REQUEST_CHANGES: "REQUEST_CHANGES",
            ReviewVerdict.COMMENT: "COMMENT",
        }
        github_event = event_map.get(result.verdict, "COMMENT")

        payload: Dict[str, Any] = {
            "body": body_markdown,
            "event": github_event,
        }
        if commit_sha:
            payload["commit_id"] = commit_sha

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=self._get_headers(accept_diff=False), json=payload)
            response.raise_for_status()
            return response.json()

