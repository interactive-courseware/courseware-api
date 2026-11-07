from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase


def now():
    return datetime.now(timezone.utc)


def uid():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(Uuid(as_uuid=False), primary_key=True, default=uid)
    username = Column(String(80), nullable=False, unique=True)
    password_hash = Column(Text, nullable=False)
    display_name = Column(String(100), nullable=False)
    role = Column(String(20), nullable=False, default="member")
    status = Column(String(20), nullable=False, default="active")
    must_change_password = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=now, nullable=False)
    __table_args__ = (
        CheckConstraint("role IN ('admin','member')"),
        CheckConstraint("status IN ('active','disabled')"),
    )


class SessionToken(Base):
    __tablename__ = "sessions"
    id = Column(Uuid(as_uuid=False), primary_key=True, default=uid)
    user_id = Column(ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), default=now, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), default=now, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True))


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id = Column(Uuid(as_uuid=False), primary_key=True, default=uid)
    username = Column(String(80), nullable=False, index=True)
    ip = Column(String(128), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=now, nullable=False, index=True)


class Course(Base):
    __tablename__ = "courses"
    id = Column(Uuid(as_uuid=False), primary_key=True, default=uid)
    slug = Column(String(80), unique=True, nullable=False)
    listing_status = Column(String(20), nullable=False, default="unlisted")
    access_policy = Column(String(20), nullable=False, default="authenticated")
    display_order = Column(Integer, nullable=False, default=0)
    featured = Column(Boolean, nullable=False, default=False)
    grants_version = Column(Integer, nullable=False, default=0)
    current_release_id = Column(Uuid(as_uuid=False), nullable=True)
    published_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=now, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["id", "current_release_id"],
            ["releases.course_id", "releases.id"],
            name="fk_current_course_release",
            use_alter=True,
        ),
        CheckConstraint("listing_status IN ('listed','unlisted')"),
        CheckConstraint("access_policy IN ('authenticated','restricted')"),
    )


class CourseSource(Base):
    __tablename__ = "course_sources"
    course_id = Column(ForeignKey("courses.id"), primary_key=True)
    repo_url = Column(Text, nullable=False)
    branch = Column(String(200), nullable=False)
    credential_ref = Column(String(80))
    enabled = Column(Boolean, nullable=False, default=True)


class CourseGrant(Base):
    __tablename__ = "course_grants"
    course_id = Column(ForeignKey("courses.id"), primary_key=True)
    user_id = Column(ForeignKey("users.id"), primary_key=True)
    granted_by = Column(ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), default=now, nullable=False)


class Release(Base):
    __tablename__ = "releases"
    id = Column(Uuid(as_uuid=False), primary_key=True, default=uid)
    course_id = Column(ForeignKey("courses.id"), nullable=False, index=True)
    source_snapshot = Column(JSONB, nullable=False)
    commit_sha = Column(String(64), nullable=False)
    schema_version = Column(String(20), nullable=False)
    validator_version = Column(String(80), nullable=False)
    manifest_hash = Column(String(64), nullable=False)
    manifest_json = Column(JSONB, nullable=False)
    artifact_hash = Column(String(64), nullable=False)
    status = Column(String(20), nullable=False, default="ready")
    revoked_at = Column(DateTime(timezone=True))
    serving_until = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=now, nullable=False)
    __table_args__ = (
        UniqueConstraint("course_id", "id"),
        UniqueConstraint(
            "course_id", "commit_sha", "manifest_hash", "validator_version", name="uq_release_content"
        ),
    )


class ReleaseFile(Base):
    __tablename__ = "release_files"
    release_id = Column(ForeignKey("releases.id"), primary_key=True)
    path = Column(String(512), primary_key=True)
    sha256 = Column(String(64), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    media_type = Column(String(100), nullable=False)


class CourseMetadata(Base):
    __tablename__ = "course_metadata"
    release_id = Column(ForeignKey("releases.id"), primary_key=True)
    title = Column(String(120), nullable=False)
    summary = Column(Text, nullable=False)
    description = Column(Text, nullable=False)
    language = Column(String(35), nullable=False)
    level = Column(String(20))
    tags = Column(JSONB, nullable=False)
    entry = Column(String(512), nullable=False)
    search_text = Column(Text, nullable=False)
    __table_args__ = (
        Index(
            "ix_metadata_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
        Index("ix_metadata_tags", "tags", postgresql_using="gin"),
    )


class Image(Base):
    __tablename__ = "course_images"
    release_id = Column(ForeignKey("releases.id"), primary_key=True)
    id = Column(String(80), primary_key=True)
    role = Column(String(20), nullable=False)
    path = Column(String(512), nullable=False)
    alt = Column(String(200), nullable=False)
    width = Column(Integer)
    height = Column(Integer)
    position = Column(Integer, nullable=False)


class Creator(Base):
    __tablename__ = "course_creators"
    release_id = Column(ForeignKey("releases.id"), primary_key=True)
    id = Column(String(80), primary_key=True)
    name = Column(String(100), nullable=False)
    roles = Column(JSONB, nullable=False)
    organization = Column(String(200))
    team = Column(String(200))
    bio = Column(Text)
    avatar = Column(JSONB)
    position = Column(Integer, nullable=False)


class Group(Base):
    __tablename__ = "course_groups"
    release_id = Column(ForeignKey("releases.id"), primary_key=True)
    id = Column(String(80), primary_key=True)
    title = Column(String(120), nullable=False)
    summary = Column(Text)
    position = Column(Integer, nullable=False)


class Chapter(Base):
    __tablename__ = "chapters"
    release_id = Column(ForeignKey("releases.id"), primary_key=True)
    id = Column(String(80), primary_key=True)
    parent_id = Column(String(80))
    title = Column(String(120), nullable=False)
    summary = Column(Text)
    position = Column(Integer, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["release_id", "parent_id"],
            ["chapters.release_id", "chapters.id"],
            deferrable=True,
            initially="DEFERRED",
        ),
    )


class Lesson(Base):
    __tablename__ = "lessons"
    release_id = Column(ForeignKey("releases.id"), primary_key=True)
    id = Column(String(80), primary_key=True)
    group_id = Column(String(80), nullable=False)
    title = Column(String(120), nullable=False)
    summary = Column(Text, nullable=False)
    kind = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False)
    entry = Column(String(512))
    keywords = Column(JSONB, nullable=False)
    duration_minutes = Column(Integer)
    cover_image_id = Column(String(80))
    position = Column(Integer, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["release_id", "group_id"], ["course_groups.release_id", "course_groups.id"]),
        ForeignKeyConstraint(
            ["release_id", "cover_image_id"], ["course_images.release_id", "course_images.id"]
        ),
        CheckConstraint("(status='published' AND entry IS NOT NULL) OR (status='planned' AND entry IS NULL)"),
    )


class LessonChapter(Base):
    __tablename__ = "lesson_chapters"
    release_id = Column(Uuid(as_uuid=False), primary_key=True)
    lesson_id = Column(String(80), primary_key=True)
    chapter_id = Column(String(80), primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(["release_id", "lesson_id"], ["lessons.release_id", "lessons.id"]),
        ForeignKeyConstraint(["release_id", "chapter_id"], ["chapters.release_id", "chapters.id"]),
    )


class LessonCreator(Base):
    __tablename__ = "lesson_creators"
    release_id = Column(Uuid(as_uuid=False), primary_key=True)
    lesson_id = Column(String(80), primary_key=True)
    creator_id = Column(String(80), primary_key=True)
    position = Column(Integer, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["release_id", "lesson_id"], ["lessons.release_id", "lessons.id"]),
        ForeignKeyConstraint(
            ["release_id", "creator_id"], ["course_creators.release_id", "course_creators.id"]
        ),
    )


class Resource(Base):
    __tablename__ = "resources"
    release_id = Column(ForeignKey("releases.id"), primary_key=True)
    id = Column(String(80), primary_key=True)
    title = Column(String(120), nullable=False)
    kind = Column(String(20), nullable=False)
    path = Column(String(512), nullable=False)
    position = Column(Integer, nullable=False)


class ResourceLesson(Base):
    __tablename__ = "resource_lessons"
    release_id = Column(Uuid(as_uuid=False), primary_key=True)
    resource_id = Column(String(80), primary_key=True)
    lesson_id = Column(String(80), primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(["release_id", "resource_id"], ["resources.release_id", "resources.id"]),
        ForeignKeyConstraint(["release_id", "lesson_id"], ["lessons.release_id", "lessons.id"]),
    )


class SyncJob(Base):
    __tablename__ = "sync_jobs"
    id = Column(Uuid(as_uuid=False), primary_key=True, default=uid)
    course_id = Column(ForeignKey("courses.id"), nullable=False)
    requested_by = Column(ForeignKey("users.id"), nullable=False)
    trigger = Column(String(20), nullable=False, default="admin")
    source_snapshot = Column(JSONB, nullable=False)
    status = Column(String(20), nullable=False, default="queued")
    stage = Column(String(80), nullable=False, default="等待执行")
    commit_sha = Column(String(64))
    release_id = Column(ForeignKey("releases.id"))
    idempotency_key = Column(String(100))
    lease_owner = Column(String(80))
    heartbeat_at = Column(DateTime(timezone=True))
    error_code = Column(String(80))
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), default=now, nullable=False)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    __table_args__ = (
        Index(
            "uq_active_course_job",
            "course_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running')"),
        ),
        UniqueConstraint("course_id", "requested_by", "idempotency_key", name="uq_job_idempotency"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id = Column(Uuid(as_uuid=False), primary_key=True, default=uid)
    actor_user_id = Column(ForeignKey("users.id"), nullable=False)
    action = Column(String(80), nullable=False)
    course_id = Column(ForeignKey("courses.id"))
    release_id = Column(ForeignKey("releases.id"))
    details = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=now, nullable=False)
