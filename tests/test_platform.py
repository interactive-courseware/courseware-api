import json
from datetime import timedelta

import pytest
from conftest import PASSWORD, login
from fastapi.testclient import TestClient
from sqlalchemy import select

from courseware.cli import import_local
from courseware.config import settings
from courseware.contract import load_manifest, validate_package
from courseware.errors import Problem
from courseware.main import app
from courseware.models import Course, CourseGrant, Release, SessionToken, SyncJob, now


def test_sessions_csrf_logout_and_independent_users(client, user_factory, db):
    user_factory()
    user_factory("learner", "member")
    assert client.get("/api/v1/courses").status_code == 401
    assert (
        client.post("/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 403
    )
    response = login(client)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    with TestClient(app, base_url=settings().public_origin) as learner:
        login(learner, "learner")
        assert learner.get("/api/v1/auth/me").json()["user"]["username"] == "learner"
        assert learner.get("/api/v1/admin/users").status_code == 403
        assert client.get("/api/v1/auth/me").json()["user"]["username"] == "admin"
        assert client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "bad"}).status_code == 403
        assert client.post("/api/v1/auth/logout").status_code == 204
        assert client.get("/api/v1/auth/me").status_code == 401
        assert learner.get("/api/v1/auth/me").status_code == 200
    assert all(len(s.token_hash) == 64 for s in db.scalars(select(SessionToken)))


def test_catalog_import_counts_search_files_and_restriction(client, user_factory, db, demo):
    admin = user_factory()
    learner = user_factory("learner", "member")
    imported = import_local(demo, "admin", listed=True)
    login(client)
    course = client.get("/api/v1/courses/courseware-demo").json()
    assert course["counts"]["lesson_count"] == 2
    assert course["counts"]["chapter_count"] == 2
    assert course["counts"]["planned_lesson_count"] == 1
    assert "publish" not in course and "repo_url" not in course
    assert course["lessons"][2]["url"] is None
    assert client.get("/api/v1/courses", params={"q": "斜率"}).json()["total"] == 1
    assert client.get("/api/v1/courses", params={"q": "%"}).json()["total"] == 0
    assert client.get("/api/v1/search", params={"q": "参数", "type": "lesson"}).json()["total"] == 2
    image_url = course["cover"]["url"]
    assert client.get(image_url).headers["content-type"] == "image/png"
    assert client.head(image_url).status_code == 200
    assert client.get(image_url, headers={"Range": "bytes=0-9"}).status_code == 206
    assert client.get(course["launch_url"], follow_redirects=False).status_code == 302
    with TestClient(app, base_url=settings().public_origin) as other:
        assert other.get(image_url).status_code == 401
        login(other, "learner")
        assert other.get(image_url).status_code == 200
        record = db.scalar(select(Course))
        record.access_policy = "restricted"
        db.commit()
        assert other.get("/api/v1/courses").json()["total"] == 0
        assert other.get("/api/v1/catalog/stats").json()["course_count"] == 0
        assert other.get(image_url).status_code == 404
        db.add(CourseGrant(course_id=record.id, user_id=learner.id, granted_by=admin.id))
        db.commit()
        assert other.get(image_url).status_code == 200
        assert (
            other.get(image_url.replace("assets/cover.png", "web/%252e%252e/course.json")).status_code == 404
        )
    assert imported["release_id"] == course["release_id"]


def test_publication_failure_and_atomic_rollback(client, user_factory, db, demo):
    user_factory()
    first = import_local(demo, "admin", listed=True)
    path = demo / "course.json"
    data = json.loads(path.read_text())
    data["title"] = "第二个版本"
    path.write_text(json.dumps(data, ensure_ascii=False))
    second = import_local(demo, "admin", listed=True)
    assert second["release_id"] != first["release_id"]
    login(client)
    assert client.get("/api/v1/courses/courseware-demo").json()["title"] == "第二个版本"
    data["lessons"][0]["chapter_ids"] = ["missing"]
    path.write_text(json.dumps(data))
    with pytest.raises(Problem):
        import_local(demo, "admin")
    assert client.get("/api/v1/courses/courseware-demo").json()["release_id"] == second["release_id"]
    cid = db.scalar(select(Course.id))
    response = client.post(
        f"/api/v1/admin/courses/{cid}/activate",
        json={
            "release_id": first["release_id"],
            "expected_current_release_id": second["release_id"],
            "reason": "验收回退",
        },
    )
    assert response.status_code == 200, response.text
    detail = client.get("/api/v1/courses/courseware-demo").json()
    assert detail["title"] == "把变化画出来" and detail["release_id"] == first["release_id"]
    assert client.get(detail["cover"]["url"]).status_code == 200
    assert (
        client.post(
            f"/api/v1/admin/courses/{cid}/activate",
            json={
                "release_id": second["release_id"],
                "expected_current_release_id": second["release_id"],
                "reason": "过期操作",
            },
        ).status_code
        == 409
    )


def test_admin_account_changes_and_last_admin(client, user_factory):
    admin = user_factory()
    learner = user_factory("learner", "member")
    login(client)
    assert client.patch(f"/api/v1/admin/users/{admin.id}", json={"status": "disabled"}).status_code == 409
    with TestClient(app, base_url=settings().public_origin) as other:
        login(other, "learner")
        assert (
            client.patch(f"/api/v1/admin/users/{learner.id}", json={"status": "disabled"}).status_code == 200
        )
        assert other.get("/api/v1/auth/me").status_code == 401
    assert (
        client.post(
            "/api/v1/admin/users",
            json={"username": "newuser", "display_name": "新同学", "password": PASSWORD, "role": "member"},
        ).status_code
        == 201
    )
    with TestClient(app, base_url=settings().public_origin) as new:
        login(new, "newuser")
        assert new.get("/api/v1/courses").json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"
        assert (
            new.put(
                "/api/v1/auth/password",
                json={"current_password": PASSWORD, "new_password": PASSWORD + "-changed"},
            ).status_code
            == 204
        )
        assert new.get("/api/v1/auth/me").status_code == 401
        login(new, "newuser", PASSWORD + "-changed")
        assert new.get("/api/v1/courses").status_code == 200


def test_sync_idempotency_grants_version_and_stale_worker(client, user_factory, db):
    user_factory()
    learner = user_factory("learner", "member")
    login(client)
    result = client.post(
        "/api/v1/admin/courses",
        json={"slug": "new-course", "repo_url": "https://github.com/example/course.git", "branch": "main"},
    )
    assert result.status_code == 201, result.text
    cid = result.json()["id"]
    grant = {"user_ids": [learner.id], "expected_version": 0}
    assert client.put(f"/api/v1/admin/courses/{cid}/grants", json=grant).status_code == 200
    assert client.put(f"/api/v1/admin/courses/{cid}/grants", json=grant).status_code == 409
    job = client.post(f"/api/v1/admin/courses/{cid}/sync", headers={"Idempotency-Key": "once"})
    assert job.status_code == 202
    repeated = client.post(f"/api/v1/admin/courses/{cid}/sync", headers={"Idempotency-Key": "once"})
    assert repeated.json()["id"] == job.json()["id"]
    assert client.post(f"/api/v1/admin/courses/{cid}/sync").status_code == 409
    record = db.get(SyncJob, job.json()["id"])
    record.status = "running"
    record.heartbeat_at = now() - timedelta(minutes=10)
    db.commit()
    from courseware.worker import run_once

    assert run_once() is False
    db.refresh(record)
    assert record.status == "failed" and record.error_code == "WORKER_INTERRUPTED"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(entry="/web/index.html"),
        lambda d: d["lessons"][0].update(chapter_ids=["unknown"]),
        lambda d: d["chapters"][0].update(parent_id=d["chapters"][0]["id"]),
        lambda d: d["lessons"][2].update(entry="web/index.html"),
        lambda d: d["publish"].update(include=["lessons/"]),
        lambda d: d["creators"][0].update(roles=["instructor"]),
    ],
)
def test_manifest_rejects_invalid_contract(demo, mutation):
    data = json.loads((demo / "course.json").read_text())
    mutation(data)
    with pytest.raises(Problem):
        load_manifest(json.dumps(data).encode(), "courseware-demo")


def test_symlinks_and_absolute_resources_rejected(demo):
    raw = (demo / "course.json").read_bytes()
    data = load_manifest(raw, "courseware-demo")
    (demo / "web/leak.txt").symlink_to(demo / "course.json")
    with pytest.raises(Problem):
        validate_package(demo, data)
    (demo / "web/leak.txt").unlink()
    with (demo / "web/index.html").open("a") as file:
        file.write('<img src="/private.png">')
    with pytest.raises(Problem):
        validate_package(demo, data)


def test_expired_session_and_withdrawn_release(client, user_factory, db, demo):
    user_factory()
    import_local(demo, "admin", listed=True)
    login(client)
    detail = client.get("/api/v1/courses/courseware-demo").json()
    release = db.get(Release, detail["release_id"])
    release.revoked_at = now()
    db.commit()
    assert client.get(detail["cover"]["url"]).status_code == 404
    assert client.get("/api/v1/courses").json()["total"] == 0
    session = db.scalar(select(SessionToken))
    session.last_seen_at = now() - timedelta(hours=25)
    db.commit()
    assert client.get("/api/v1/auth/me").status_code == 401
