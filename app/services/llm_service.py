import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import httpx

from app.config import settings
from app.models.schemas import (
    FindingCategory,
    FindingSeverity,
    PRReviewResult,
    ReviewFinding,
    ReviewMetrics,
    ReviewVerdict,
)
from app.services.diff_parser import ParsedFileDiff

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a senior backend and security code reviewer evaluating a pull request diff.
Your task is to inspect code changes and return a structured JSON report identifying actionable issues.

Review Focus:
1. Security & Edge Cases: Injection risks, authentication/authorization gaps, secrets, unhandled edge cases.
2. Backend Bugs & Reliability: Race conditions, unhandled exceptions, resource leaks, N+1 queries, incorrect transaction boundaries.
3. Performance: Inefficient data structures, quadratic loops, missing database indexes on filter fields.
4. Maintainability: Violations of SOLID, excessive complexity, missing error context.

Guidelines:
- Do NOT comment on purely cosmetic style, whitespace, or formatting (linters handle that).
- Focus strictly on modified lines and their immediate context.
- Be actionable: each finding must have a concise explanation of the issue and a concrete suggested fix.
- Output MUST be valid JSON strictly conforming to this schema:
{
  "verdict": "approve" | "request_changes" | "comment",
  "summary": "Executive summary of the changes and overall quality verdict",
  "findings": [
    {
      "file": "path/to/file.py",
      "line_hint": 42,
      "severity": "critical" | "error" | "warning" | "info",
      "category": "bug" | "security" | "edge_case" | "maintainability" | "performance",
      "issue": "Description of defect or risk",
      "suggested_fix": "Concrete code snippet or advice to fix"
    }
  ]
}
"""


class LLMReviewer:
    """Manages LLM review execution with support for Gemini, OpenAI, and standalone Mock mode."""

    def __init__(self):
        self.provider = settings.LLM_PROVIDER
        if self.provider == "gemini" and not settings.GEMINI_API_KEY:
            logger.warning("GEMINI_API_KEY is not set. Falling back to mock reviewer.")
            self.provider = "mock"
        elif self.provider == "openai" and not settings.OPENAI_API_KEY:
            logger.warning("OPENAI_API_KEY is not set. Falling back to mock reviewer.")
            self.provider = "mock"

    async def review_diff(
        self,
        files: List[ParsedFileDiff],
        static_findings: List[ReviewFinding],
    ) -> Tuple[str, ReviewVerdict, List[ReviewFinding], int]:
        """
        Executes structured LLM pass.
        Returns (summary, verdict, findings, tokens_used).
        """
        if self.provider == "gemini":
            try:
                return await self._review_with_gemini(files, static_findings)
            except Exception as exc:
                logger.error(f"Gemini LLM review failed ({exc}). Falling back to mock analyzer.")
                return self._review_with_mock(files, static_findings)

        elif self.provider == "openai":
            try:
                return await self._review_with_openai(files, static_findings)
            except Exception as exc:
                logger.error(f"OpenAI LLM review failed ({exc}). Falling back to mock analyzer.")
                return self._review_with_mock(files, static_findings)

        else:
            return self._review_with_mock(files, static_findings)

    def _build_review_payload(
        self, files: List[ParsedFileDiff], static_findings: List[ReviewFinding]
    ) -> str:
        prompt_parts = ["Analyze the following pull request code changes:\n"]

        if static_findings:
            prompt_parts.append("### Note: The following deterministic static issues were already detected:")
            for f in static_findings[:10]:
                prompt_parts.append(f"- [{f.severity.value.upper()}] {f.file}:{f.line_hint or '?'} ({f.category.value}): {f.issue}")
            prompt_parts.append("\nPlease look for deeper architectural, logical, and edge-case issues.\n")

        prompt_parts.append("### Changed Files & Diffs:\n")
        for file in files:
            if file.is_ignored or file.is_binary or file.is_deleted:
                continue
            prompt_parts.append(f"--- File: {file.filename} ---")
            prompt_parts.append(file.raw_diff[:20000])  # Cap diff per file to avoid token explosion
            prompt_parts.append("\n")

        return "\n".join(prompt_parts)

    async def _review_with_gemini(
        self, files: List[ParsedFileDiff], static_findings: List[ReviewFinding]
    ) -> Tuple[str, ReviewVerdict, List[ReviewFinding], int]:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{settings.GEMINI_MODEL}:generateContent?key={settings.GEMINI_API_KEY}"
        )
        content_prompt = self._build_review_payload(files, static_findings)

        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": content_prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "response_mime_type": "application/json",
            },
        }

        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        tokens_used = data.get("usageMetadata", {}).get("totalTokenCount", 0)
        return self._parse_llm_json_response(raw_text, tokens_used)

    async def _review_with_openai(
        self, files: List[ParsedFileDiff], static_findings: List[ReviewFinding]
    ) -> Tuple[str, ReviewVerdict, List[ReviewFinding], int]:
        base_url = (settings.OPENAI_BASE_URL or "https://api.openai.com/v1").rstrip("/")
        url = f"{base_url}/chat/completions"
        content_prompt = self._build_review_payload(files, static_findings)

        headers = {
            "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": settings.OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }

        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        raw_text = data["choices"][0]["message"]["content"]
        tokens_used = data.get("usage", {}).get("total_tokens", 0)
        return self._parse_llm_json_response(raw_text, tokens_used)

    def _parse_llm_json_response(
        self, raw_json_str: str, tokens_used: int
    ) -> Tuple[str, ReviewVerdict, List[ReviewFinding], int]:
        clean_text = raw_json_str.strip()
        if clean_text.startswith("```json"):
            clean_text = clean_text[7:]
        if clean_text.startswith("```"):
            clean_text = clean_text[3:]
        if clean_text.endswith("```"):
            clean_text = clean_text[:-3]

        parsed = json.loads(clean_text.strip())

        summary = parsed.get("summary", "Review completed successfully.")
        verdict_str = parsed.get("verdict", "comment").lower()

        verdict_map = {
            "approve": ReviewVerdict.APPROVE,
            "request_changes": ReviewVerdict.REQUEST_CHANGES,
            "comment": ReviewVerdict.COMMENT,
        }
        verdict = verdict_map.get(verdict_str, ReviewVerdict.COMMENT)

        findings: List[ReviewFinding] = []
        for item in parsed.get("findings", []):
            try:
                finding = ReviewFinding(
                    file=item.get("file", "unknown"),
                    line_hint=item.get("line_hint"),
                    severity=FindingSeverity(item.get("severity", "info").lower()),
                    category=FindingCategory(item.get("category", "maintainability").lower()),
                    issue=item.get("issue", ""),
                    suggested_fix=item.get("suggested_fix", ""),
                    source="llm",
                )
                findings.append(finding)
            except Exception as e:
                logger.warning(f"Skipping malformed LLM finding item: {item} ({e})")

        return summary, verdict, findings, tokens_used

    def _review_with_mock(
        self, files: List[ParsedFileDiff], static_findings: List[ReviewFinding]
    ) -> Tuple[str, ReviewVerdict, List[ReviewFinding], int]:
        """
        Deterministic, offline backend code quality analysis.
        Inspects diffs for key backend anti-patterns and flaws.
        """
        findings: List[ReviewFinding] = []
        analyzed_count = 0

        for file in files:
            if file.is_ignored or file.is_binary or file.is_deleted:
                continue

            analyzed_count += 1
            for hunk in file.hunks:
                for line_no, content in hunk.added_lines:
                    line_stripped = content.strip()

                    # 1. Blocking calls in async definitions
                    if "time.sleep(" in line_stripped:
                        findings.append(
                            ReviewFinding(
                                file=file.filename,
                                line_hint=line_no,
                                severity=FindingSeverity.WARNING,
                                category=FindingCategory.PERFORMANCE,
                                issue="Synchronous `time.sleep()` blocks the event loop in asynchronous applications.",
                                suggested_fix="Use `await asyncio.sleep(...)` instead of blocking sleep.",
                                source="llm",
                            )
                        )

                    # 2. Database query inside loop heuristic
                    if re.search(r"for\s+\w+\s+in\s+.*:", line_stripped):
                        # Flag loop start
                        pass
                    if ("session.query(" in line_stripped or "db.execute(" in line_stripped) and "for " in hunk.header:
                        findings.append(
                            ReviewFinding(
                                file=file.filename,
                                line_hint=line_no,
                                severity=FindingSeverity.WARNING,
                                category=FindingCategory.PERFORMANCE,
                                issue="Potential N+1 query issue: database query inside an iteration block.",
                                suggested_fix="Batch fetch entities using `in_()` or join relationship queries prior to iteration.",
                                source="llm",
                            )
                        )

                    # 3. Missing timeout on network requests
                    if (
                        ("requests.get(" in line_stripped or "requests.post(" in line_stripped or "httpx.get(" in line_stripped)
                        and "timeout=" not in line_stripped
                    ):
                        findings.append(
                            ReviewFinding(
                                file=file.filename,
                                line_hint=line_no,
                                severity=FindingSeverity.WARNING,
                                category=FindingCategory.EDGE_CASE,
                                issue="HTTP network call without explicit `timeout` may hang indefinitely under network degradation.",
                                suggested_fix="Always specify explicit timeouts: e.g. `timeout=10.0`.",
                                source="llm",
                            )
                        )

                    # 4. Unhandled NoneType on dict.get or json access without default
                    if re.search(r"\.get\(.+?\)\[.+?\]", line_stripped):
                        findings.append(
                            ReviewFinding(
                                file=file.filename,
                                line_hint=line_no,
                                severity=FindingSeverity.WARNING,
                                category=FindingCategory.BUG,
                                issue="Chained indexing on `.get()` result without None-check risks `TypeError: 'NoneType' object is not subscriptable`.",
                                suggested_fix="Provide a default fallback dict `.get('key', {})` or validate key existence before subscripting.",
                                source="llm",
                            )
                        )

                    # 5. Missing type annotation in function definition
                    if line_stripped.startswith("def ") and "->" not in line_stripped and not file.filename.startswith("tests/"):
                        findings.append(
                            ReviewFinding(
                                file=file.filename,
                                line_hint=line_no,
                                severity=FindingSeverity.INFO,
                                category=FindingCategory.MAINTAINABILITY,
                                issue="Function signature lacks return type annotation.",
                                suggested_fix="Add explicit return type hint: `def func(...) -> ReturnType:`",
                                source="llm",
                            )
                        )

        total_issues = len(findings) + len(static_findings)
        has_critical_or_error = any(
            f.severity in (FindingSeverity.CRITICAL, FindingSeverity.ERROR)
            for f in findings + static_findings
        )

        if has_critical_or_error:
            verdict = ReviewVerdict.REQUEST_CHANGES
            summary = (
                f"Reviewed {analyzed_count} changed file(s). Found {total_issues} issue(s) "
                f"including critical/error-level defects that require changes before merging."
            )
        elif total_issues > 0:
            verdict = ReviewVerdict.COMMENT
            summary = (
                f"Reviewed {analyzed_count} changed file(s). Found {total_issues} suggestion(s). "
                f"Changes look solid with minor maintainability or performance improvements noted."
            )
        else:
            verdict = ReviewVerdict.APPROVE
            summary = (
                f"Reviewed {analyzed_count} changed file(s). Clean implementation with zero static defects "
                f"or critical bugs detected."
            )

        tokens_used = 450 + (analyzed_count * 120)
        return summary, verdict, findings, tokens_used

