"""Read or recompute a submission's comparison against official notices."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from finalsay.auth import ensure_notice_access, get_current_user, require_role
from finalsay.db import get_db
from finalsay.models import Notice, NoticeDocumentExtraction, RelationEdge, ReviewCase, User
from finalsay.schemas import ComparisonResponse
from finalsay.services import comparison

router = APIRouter(prefix="/api/compare", tags=["comparison"])


def comparison_response(
    db: Session,
    edge: RelationEdge,
    review_case: ReviewCase | None,
    *,
    candidate_score: float | None = None,
    evidence: list[str] | None = None,
) -> ComparisonResponse:
    # No-match edges point back to the submission as a storage sentinel.
    candidate = db.get(Notice, edge.dst_notice_id) if edge.dst_notice_id != edge.src_notice_id else None
    extractions = (
        list(db.scalars(
            select(NoticeDocumentExtraction)
            .where(NoticeDocumentExtraction.notice_id == candidate.id)
            .order_by(NoticeDocumentExtraction.extracted_at.desc())
        ))
        if candidate else []
    )
    return ComparisonResponse(
        submission_id=edge.src_notice_id,
        candidate_id=candidate.id if candidate else None,
        edge_id=edge.id,
        label=edge.label,
        confidence=edge.confidence,
        rationale=edge.rationale or "",
        model=edge.model or "",
        status=edge.status,
        gated=review_case is not None and review_case.status == "open",
        review_case_id=review_case.id if review_case else None,
        candidate_score=candidate_score,
        candidate_institution=candidate.institution.name if candidate and candidate.institution else None,
        candidate_issuer=candidate.issuer if candidate else None,
        candidate_notice_date=candidate.notice_date if candidate else None,
        candidate_deadline=candidate.deadline if candidate else None,
        candidate_source_url=candidate.source_url if candidate else None,
        candidate_text=candidate.redacted_text if candidate else None,
        candidate_supplemental_extractions=extractions,
        evidence=evidence or [],
    )


def _accessible_submission(db: Session, submission_id: int, user: User) -> Notice:
    submission = db.get(Notice, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail=f"notice {submission_id} not found")
    if submission.kind != "submission":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="comparison requires a submission notice",
        )
    ensure_notice_access(submission, user)
    return submission


@router.get("/{submission_id}", response_model=ComparisonResponse)
def get_comparison(
    submission_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ComparisonResponse:
    """Return the saved result, including any reviewer decision, without rerunning the model."""
    submission = _accessible_submission(db, submission_id, current_user)
    edge = db.scalar(
        select(RelationEdge)
        .where(RelationEdge.src_notice_id == submission.id)
        .order_by(RelationEdge.id.desc())
        .limit(1)
    )
    if edge is None:
        raise HTTPException(status_code=404, detail="No comparison has been recorded for this submission.")
    review_case = db.scalar(select(ReviewCase).where(ReviewCase.edge_id == edge.id))
    return comparison_response(db, edge, review_case)


@router.post("/{submission_id}", response_model=ComparisonResponse)
def compare(
    submission_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("student", "admin")),
) -> ComparisonResponse:
    submission = _accessible_submission(db, submission_id, current_user)
    outcome = comparison.classify_submission(db, submission)
    db.commit()
    return comparison_response(
        db, outcome.edge, outcome.review_case,
        candidate_score=outcome.candidate_score,
        evidence=outcome.evidence,
    )
