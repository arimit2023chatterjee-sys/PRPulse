import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, Float, Text, DateTime, ForeignKey, Enum as SQLEnum
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class ReviewRunRecord(Base):
    __tablename__ = "review_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    repo = Column(String(255), nullable=True, index=True)
    pull_number = Column(Integer, nullable=True, index=True)
    commit_sha = Column(String(64), nullable=True)
    status = Column(String(32), default="success", index=True)
    verdict = Column(String(32), default="comment")
    summary = Column(Text, nullable=False)
    total_findings = Column(Integer, default=0)
    static_findings_count = Column(Integer, default=0)
    llm_findings_count = Column(Integer, default=0)
    tokens_used = Column(Integer, default=0)
    duration_ms = Column(Float, default=0.0)
    posted_to_github = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow, index=True)

    findings = relationship(
        "FindingRecord", back_populates="review_run", cascade="all, delete-orphan"
    )


class FindingRecord(Base):
    __tablename__ = "findings"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    review_run_id = Column(
        String(36), ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file = Column(String(512), nullable=False)
    line_hint = Column(Integer, nullable=True)
    severity = Column(String(32), nullable=False, index=True)
    category = Column(String(32), nullable=False, index=True)
    issue = Column(Text, nullable=False)
    suggested_fix = Column(Text, nullable=False)
    source = Column(String(32), default="llm")
    created_at = Column(DateTime, default=utcnow)

    review_run = relationship("ReviewRunRecord", back_populates="findings")

