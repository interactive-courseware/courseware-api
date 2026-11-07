from collections import Counter
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import Text, and_, case, cast, exists, func, or_, select

from .auth import DB, Member
from .config import settings
from .contract import PATH
from .dto import CourseCard, CourseDetail, LessonView, Page
from .errors import Problem
from .models import Course, CourseGrant, CourseMetadata, Creator, Lesson, Release, ReleaseFile, User, now

router = APIRouter(tags=["Catalog"])


def permitted(user: User):
    if user.role == "admin":
        return True
    return and_(
        Course.listing_status == "listed",
        or_(
            Course.access_policy == "authenticated",
            exists(
                select(CourseGrant.course_id).where(
                    CourseGrant.course_id == Course.id, CourseGrant.user_id == user.id
                )
            ),
        ),
    )


def visible_query(user):
    return (
        select(Course, Release, CourseMetadata)
        .join(Release, Course.current_release_id == Release.id)
        .join(CourseMetadata, CourseMetadata.release_id == Release.id)
        .where(
            permitted(user),
            Course.listing_status == "listed",
            Release.revoked_at.is_(None),
            Release.status == "ready",
        )
    )


def find_course(db, slug, user):
    course = db.scalar(select(Course).where(Course.slug == slug, permitted(user)))
    if not course:
        raise Problem(404, "COURSE_NOT_FOUND", "课程不存在或无权访问")
    return course


def find_release(db, course, user, release_id=None):
    release = (
        db.get(Release, release_id or course.current_release_id)
        if (release_id or course.current_release_id)
        else None
    )
    if (
        not release
        or release.course_id != course.id
        or release.status != "ready"
        or release.revoked_at
        or (
            user.role != "admin"
            and release.id != course.current_release_id
            and (not release.serving_until or release.serving_until <= now())
        )
    ):
        raise Problem(404, "RELEASE_NOT_FOUND", "课程版本不可访问")
    return release


def file_url(course, release, path):
    return f"/courses/{course.slug}/releases/{release.id}/{path}"


def counts(data):
    published = [item for item in data["lessons"] if item["status"] == "published"]
    return {
        "chapter_count": sum(not c.get("parent_id") for c in data["chapters"]),
        "section_count": len(data["chapters"]),
        "group_count": len(data["groups"]),
        "lesson_count": len(published),
        "planned_lesson_count": len(data["lessons"]) - len(published),
        "resource_count": len(data["resources"]),
        "lesson_counts_by_kind": {
            kind: sum(lesson["kind"] == kind for lesson in published)
            for kind in ("theory", "lab", "project", "reading")
        },
        "known_duration_minutes": sum(lesson.get("duration_minutes", 0) for lesson in published),
        "lessons_without_duration": sum("duration_minutes" not in lesson for lesson in published),
    }


def image_dto(course, release, data):
    return {key: value for key, value in data.items() if key in {"id", "alt", "width", "height"}} | {
        "url": file_url(course, release, data["path"])
    }


def lesson_dto(course, release, lesson):
    keys = {
        "id",
        "title",
        "summary",
        "kind",
        "status",
        "group_id",
        "chapter_ids",
        "creator_ids",
        "keywords",
        "duration_minutes",
        "cover_image_id",
    }
    published = lesson["status"] == "published"
    return {key: value for key, value in lesson.items() if key in keys} | {
        "url": file_url(course, release, lesson["entry"]) if published else None,
        "launch_url": f"/courses/{course.slug}/lessons/{lesson['id']}/" if published else None,
    }


def card(course, release):
    data = release.manifest_json
    creators = []
    for creator in data["creators"]:
        output = {
            key: creator[key]
            for key in ("id", "name", "roles", "organization", "team", "bio")
            if key in creator
        }
        if creator.get("avatar"):
            output["avatar"] = image_dto(course, release, creator["avatar"])
        creators.append(output)
    return {
        "id": course.id,
        "slug": course.slug,
        "release_id": release.id,
        "title": data["title"],
        "summary": data["summary"],
        "language": data["language"],
        "level": data.get("level"),
        "tags": data["tags"],
        "featured": course.featured,
        "published_at": course.published_at.isoformat() if course.published_at else None,
        "cover": image_dto(course, release, next(i for i in data["images"] if i["role"] == "cover")),
        "creators": creators,
        "counts": counts(data),
        "detail_url": f"/catalog/{course.slug}",
        "launch_url": f"/courses/{course.slug}/",
    }


def search_filter(query, q):
    return query.where(CourseMetadata.search_text.icontains(q, autoescape=True)) if q else query


def page_result(db, query, page, page_size):
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    return db.execute(query.offset((page - 1) * page_size).limit(page_size)).all(), total


@router.get("/api/v1/courses", response_model=Page[CourseCard])
def courses(
    db: DB,
    user: Member,
    q: str = Query("", max_length=100),
    tag: str | None = None,
    creator_id: str | None = None,
    kind: Literal["theory", "lab", "project", "reading"] | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort: Literal["relevance", "published", "title"] = "relevance",
):
    q = q.strip()
    query = search_filter(visible_query(user), q)
    if tag:
        query = query.where(CourseMetadata.tags.contains([tag]))
    if creator_id:
        query = query.where(
            exists(select(Creator.id).where(Creator.release_id == Release.id, Creator.id == creator_id))
        )
    if kind:
        query = query.where(
            exists(
                select(Lesson.id).where(
                    Lesson.release_id == Release.id, Lesson.status == "published", Lesson.kind == kind
                )
            )
        )
    if sort == "title":
        query = query.order_by(CourseMetadata.title)
    elif sort == "published":
        query = query.order_by(Course.published_at.desc())
    else:
        if q:
            query = query.order_by(
                case(
                    (CourseMetadata.title == q, 0),
                    (CourseMetadata.title.icontains(q, autoescape=True), 1),
                    else_=2,
                )
            )
        query = query.order_by(Course.featured.desc(), Course.display_order, Course.published_at.desc())
    rows, total = page_result(db, query.order_by(Course.id), page, page_size)
    return {"items": [card(c, r) for c, r, _ in rows], "page": page, "page_size": page_size, "total": total}


@router.get("/api/v1/courses/{slug}", response_model=CourseDetail)
def detail(slug: str, db: DB, user: Member):
    course = find_course(db, slug, user)
    release = find_release(db, course, user)
    data = release.manifest_json
    return card(course, release) | {
        "description": data["description"],
        "gallery": [image_dto(course, release, i) for i in data["images"] if i["role"] == "gallery"],
        "groups": data["groups"],
        "chapters": data["chapters"],
        "lessons": [lesson_dto(course, release, lesson) for lesson in data["lessons"]],
        "resources": [
            {k: r[k] for k in ("id", "title", "kind", "lesson_ids")}
            | {"url": file_url(course, release, r["path"])}
            for r in data["resources"]
        ],
    }


@router.get("/api/v1/courses/{slug}/lessons", response_model=Page[LessonView])
def lessons(
    slug: str,
    db: DB,
    user: Member,
    group_id: str | None = None,
    chapter_id: str | None = None,
    kind: str | None = None,
    status: Literal["published", "planned"] | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    course = find_course(db, slug, user)
    release = find_release(db, course, user)
    selected = [
        lesson
        for lesson in release.manifest_json["lessons"]
        if (not group_id or lesson["group_id"] == group_id)
        and (not chapter_id or chapter_id in lesson["chapter_ids"])
        and (not kind or lesson["kind"] == kind)
        and (not status or lesson["status"] == status)
    ]
    return {
        "items": [
            lesson_dto(course, release, lesson)
            for lesson in selected[(page - 1) * page_size : page * page_size]
        ],
        "page": page,
        "page_size": page_size,
        "total": len(selected),
    }


@router.get("/api/v1/catalog/stats")
def stats(db: DB, user: Member):
    rows = db.execute(visible_query(user)).all()
    return {
        "course_count": len(rows),
        "lesson_count": sum(counts(r.manifest_json)["lesson_count"] for _, r, _ in rows),
        "creator_count": len({c["id"] for _, r, _ in rows for c in r.manifest_json["creators"]}),
    }


@router.get("/api/v1/catalog/facets")
def facets(db: DB, user: Member):
    tags, creators, kinds = Counter(), {}, Counter()
    for _, release, _ in db.execute(visible_query(user)):
        data = release.manifest_json
        tags.update(data["tags"])
        kinds.update({lesson["kind"] for lesson in data["lessons"] if lesson["status"] == "published"})
        for c in data["creators"]:
            item = creators.setdefault(c["id"], {"id": c["id"], "name": c["name"], "count": 0})
            item["count"] += 1
    return {
        "tags": [{"name": k, "count": v} for k, v in sorted(tags.items())],
        "creators": sorted(creators.values(), key=lambda c: c["name"]),
        "kinds": [{"name": k, "count": v} for k, v in sorted(kinds.items())],
    }


@router.get("/api/v1/search")
def search(
    db: DB,
    user: Member,
    q: str = Query(..., min_length=1, max_length=100),
    type: Literal["course", "lesson"] = "course",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    tag: str | None = None,
    creator_id: str | None = None,
    kind: Literal["theory", "lab", "project", "reading"] | None = None,
):
    if type == "course":
        return courses(db, user, q, tag, creator_id, kind, page, page_size, "relevance")
    query = (
        visible_query(user)
        .add_columns(Lesson)
        .join(Lesson, Lesson.release_id == Release.id)
        .where(
            Lesson.status == "published",
            or_(
                Lesson.title.icontains(q, autoescape=True),
                Lesson.summary.icontains(q, autoescape=True),
                cast(Lesson.keywords, Text).icontains(q, autoescape=True),
            ),
        )
    )
    if tag:
        query = query.where(CourseMetadata.tags.contains([tag]))
    if kind:
        query = query.where(Lesson.kind == kind)
    if creator_id:
        query = query.where(
            exists(select(Creator.id).where(Creator.release_id == Release.id, Creator.id == creator_id))
        )
    rows, total = page_result(db, query.order_by(Course.id, Lesson.position), page, page_size)
    return {
        "items": [
            {
                "course_slug": c.slug,
                "course_title": m.title,
                "release_id": r.id,
                "id": lesson.id,
                "title": lesson.title,
                "summary": lesson.summary,
                "kind": lesson.kind,
                "url": file_url(c, r, lesson.entry),
            }
            for c, r, m, lesson in rows
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@router.api_route(
    "/courses/{slug}/releases/{release_id}/{path:path}", methods=["GET", "HEAD"], include_in_schema=False
)
def protected_file(slug: str, release_id: UUID, path: str, request: Request, db: DB, user: Member):
    course = find_course(db, slug, user)
    release = find_release(db, course, user, str(release_id))
    record = db.get(ReleaseFile, (release.id, path)) if PATH.fullmatch(path) else None
    if not record:
        raise Problem(404, "FILE_NOT_FOUND", "文件不存在")
    directory = settings().storage_path / "releases" / course.id / release.id
    target = directory / path
    if (
        not target.is_file()
        or target.is_symlink()
        or not target.resolve().is_relative_to(directory.resolve())
    ):
        raise Problem(404, "FILE_NOT_FOUND", "文件不存在")
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    attachment = record.media_type == "application/octet-stream"
    if settings().x_accel:
        headers["X-Accel-Redirect"] = f"/_protected/{course.id}/{release.id}/{path}"
        if attachment:
            headers["Content-Disposition"] = f'attachment; filename="{Path(path).name}"'
        return Response(headers=headers, media_type=record.media_type)
    return FileResponse(
        target, headers=headers, media_type=record.media_type, filename=target.name if attachment else None
    )


@router.get("/courses/{slug}/lessons/{lesson_id}/")
def launch_lesson(slug: str, lesson_id: str, db: DB, user: Member):
    course = find_course(db, slug, user)
    release = find_release(db, course, user)
    lesson = db.get(Lesson, (release.id, lesson_id))
    if not lesson or lesson.status != "published":
        raise Problem(404, "LESSON_NOT_FOUND", "课时尚未发布或不存在")
    return RedirectResponse(file_url(course, release, lesson.entry), status_code=302)


@router.get("/courses/{slug}/")
def launch(slug: str, db: DB, user: Member):
    course = find_course(db, slug, user)
    release = find_release(db, course, user)
    return RedirectResponse(file_url(course, release, release.manifest_json["entry"]), status_code=302)


@router.get("/courses/{slug}/{path:path}")
def compatibility(slug: str, path: str, db: DB, user: Member):
    course = find_course(db, slug, user)
    release = find_release(db, course, user)
    if not PATH.fullmatch(path) or not db.get(ReleaseFile, (release.id, path)):
        raise Problem(404, "FILE_NOT_FOUND", "文件不存在")
    return RedirectResponse(file_url(course, release, path), status_code=302)
