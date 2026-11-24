from datetime import datetime
from enum import Enum
from typing import List, Literal, Optional
from pydantic import BaseModel, Field, HttpUrl, field_validator


class FindingSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class FindingCategory(str, Enum):
    BUG = "bug"
    SECURITY = "security"
    EDGE_CASE = "edge_case"
    MAINTAINABILITY = "maintainability"
    PERFORMANCE = "performance"


class ReviewVerdict(str, Enum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    COMMENT = "comment"


class ReviewFinding(BaseModel):
    file: str = Field(..., description="File path relative to repository root")
    line_hint: Optional[int] = Field(
        None, description="Line number in the new/modified file, if applicable"
    )
    severity: FindingSeverity = Field(
        ..., description="Severity of the issue (info, warning, error, critical)"
    )
    category: FindingCategory = Field(
        ..., description="Category (bug, security, edge_case, maintainability, performance)"
    )
    issue: str = Field(..., description="Concise description of the detected defect or risk")
    suggested_fix: str = Field(
        ..., description="Actionable fix or refactored code snippet resolving the issue"
    )
    source: Literal["static", "llm"] = Field(
        "llm", description="Whether finding originated from deterministic static check or LLM pass"
    )


class ReviewMetrics(BaseModel):
    total_files: int = 0
    analyzed_files: int = 0
    filtered_files: int = 0
    static_findings_count: int = 0
    llm_findings_count: int = 0
    total_findings: int = 0
    tokens_used: int = 0
    duration_ms: float = 0.0


class PRReviewResult(BaseModel):
    id: Optional[str] = None
    repo: Optional[str] = None
    pull_number: Optional[int] = None
    commit_sha: Optional[str] = None
    verdict: ReviewVerdict = ReviewVerdict.COMMENT
    summary: str = Field(..., description="High-level evaluation and executive summary of PR health")
    findings: List[ReviewFinding] = Field(default_factory=list)
    metrics: ReviewMetrics = Field(default_factory=ReviewMetrics)
    posted_to_github: bool = False
    created_at: Optional[datetime] = None


class PRReviewRequest(BaseModel):
    repo: Optional[str] = Field(None, json_schema_extra={"example": "octocat/Hello-World"})
    pull_number: Optional[int] = Field(None, json_schema_extra={"example": 42})
    pr_url: Optional[str] = Field(
        None, json_schema_extra={"example": "https://github.com/octocat/Hello-World/pull/42"}
    )
    post_to_github: bool = Field(
        False, description="Whether to publish comment back to the GitHub PR"
    )

    @field_validator("repo")
    @classmethod
    def validate_repo_format(cls, v: Optional[str]) -> Optional[str]:
        if v and "/" not in v:
            raise ValueError("Repository must be in 'owner/repo' format")
        return v


class DiffReviewRequest(BaseModel):
    diff_text: str = Field(..., min_length=1, description="Raw unified diff content")
    title: Optional[str] = Field("Manual Diff Review", description="Label or title for this review")


class WebhookEventResponse(BaseModel):
    status: str
    action: Optional[str] = None
    repo: Optional[str] = None
    pull_number: Optional[int] = None
    review_id: Optional[str] = None
    detail: Optional[str] = None
