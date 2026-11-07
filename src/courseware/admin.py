from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query
from pydantic import Field
from sqlalchemy import delete, func, select, text

from .auth import DB, Admin, Input, hasher, revoke_sessions, user_dto
from .dto import Page, UserView
from .errors import Problem
from .models import AuditEvent, Course, CourseGrant, CourseSource, Release, SyncJob, User, now
from .publishing import activate, active_job, course_lock, require_idle, verify_artifact
from .worker import validate_source

router = APIRouter(prefix="/api/v1/admin", tags=["Administration"])


class UserCreate(Input):
    username: str = Field(pattern=r"^[a-z][a-z0-9._-]{2,79}$")
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=200)
    role: Literal["admin", "member"] = "member"


class UserPatch(Input):
    display_name: str | None = Field(None, min_length=1, max_length=100)
    role: Literal["admin", "member"] | None = None
    status: Literal["active", "disabled"] | None = None


class ResetPassword(Input):
    password: str = Field(min_length=12, max_length=200)


class SourceInput(Input):
    repo_url: str = Field(max_length=1000)
    branch: str = Field(default="main", max_length=200)
    credential_ref: str | None = Field(None, max_length=80)
    enabled: bool = True


class CourseCreate(SourceInput):
    slug: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class CoursePatch(Input):
    listing_status: Literal["listed", "unlisted"] | None = None
    access_policy: Literal["authenticated", "restricted"] | None = None
    display_order: int | None = None
    featured: bool | None = None


class GrantsInput(Input):
    user_ids: list[UUID] = Field(max_length=10000)
    expected_version: int = Field(ge=0)


class ActivateInput(Input):
    release_id: UUID
    expected_current_release_id: UUID | None
    reason: str = Field(min_length=1, max_length=500)


def audit(db, actor, action, course=None, **details):
    db.add(
        AuditEvent(
            actor_user_id=actor.id, action=action, course_id=course.id if course else None, details=details
        )
    )


def course_admin_dto(db, course):
    source = db.get(CourseSource, course.id)
    release = db.get(Release, course.current_release_id) if course.current_release_id else None
    job = db.scalar(
        select(SyncJob).where(SyncJob.course_id == course.id).order_by(SyncJob.created_at.desc()).limit(1)
    )
    return {
        "id": course.id,
        "slug": course.slug,
        "title": release.manifest_json["title"] if release else course.slug,
        "listing_status": course.listing_status,
        "access_policy": course.access_policy,
        "display_order": course.display_order,
        "featured": course.featured,
        "current_release_id": course.current_release_id,
        "published_at": course.published_at,
        "source": {
            "repo_url": source.repo_url,
            "branch": source.branch,
            "credential_ref": source.credential_ref,
            "enabled": source.enabled,
        }
        if source
        else None,
        "latest_job": job_dto(job) if job else None,
    }


def job_dto(job):
    return {
        "id": job.id,
        "course_id": job.course_id,
        "status": job.status,
        "stage": job.stage,
        "commit_sha": job.commit_sha,
        "release_id": job.release_id,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


@router.get("/users", response_model=Page[UserView])
def users(db: DB, actor: Admin, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    rows = db.scalars(
        select(User).order_by(User.created_at, User.id).offset((page - 1) * page_size).limit(page_size)
    )
    return {
        "items": [user_dto(u) for u in rows],
        "page": page,
        "page_size": page_size,
        "total": db.scalar(select(func.count()).select_from(User)),
    }


@router.post("/users", status_code=201, response_model=UserView)
def create_user(body: UserCreate, db: DB, actor: Admin):
    if db.scalar(select(User).where(User.username == body.username)):
        raise Problem(409, "USERNAME_EXISTS", "用户名已存在")
    user = User(
        username=body.username,
        display_name=body.display_name,
        password_hash=hasher.hash(body.password),
        role=body.role,
        must_change_password=True,
    )
    db.add(user)
    audit(db, actor, "user_created", username=body.username)
    db.commit()
    return user_dto(user)


@router.patch("/users/{user_id}", response_model=UserView)
def patch_user(user_id: UUID, body: UserPatch, db: DB, actor: Admin):
    db.execute(text("SELECT pg_advisory_xact_lock(8102)"))
    user = db.scalar(
        select(User)
        .where(User.id == str(user_id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not user:
        raise Problem(404, "USER_NOT_FOUND", "用户不存在")
    changes = body.model_dump(exclude_none=True)
    if (
        user.role == "admin"
        and user.status == "active"
        and (changes.get("role", user.role) != "admin" or changes.get("status", user.status) != "active")
    ):
        remaining = db.scalar(
            select(func.count()).select_from(User).where(User.role == "admin", User.status == "active")
        )
        if remaining <= 1:
            raise Problem(409, "LAST_ADMIN", "不能停用或降级最后一个启用的管理员")
    for key, value in changes.items():
        setattr(user, key, value)
    if "role" in changes or "status" in changes:
        revoke_sessions(db, user.id)
    audit(db, actor, "user_updated", user_id=user.id, changes=changes)
    db.commit()
    return user_dto(user)


@router.post("/users/{user_id}/reset-password", status_code=204)
def reset_password(user_id: UUID, body: ResetPassword, db: DB, actor: Admin):
    user = db.scalar(select(User).where(User.id == str(user_id)).with_for_update())
    if not user:
        raise Problem(404, "USER_NOT_FOUND", "用户不存在")
    user.password_hash = hasher.hash(body.password)
    user.must_change_password = True
    revoke_sessions(db, user.id)
    audit(db, actor, "password_reset", user_id=user.id)
    db.commit()


@router.get("/courses")
def courses(db: DB, actor: Admin, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    rows = db.scalars(
        select(Course).order_by(Course.created_at, Course.id).offset((page - 1) * page_size).limit(page_size)
    )
    return {
        "items": [course_admin_dto(db, c) for c in rows],
        "page": page,
        "page_size": page_size,
        "total": db.scalar(select(func.count()).select_from(Course)),
    }


@router.post("/courses", status_code=201)
def create_course(body: CourseCreate, db: DB, actor: Admin):
    validate_source(body.repo_url, body.branch, body.credential_ref)
    if db.scalar(select(Course).where(Course.slug == body.slug)):
        raise Problem(409, "SLUG_EXISTS", "课程标识已登记")
    course = Course(slug=body.slug)
    db.add(course)
    db.flush()
    db.add(CourseSource(course_id=course.id, **body.model_dump(exclude={"slug"})))
    audit(db, actor, "course_registered", course)
    db.commit()
    return course_admin_dto(db, course)


@router.patch("/courses/{course_id}")
def patch_course(course_id: UUID, body: CoursePatch, db: DB, actor: Admin):
    course = course_lock(db, str(course_id))
    changes = body.model_dump(exclude_none=True)
    if changes.get("listing_status") == "listed":
        release = db.get(Release, course.current_release_id) if course.current_release_id else None
        if not release or release.status != "ready" or release.revoked_at:
            raise Problem(409, "NO_READY_RELEASE", "请先成功导入一个版本再上架")
    for key, value in changes.items():
        setattr(course, key, value)
    audit(db, actor, "course_updated", course, changes=changes)
    db.commit()
    return course_admin_dto(db, course)


@router.put("/courses/{course_id}/source")
def set_source(course_id: UUID, body: SourceInput, db: DB, actor: Admin):
    validate_source(body.repo_url, body.branch, body.credential_ref)
    course = course_lock(db, str(course_id))
    source = db.get(CourseSource, course.id)
    if not source:
        source = CourseSource(course_id=course.id)
        db.add(source)
    for key, value in body.model_dump().items():
        setattr(source, key, value)
    audit(db, actor, "course_source_updated", course)
    db.commit()
    return course_admin_dto(db, course)


@router.get("/courses/{course_id}/grants")
def grants(course_id: UUID, db: DB, actor: Admin):
    course = db.get(Course, str(course_id))
    if not course:
        raise Problem(404, "COURSE_NOT_FOUND", "课程不存在")
    return {
        "version": course.grants_version,
        "user_ids": list(db.scalars(select(CourseGrant.user_id).where(CourseGrant.course_id == course.id))),
    }


@router.put("/courses/{course_id}/grants")
def replace_grants(course_id: UUID, body: GrantsInput, db: DB, actor: Admin):
    course = course_lock(db, str(course_id))
    if course.grants_version != body.expected_version:
        raise Problem(409, "GRANTS_CHANGED", "授权已被其他操作修改，请刷新后重试")
    ids = {str(i) for i in body.user_ids}
    if len(ids) != len(body.user_ids) or db.scalar(
        select(func.count()).select_from(User).where(User.id.in_(ids))
    ) != len(ids):
        raise Problem(422, "GRANTS_INVALID", "授权用户重复或不存在")
    db.execute(delete(CourseGrant).where(CourseGrant.course_id == course.id))
    db.add_all(CourseGrant(course_id=course.id, user_id=i, granted_by=actor.id) for i in ids)
    course.grants_version += 1
    audit(db, actor, "course_grants_updated", course, user_ids=sorted(ids))
    db.commit()
    return {"version": course.grants_version, "user_ids": sorted(ids)}


@router.post("/courses/{course_id}/sync", status_code=202)
def sync(course_id: UUID, db: DB, actor: Admin, idempotency_key: str | None = Header(None, max_length=100)):
    course = course_lock(db, str(course_id))
    if idempotency_key:
        previous = db.scalar(
            select(SyncJob).where(
                SyncJob.course_id == course.id,
                SyncJob.requested_by == actor.id,
                SyncJob.idempotency_key == idempotency_key,
            )
        )
        if previous:
            return job_dto(previous)
    if active_job(db, course.id):
        raise Problem(409, "COURSE_BUSY", "该课程已有更新任务")
    source = db.get(CourseSource, course.id)
    if not source or not source.enabled:
        raise Problem(409, "SOURCE_DISABLED", "请先配置并启用课程仓库源")
    snapshot = {
        "repo_url": source.repo_url,
        "branch": source.branch,
        "credential_ref": source.credential_ref,
        "manifest_path": "course.json",
        "kind": "git",
    }
    validate_source(source.repo_url, source.branch, source.credential_ref)
    job = SyncJob(
        course_id=course.id, requested_by=actor.id, source_snapshot=snapshot, idempotency_key=idempotency_key
    )
    db.add(job)
    audit(db, actor, "sync_requested", course)
    db.commit()
    return job_dto(job)


@router.get("/jobs/{job_id}")
def job_status(job_id: UUID, db: DB, actor: Admin):
    job = db.get(SyncJob, str(job_id))
    if not job:
        raise Problem(404, "JOB_NOT_FOUND", "更新任务不存在")
    return job_dto(job)


@router.get("/courses/{course_id}/releases")
def releases(course_id: UUID, db: DB, actor: Admin):
    course = db.get(Course, str(course_id))
    if not course:
        raise Problem(404, "COURSE_NOT_FOUND", "课程不存在")
    rows = db.scalars(
        select(Release).where(Release.course_id == course.id).order_by(Release.created_at.desc())
    )
    return {
        "current_release_id": course.current_release_id,
        "items": [
            {
                "id": r.id,
                "title": r.manifest_json["title"],
                "commit_sha": r.commit_sha,
                "status": r.status,
                "created_at": r.created_at,
                "revoked_at": r.revoked_at,
                "serving_until": r.serving_until,
                "source_kind": r.source_snapshot.get("kind", "git"),
            }
            for r in rows
        ],
    }


@router.post("/courses/{course_id}/activate")
def activate_release(course_id: UUID, body: ActivateInput, db: DB, actor: Admin):
    course = course_lock(db, str(course_id))
    require_idle(db, course.id)
    expected = str(body.expected_current_release_id) if body.expected_current_release_id else None
    if course.current_release_id != expected:
        raise Problem(409, "CURRENT_RELEASE_CHANGED", "当前版本已变化，请刷新后重试")
    release = db.get(Release, str(body.release_id))
    if not release or release.course_id != course.id:
        raise Problem(404, "RELEASE_NOT_FOUND", "版本不存在")
    verify_artifact(db, course, release)
    activate(db, course, release, actor.id, body.reason)
    db.commit()
    return course_admin_dto(db, course)


@router.post("/courses/{course_id}/releases/{release_id}/revoke", status_code=204)
def revoke(course_id: UUID, release_id: UUID, db: DB, actor: Admin):
    course = course_lock(db, str(course_id))
    require_idle(db, course.id)
    release = db.get(Release, str(release_id))
    if not release or release.course_id != course.id:
        raise Problem(404, "RELEASE_NOT_FOUND", "版本不存在")
    if course.current_release_id == release.id and course.listing_status == "listed":
        raise Problem(409, "CURRENT_RELEASE_IN_USE", "请先下架课程或切换其他版本")
    release.revoked_at = now()
    audit(db, actor, "release_revoked", course, release_id=release.id)
    db.commit()
