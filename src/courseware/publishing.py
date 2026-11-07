import hashlib
import json
import logging
import os
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .contract import VALIDATOR_VERSION, load_manifest, validate_package
from .errors import Problem
from .models import (
    AuditEvent,
    Chapter,
    Course,
    CourseMetadata,
    Creator,
    Group,
    Image,
    Lesson,
    LessonChapter,
    LessonCreator,
    Release,
    ReleaseFile,
    Resource,
    ResourceLesson,
    SyncJob,
    now,
    uid,
)


def course_lock(db: Session, course_id: str):
    course = db.scalar(select(Course).where(Course.id == course_id).with_for_update())
    if not course:
        raise Problem(404, "COURSE_NOT_FOUND", "课程不存在")
    return course


def active_job(db, course_id):
    return db.scalar(
        select(SyncJob).where(SyncJob.course_id == course_id, SyncJob.status.in_(["queued", "running"]))
    )


def require_idle(db, course_id):
    if active_job(db, course_id):
        raise Problem(409, "COURSE_BUSY", "课程正在更新，请等待任务完成")


def search_document(data):
    texts = [data["title"], data["summary"], *data["tags"]]
    for creator in data["creators"]:
        texts.extend(creator.get(key, "") for key in ("name", "organization", "team"))
    texts.extend(chapter["title"] for chapter in data["chapters"])
    for lesson in data["lessons"]:
        if lesson["status"] == "published":
            texts.extend([lesson["title"], lesson["summary"], *lesson["keywords"]])
    return "\n".join(texts)


def save_metadata(db: Session, release: Release, files):
    data, rid = release.manifest_json, release.id
    db.add(
        CourseMetadata(
            release_id=rid,
            **{
                k: data.get(k)
                for k in ("title", "summary", "description", "language", "level", "tags", "entry")
            },
            search_text=search_document(data),
        )
    )
    for model, key in [(Image, "images"), (Creator, "creators"), (Group, "groups"), (Chapter, "chapters")]:
        for position, row in enumerate(data[key]):
            db.add(model(release_id=rid, position=position, **row))
    db.add_all([ReleaseFile(release_id=rid, **file) for file in files])
    db.flush()
    for position, row in enumerate(data["lessons"]):
        db.add(
            Lesson(
                release_id=rid,
                position=position,
                **{k: v for k, v in row.items() if k not in {"chapter_ids", "creator_ids"}},
            )
        )
    for position, row in enumerate(data["resources"]):
        db.add(
            Resource(release_id=rid, position=position, **{k: v for k, v in row.items() if k != "lesson_ids"})
        )
    db.flush()
    for row in data["lessons"]:
        for cid in row["chapter_ids"]:
            db.add(LessonChapter(release_id=rid, lesson_id=row["id"], chapter_id=cid))
        for position, cid in enumerate(row["creator_ids"]):
            db.add(LessonCreator(release_id=rid, lesson_id=row["id"], creator_id=cid, position=position))
    for row in data["resources"]:
        for lid in row["lesson_ids"]:
            db.add(ResourceLesson(release_id=rid, resource_id=row["id"], lesson_id=lid))
    db.flush()


def verify_artifact(db, course, release):
    root = settings().storage_path / "releases" / course.id / release.id
    files = db.scalars(select(ReleaseFile).where(ReleaseFile.release_id == release.id)).all()
    if not files:
        raise Problem(409, "ARTIFACT_MISSING", "版本文件清单缺失，不能激活")
    for item in files:
        path = root / item.path
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise Problem(409, "ARTIFACT_MISSING", "版本文件缺失，不能激活")
        with path.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != item.sha256:
                raise Problem(409, "ARTIFACT_CHANGED", "版本文件校验失败，不能激活")


def activate(db, course, release, actor_id, reason):
    if release.course_id != course.id or release.status != "ready" or release.revoked_at:
        raise Problem(409, "RELEASE_INVALID", "目标版本不可激活")
    if course.current_release_id and course.current_release_id != release.id:
        previous = db.get(Release, course.current_release_id)
        previous.serving_until = now() + timedelta(hours=24)
    course.current_release_id = release.id
    course.published_at = now()
    release.serving_until = None
    db.add(
        AuditEvent(
            actor_user_id=actor_id,
            action="release_activated",
            course_id=course.id,
            release_id=release.id,
            details={"reason": reason},
        )
    )


def publish_directory(db: Session, job_id: str, root: Path, commit_sha: str, owner: str):
    job = db.get(SyncJob, job_id)
    if not job or job.status != "running" or job.lease_owner != owner:
        raise Problem(409, "JOB_LEASE_LOST", "任务租约已失效")
    course = course_lock(db, job.course_id)
    raw = (root / "course.json").read_bytes()
    data = load_manifest(raw, course.slug)
    files = validate_package(root.resolve(), data)
    manifest_hash = hashlib.sha256(raw).hexdigest()
    existing = db.scalar(
        select(Release).where(
            Release.course_id == course.id,
            Release.commit_sha == commit_sha,
            Release.manifest_hash == manifest_hash,
            Release.validator_version == VALIDATOR_VERSION,
        )
    )
    if existing:
        job.status, job.stage, job.release_id = "succeeded", "内容已存在，保留当前版本", existing.id
        job.commit_sha, job.finished_at = commit_sha, now()
        db.commit()
        return existing
    storage = settings().storage_path
    (storage / "work").mkdir(parents=True, exist_ok=True)
    size = sum(item["size_bytes"] for item in files)
    if shutil.disk_usage(storage).free < size + 100 * 1024 * 1024:
        raise Problem(422, "STORAGE_FULL", "发布磁盘剩余空间不足")
    rid = uid()
    target = storage / "releases" / course.id / rid
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="publish-", dir=storage / "work"))
    try:
        for record in files:
            destination = stage / record["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            with (root / record["path"]).open("rb") as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            with destination.open("rb") as check:
                if hashlib.file_digest(check, "sha256").hexdigest() != record["sha256"]:
                    raise Problem(409, "SOURCE_CHANGED", "发布过程中源文件发生变化，请重试")
        # Persist directory entries before the database exposes this immutable package.
        for directory in [p for p in stage.rglob("*") if p.is_dir()] + [stage]:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        os.replace(stage, target)
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        release = Release(
            id=rid,
            course_id=course.id,
            source_snapshot=job.source_snapshot,
            commit_sha=commit_sha,
            schema_version=data["schema_version"],
            validator_version=VALIDATOR_VERSION,
            manifest_hash=manifest_hash,
            manifest_json=data,
            artifact_hash=hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
        )
        db.add(release)
        db.flush()
        save_metadata(db, release, files)
        # Recheck the lease under a row lock before making the candidate visible.
        db.refresh(job, with_for_update=True)
        if job.status != "running" or job.lease_owner != owner:
            raise Problem(409, "JOB_LEASE_LOST", "任务租约已失效")
        activate(db, course, release, job.requested_by, "课程同步")
        job.status, job.stage, job.release_id = "succeeded", "发布完成", rid
        job.commit_sha, job.finished_at = commit_sha, now()
        db.commit()
        return release
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def fail_job(db, job_id, exc):
    logging.getLogger("courseware.worker").error("job_failed job_id=%s type=%s", job_id, type(exc).__name__)
    db.rollback()
    job = db.get(SyncJob, job_id)
    if job and job.status in {"queued", "running"}:
        job.status, job.stage, job.finished_at = "failed", "更新失败，原版本继续服务", now()
        job.error_code = exc.code if isinstance(exc, Problem) else "PUBLISH_FAILED"
        job.error_message = (
            exc.message if isinstance(exc, Problem) else "发布失败，请查看服务端日志中的任务标识"
        )
        db.commit()
