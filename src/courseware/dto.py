from typing import Generic, Literal, TypeVar

from pydantic import BaseModel


class UserView(BaseModel):
    id: str
    username: str
    display_name: str
    role: Literal["admin", "member"]
    status: Literal["active", "disabled"]
    must_change_password: bool


class AuthView(BaseModel):
    user: UserView
    csrf_token: str


class ImageView(BaseModel):
    id: str | None = None
    url: str
    alt: str
    width: int | None = None
    height: int | None = None


class CreatorView(BaseModel):
    id: str
    name: str
    roles: list[str]
    organization: str | None = None
    team: str | None = None
    bio: str | None = None
    avatar: ImageView | None = None


class Counts(BaseModel):
    chapter_count: int
    section_count: int
    group_count: int
    lesson_count: int
    planned_lesson_count: int
    resource_count: int
    lesson_counts_by_kind: dict[str, int]
    known_duration_minutes: int
    lessons_without_duration: int


class CourseCard(BaseModel):
    id: str
    slug: str
    release_id: str
    title: str
    summary: str
    language: str
    level: str | None
    tags: list[str]
    published_at: str | None
    featured: bool
    cover: ImageView
    creators: list[CreatorView]
    counts: Counts
    detail_url: str
    launch_url: str


class GroupView(BaseModel):
    id: str
    title: str
    summary: str | None = None


class ChapterView(GroupView):
    parent_id: str | None = None


class LessonView(BaseModel):
    id: str
    title: str
    summary: str
    kind: str
    status: str
    group_id: str
    chapter_ids: list[str]
    creator_ids: list[str]
    keywords: list[str]
    duration_minutes: int | None = None
    cover_image_id: str | None = None
    url: str | None
    launch_url: str | None


class ResourceView(BaseModel):
    id: str
    title: str
    kind: str
    lesson_ids: list[str]
    url: str


class CourseDetail(CourseCard):
    description: str
    gallery: list[ImageView]
    groups: list[GroupView]
    chapters: list[ChapterView]
    lessons: list[LessonView]
    resources: list[ResourceView]


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int
