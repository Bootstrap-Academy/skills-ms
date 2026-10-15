"""Lesson engine: package revisions, a learner's state per task, and submissions.

Revisions hold no personal data. Task states and submissions belong to one learner and are erased with the
account, submissions first. The database itself guards what must never happen twice: a second active revision of
a unit, a second open submission of a task, and a second plan for the same start (§9: a start carries one plan; its
exact repetition is found by start and plan hash). Task states and submissions point to a revision of their own
unit only.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped

from api.database import Base
from api.database.database import UTCDateTime

SUBMISSION_STATES = ("accepted", "booked")
VERDICTS = ("passed", "not_passed")


class EngineRevision(Base):
    """One published revision of an engine unit: the browser package it names and the server document."""

    __tablename__ = "skills_engine_revisions"
    __table_args__ = (
        UniqueConstraint("unit_id", "number", name="uq_engine_revision_number"),
        UniqueConstraint("unit_id", "server_sha256", "package_hash", name="uq_engine_revision_content"),
        # NULL for every inactive revision, the unit for the active one: at most one active revision per unit.
        UniqueConstraint("active_for", name="uq_engine_revision_active"),
        # The target of the composite foreign keys that keep task states and submissions on their own unit.
        UniqueConstraint("id", "unit_id", name="uq_engine_revision_unit"),
        CheckConstraint("active_for IS NULL OR active_for = unit_id", name="ck_engine_revision_active_for"),
        CheckConstraint("number >= 1", name="ck_engine_revision_number"),
        CheckConstraint(
            "length(package_hash) = 64 AND length(manifest_hash) = 64 AND length(server_sha256) = 64 "
            "AND length(evidence_sha256) = 64",
            name="ck_engine_revision_hashes",
        ),
        {"mysql_collate": "utf8mb4_bin"},
    )

    id: Mapped[str] = Column(String(36), primary_key=True)
    unit_id: Mapped[str] = Column(String(80), nullable=False)
    number: Mapped[int] = Column(Integer, nullable=False)
    active_for: Mapped[str | None] = Column(String(80), nullable=True)
    module_id: Mapped[str] = Column(String(128), ForeignKey("skills_lesson_modules.id"), nullable=False)
    package_hash: Mapped[str] = Column(String(64), nullable=False)
    manifest_hash: Mapped[str] = Column(String(64), nullable=False)
    entry_path: Mapped[str] = Column(String(512), nullable=False)
    server_document: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    server_sha256: Mapped[str] = Column(String(64), nullable=False)
    evidence_sha256: Mapped[str] = Column(String(64), nullable=False)
    # Drawn from the server document: id, required, xp, sub-skill and input kind of every task.
    tasks: Mapped[list[dict[str, Any]]] = Column(JSON, nullable=False)
    created_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)


class EngineTaskState(Base):
    """A learner's state of one task: its revision, band and counters, the current public start, the first pass."""

    __tablename__ = "skills_engine_task_states"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "unit_id"], [EngineRevision.id, EngineRevision.unit_id], name="fk_engine_task_revision"
        ),
        CheckConstraint("fails >= 0 AND passes >= 0", name="ck_engine_task_counters"),
        CheckConstraint("xp_amount IS NULL OR xp_amount >= 0", name="ck_engine_task_xp_amount"),
        CheckConstraint(
            "(start_id IS NULL AND start IS NULL AND start_band IS NULL) "
            "OR (start_id IS NOT NULL AND start IS NOT NULL AND start_band IS NOT NULL)",
            name="ck_engine_task_start",
        ),
        CheckConstraint(
            "NOT xp_applied OR (xp_operation_id IS NOT NULL AND xp_amount IS NOT NULL AND xp_skill_id IS NOT NULL "
            "AND passed_at IS NOT NULL)",
            name="ck_engine_task_xp",
        ),
        {"mysql_collate": "utf8mb4_bin"},
    )

    user_id: Mapped[str] = Column(String(36), primary_key=True)
    unit_id: Mapped[str] = Column(String(80), primary_key=True)
    goal_id: Mapped[str] = Column(String(80), primary_key=True)
    revision_id: Mapped[str] = Column(String(36), nullable=False)
    # As long as the band names of the engine (at most 80 characters).
    band: Mapped[str] = Column(String(80), nullable=False)
    fails: Mapped[int] = Column(Integer, nullable=False, default=0)
    passes: Mapped[int] = Column(Integer, nullable=False, default=0)
    # The public start as the learner got it (startId, revisionId, goalId, caseId, config, initial, seed) and the
    # band it was drawn in. The band stays with the server.
    start_id: Mapped[str | None] = Column(String(36), nullable=True)
    # None is SQL NULL here, so that ck_engine_task_start sees a missing start.
    start: Mapped[dict[str, Any] | None] = Column(JSON(none_as_null=True), nullable=True)
    start_band: Mapped[str | None] = Column(String(80), nullable=True)
    # A request id of the lesson protocol: up to 100 characters (engine002).
    start_request_id: Mapped[str | None] = Column(String(100), nullable=True)
    review_id: Mapped[str | None] = Column(String(36), nullable=True)
    passed_at: Mapped[datetime | None] = Column(UTCDateTime, nullable=True)
    xp_applied: Mapped[bool] = Column(Boolean, nullable=False, default=False)
    xp_operation_id: Mapped[str | None] = Column(String(36), nullable=True)
    xp_amount: Mapped[int | None] = Column(Integer, nullable=True)
    xp_skill_id: Mapped[str | None] = Column(String(256), nullable=True)
    updated_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)


class EngineSubmission(Base):
    """A submitted plan for one start. Accepted until its verdict is booked, then immutable."""

    __tablename__ = "skills_engine_submissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "unit_id", "goal_id"],
            [EngineTaskState.user_id, EngineTaskState.unit_id, EngineTaskState.goal_id],
            name="fk_engine_submission_task",
        ),
        ForeignKeyConstraint(
            ["revision_id", "unit_id"],
            [EngineRevision.id, EngineRevision.unit_id],
            name="fk_engine_submission_revision",
        ),
        # A start carries one plan (§9). The same plan again is the same submission, found by start and plan hash.
        UniqueConstraint("user_id", "unit_id", "goal_id", "start_id", name="uq_engine_submission_start"),
        # The goal while accepted, NULL once booked: at most one open submission per task.
        UniqueConstraint("user_id", "unit_id", "open_goal_id", name="uq_engine_submission_open"),
        CheckConstraint(
            f"state IN ({', '.join(repr(state) for state in SUBMISSION_STATES)})", name="ck_engine_submission_state"
        ),
        # Accepted: open for its goal, nothing of a verdict yet. Booked: closed, with verdict, time and `counts`;
        # only a pass counts (§7). `open_goal_id IS NOT NULL` matters: NULL = goal_id is not false.
        CheckConstraint(
            "(state = 'accepted' AND open_goal_id IS NOT NULL AND open_goal_id = goal_id AND verdict IS NULL "
            "AND booked_at IS NULL AND counts IS NULL AND feedback IS NULL) "
            "OR (state = 'booked' AND open_goal_id IS NULL AND verdict IS NOT NULL AND booked_at IS NOT NULL "
            "AND counts IS NOT NULL AND (NOT counts OR verdict = 'passed'))",
            name="ck_engine_submission_booking",
        ),
        CheckConstraint(
            f"verdict IS NULL OR verdict IN ({', '.join(repr(verdict) for verdict in VERDICTS)})",
            name="ck_engine_submission_verdict",
        ),
        CheckConstraint("length(input_hash) = 64", name="ck_engine_submission_input_hash"),
        Index("ix_engine_submissions_user", "user_id"),
        {"mysql_collate": "utf8mb4_bin"},
    )

    id: Mapped[str] = Column(String(36), primary_key=True)
    user_id: Mapped[str] = Column(String(36), nullable=False)
    unit_id: Mapped[str] = Column(String(80), nullable=False)
    goal_id: Mapped[str] = Column(String(80), nullable=False)
    open_goal_id: Mapped[str | None] = Column(String(80), nullable=True)
    revision_id: Mapped[str] = Column(String(36), nullable=False)
    start_id: Mapped[str] = Column(String(36), nullable=False)
    input_hash: Mapped[str] = Column(String(64), nullable=False)
    input: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    # Frozen at acceptance: revision, public start, band and server hash. Only these decide the verdict.
    job: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    state: Mapped[str] = Column(String(16), nullable=False)
    verdict: Mapped[str | None] = Column(String(16), nullable=True)
    # The filtered feedback the learner may see; never case ids, seeds, coordinates or raw faults.
    feedback: Mapped[dict[str, Any] | None] = Column(JSON(none_as_null=True), nullable=True)
    counts: Mapped[bool | None] = Column(Boolean, nullable=True)
    accepted_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    booked_at: Mapped[datetime | None] = Column(UTCDateTime, nullable=True)
    # Until when an evaluation in the sandbox holds this submission (Ä33); a later one may take it over.
    evaluating_until: Mapped[datetime | None] = Column(UTCDateTime, nullable=True)
