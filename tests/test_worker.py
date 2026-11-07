import base64
import json
import os
import subprocess

import pytest
from sqlalchemy import select

from courseware import publishing, worker
from courseware.cli import import_local
from courseware.errors import Problem
from courseware.models import Course, Release, SyncJob


def test_local_credentials_are_opt_in_and_explicit_reference_takes_priority(monkeypatch):
    from courseware.config import settings

    snapshot = {"repo_url": "https://github.com/test/demo.git", "branch": "main"}
    calls = []

    def credentials(url):
        calls.append(url)
        return "local-user", "local-token"

    monkeypatch.setattr(worker, "local_git_credentials", credentials)
    monkeypatch.setenv("CW_USE_LOCAL_GIT_CREDENTIALS", "false")
    settings.cache_clear()
    assert worker.git_env(snapshot)["GIT_CONFIG_COUNT"] == "4"
    assert not calls
    monkeypatch.setenv("CW_USE_LOCAL_GIT_CREDENTIALS", "true")
    settings.cache_clear()
    env = worker.git_env(snapshot)
    assert calls == [snapshot["repo_url"]]
    assert env["GIT_CONFIG_VALUE_4"] == "Authorization: Basic " + base64.b64encode(b"local-user:local-token").decode()
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_VALUE_0"] == ""
    monkeypatch.setenv("CW_GIT_PASSWORD_TEST", "explicit-token")
    monkeypatch.setenv("CW_GIT_USERNAME_TEST", "explicit-user")
    env = worker.git_env(snapshot | {"credential_ref": "test"})
    assert env["GIT_CONFIG_VALUE_4"] == "Authorization: Basic " + base64.b64encode(b"explicit-user:explicit-token").decode()
    assert len(calls) == 1
    with pytest.raises(Problem):
        worker.git_env(snapshot | {"repo_url": "https://untrusted.example/test.git"})
    assert len(calls) == 1


def test_local_helper_is_noninteractive_and_anonymous_fetch_remains_available(monkeypatch):
    from courseware.config import settings

    def missing(command, **kwargs):
        assert command[-2:] == ["credential", "fill"]
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["env"]["GIT_ASKPASS"] == ""
        assert kwargs["input"] == "url=https://github.com/test/demo.git\n\n"
        assert kwargs["capture_output"] and kwargs["timeout"] == 15
        return subprocess.CompletedProcess(command, 128, stdout="", stderr="helper failure")

    monkeypatch.setattr(worker.subprocess, "run", missing)
    monkeypatch.setenv("CW_USE_LOCAL_GIT_CREDENTIALS", "true")
    settings.cache_clear()
    env = worker.git_env({"repo_url": "https://github.com/test/demo.git", "branch": "main"})
    assert env["GIT_CONFIG_COUNT"] == "4"


def test_git_worker_publishes_only_the_committed_snapshot(demo, db, user_factory, monkeypatch):
    admin = user_factory()
    subprocess.run(["git", "init", "-b", "main"], cwd=demo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=demo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Contract Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=demo,
        check=True,
        capture_output=True,
    )
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=demo, text=True).strip()
    (demo / "web/uncommitted.html").write_text("uncommitted content must never publish")
    raw = json.loads((demo / "course.json").read_text())
    raw["title"] = "uncommitted title must never publish"
    (demo / "course.json").write_text(json.dumps(raw))
    course = Course(slug="courseware-demo")
    db.add(course)
    db.flush()
    job = SyncJob(
        course_id=course.id,
        requested_by=admin.id,
        source_snapshot={"kind": "git", "repo_url": "https://github.com/test/demo.git", "branch": "main"},
    )
    db.add(job)
    db.commit()
    original_popen = subprocess.Popen

    def local_git(command, *args, **kwargs):
        command = [str(demo) if part == "https://github.com/test/demo.git" else part for part in command]
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(worker.subprocess, "Popen", local_git)
    monkeypatch.setattr(
        worker,
        "git_env",
        lambda snapshot: (
            os.environ
            | {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "protocol.file.allow",
                "GIT_CONFIG_VALUE_0": "always",
            }
        ),
    )
    assert worker.run_once()
    db.refresh(job)
    db.refresh(course)
    assert job.status == "succeeded", job.error_message
    release = db.get(Release, course.current_release_id)
    assert release.commit_sha == sha
    assert release.manifest_json["title"] == "把变化画出来"
    from courseware.config import settings

    package = settings().storage_path / "releases" / course.id / release.id
    assert (package / "web/index.html").is_file()
    assert not (package / "web/uncommitted.html").exists()


def test_import_failure_after_files_written_keeps_previous_metadata(demo, db, user_factory, monkeypatch):
    user_factory()
    first = import_local(demo, "admin", listed=True)
    manifest = json.loads((demo / "course.json").read_text())
    manifest["title"] = "This release must not become current"
    (demo / "course.json").write_text(json.dumps(manifest))

    def fail(*args):
        raise Problem(422, "IMPORT_FAILED", "模拟数据库导入失败")

    monkeypatch.setattr(publishing, "save_metadata", fail)
    with pytest.raises(Problem):
        import_local(demo, "admin")
    course = db.scalar(select(Course))
    assert course.current_release_id == first["release_id"]
    assert len(list(db.scalars(select(Release)))) == 1
    latest = db.scalar(select(SyncJob).order_by(SyncJob.created_at.desc()))
    assert latest.status == "failed" and latest.error_code == "IMPORT_FAILED"


@pytest.mark.parametrize(
    "url,branch",
    [
        ("file:///tmp/repo.git", "main"),
        ("https://github.com@evil.example/course.git", "main"),
        ("https://github.com/course.git?token=secret", "main"),
        ("https://github.com/test/repo.git", "--upload-pack=x"),
        ("http://github.com/test/repo.git", "main"),
        ("https://github.com:bad/test/repo.git", "main"),
    ],
)
def test_source_boundaries(url, branch):
    with pytest.raises(Problem):
        worker.validate_source(url, branch)
