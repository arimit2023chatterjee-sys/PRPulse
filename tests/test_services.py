from app.models.schemas import FindingCategory, FindingSeverity, PRReviewResult, ReviewFinding, ReviewMetrics, ReviewVerdict
from app.services.github_service import GitHubService
from app.services.llm_service import LLMReviewer


def test_markdown_formatter_with_findings():
    service = GitHubService()
    finding = ReviewFinding(
        file="app/services/auth.py",
        line_hint=42,
        severity=FindingSeverity.CRITICAL,
        category=FindingCategory.SECURITY,
        issue="Hardcoded API secret key detected.",
        suggested_fix="Load key from environment variables.",
        source="static",
    )
    result = PRReviewResult(
        id="test-123",
        repo="octocat/Hello-World",
        pull_number=1,
        verdict=ReviewVerdict.REQUEST_CHANGES,
        summary="Security flaws discovered.",
        findings=[finding],
        metrics=ReviewMetrics(
            total_files=2,
            analyzed_files=1,
            filtered_files=1,
            static_findings_count=1,
            llm_findings_count=0,
            total_findings=1,
            tokens_used=150,
            duration_ms=45.2,
        ),
    )
    md = service.format_review_markdown(result)
    assert "REQUEST CHANGES" in md
    assert "Security flaws discovered." in md
    assert "app/services/auth.py" in md
    assert "42" in md
    assert "Hardcoded API secret key" in md


def test_markdown_formatter_clean():
    service = GitHubService()
    result = PRReviewResult(
        id="test-clean",
        verdict=ReviewVerdict.APPROVE,
        summary="Clean diff.",
        findings=[],
        metrics=ReviewMetrics(),
    )
    md = service.format_review_markdown(result)
    assert "APPROVE" in md
    assert "No defects, security issues, or anti-patterns detected." in md


def test_llm_json_parser_with_code_fences():
    reviewer = LLMReviewer()
    raw_response = """```json
{
  "verdict": "request_changes",
  "summary": "Detected unhandled database transaction and SQL formatting.",
  "findings": [
    {
      "file": "app/db.py",
      "line_hint": 55,
      "severity": "error",
      "category": "bug",
      "issue": "Session not rolled back upon exception.",
      "suggested_fix": "Wrap in try/except and call session.rollback()."
    }
  ]
}
```"""
    summary, verdict, findings, tokens = reviewer._parse_llm_json_response(raw_response, 350)
    assert verdict == ReviewVerdict.REQUEST_CHANGES
    assert "Unhandled database transaction" in summary.capitalize() or "Detected" in summary
    assert len(findings) == 1
    assert findings[0].file == "app/db.py"
    assert findings[0].line_hint == 55
    assert findings[0].severity == FindingSeverity.ERROR
    assert findings[0].category == FindingCategory.BUG
    assert tokens == 350

