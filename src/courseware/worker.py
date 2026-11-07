import base64
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import settings
from .contract import included, load_manifest, safe_path
from .db import engine
from .errors import Problem
from .models import Course, SyncJob, now, uid
from .publishing import fail_job, publish_directory


def validate_source(url, branch, credential_ref=None):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise Problem(422, "SOURCE_NOT_ALLOWED", "Git 仓库地址格式无效") from None
    allowed = {host.strip() for host in settings().allowed_git_hosts.split(",")}
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or not parsed.path.endswith(".git")
        or parsed.query
        or parsed.fragment
        or any(c in url for c in "\r\n\x00")
    ):
        raise Problem(422, "SOURCE_NOT_ALLOWED", "仅允许部署配置中的 HTTPS Git 仓库地址，且不能携带凭据")
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch)
        or any(token in branch for token in ("..", "//", "@{", ".lock"))
        or branch.endswith(("/", "."))
    ):
        raise Problem(422, "BRANCH_INVALID", "分支名无效")
    if credential_ref and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", credential_ref):
        raise Problem(422, "CREDENTIAL_REF_INVALID", "凭据引用格式无效")


def local_git_credentials(url):
    # Only ask the trusted machine's helpers for credentials; fetch stays isolated.
    env = dict(os.environ)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", SSH_ASKPASS="", GCM_INTERACTIVE="never")
    try:
        result = subprocess.run(
            ["git", "-c", "core.askPass=", "-c", "credential.interactive=false", "credential", "fill"],
            input=f"url={url}\n\n", text=True, capture_output=True, env=env, timeout=15,
        )
    except subprocess.TimeoutExpired:
        raise Problem(422, "GIT_CREDENTIAL_TIMEOUT", "读取本机 Git 凭据超时") from None
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode or not values.get("username") or not values.get("password"):
        return None, None  # Public repositories can still be fetched anonymously.
    return values["username"], values["password"]


def git_env(snapshot):
    env = dict(os.environ)
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_ASKPASS="",
        SSH_ASKPASS="",
        GIT_CONFIG_COUNT="3",
        GIT_CONFIG_KEY_0="credential.helper",
        GIT_CONFIG_VALUE_0="",
        GIT_CONFIG_KEY_1="http.followRedirects",
        GIT_CONFIG_VALUE_1="false",
        GIT_CONFIG_KEY_2="protocol.allow",
        GIT_CONFIG_VALUE_2="never",
    )
    env.update(GIT_CONFIG_COUNT="4", GIT_CONFIG_KEY_3="protocol.https.allow", GIT_CONFIG_VALUE_3="always")
    reference = snapshot.get("credential_ref")
    password = None
    if reference:
        key = reference.upper().replace("-", "_")
        password = os.environ.get("CW_GIT_PASSWORD_" + key)
        username = os.environ.get("CW_GIT_USERNAME_" + key, "oauth2")
        if not password:
            raise Problem(422, "GIT_CREDENTIAL_MISSING", "Worker 未配置对应的只读 Git 凭据")
    elif settings().use_local_git_credentials:
        validate_source(snapshot["repo_url"], snapshot.get("branch", "main"))
        username, password = local_git_credentials(snapshot["repo_url"])
    if password:
        auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        host = urlsplit(snapshot["repo_url"]).hostname
        env.update(
            GIT_CONFIG_COUNT="5",
            GIT_CONFIG_KEY_4=f"http.https://{host}/.extraHeader",
            GIT_CONFIG_VALUE_4="Authorization: Basic " + auth,
        )
    return env


def heartbeat(job_id, owner):
    with Session(engine()) as db:
        result = db.execute(
            update(SyncJob)
            .where(SyncJob.id == job_id, SyncJob.status == "running", SyncJob.lease_owner == owner)
            .values(heartbeat_at=now())
        )
        db.commit()
        if result.rowcount != 1:
            raise Problem(409, "JOB_LEASE_LOST", "任务已失效")


def checkout(snapshot, slug, root, job_id, owner):
    validate_source(snapshot["repo_url"], snapshot["branch"], snapshot.get("credential_ref"))
    env = git_env(snapshot)
    git = root / "git"
    package = root / "package"
    package.mkdir()
    subprocess.run(["git", "init", "--bare", str(git)], env=env, check=True, capture_output=True)
    command = [
        "git",
        "-C",
        str(git),
        "fetch",
        "--depth=1",
        "--no-tags",
        "--",
        snapshot["repo_url"],
        "refs/heads/" + snapshot["branch"],
    ]
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(command, env=env, stdout=log, stderr=log)
        start = last_heartbeat = time.monotonic()
        try:
            while process.poll() is None:
                if time.monotonic() - start > 120:
                    raise Problem(422, "GIT_TIMEOUT", "拉取仓库超时")
                if sum(p.stat().st_size for p in git.rglob("*") if p.is_file()) > settings().max_git_bytes:
                    raise Problem(422, "GIT_TOO_LARGE", "仓库下载超过大小限制")
                if time.monotonic() - last_heartbeat > 10:
                    heartbeat(job_id, owner)
                    last_heartbeat = time.monotonic()
                time.sleep(0.25)
            if process.returncode:
                raise Problem(422, "GIT_FETCH_FAILED", "仓库拉取失败，请检查地址、分支、网络或只读凭据")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def git_output(*args):
        return subprocess.check_output(
            ["git", "-C", str(git), *args], env=env, stderr=subprocess.DEVNULL, timeout=30
        )

    commit = git_output("rev-parse", "FETCH_HEAD").decode().strip()
    entries = {}
    raw_tree = git_output("ls-tree", "-r", "-z", "-l", commit)
    if len(raw_tree) > 8 * 1024 * 1024:
        raise Problem(422, "GIT_TOO_LARGE", "仓库文件清单过大")
    for entry in raw_tree.split(b"\0"):
        if not entry:
            continue
        meta, name = entry.split(b"\t", 1)
        mode, kind, sha, size = meta.decode().split()
        entries[name.decode("utf-8")] = (mode, kind, sha, int(size) if size != "-" else 0)
    manifest_entry = entries.get("course.json")
    if not manifest_entry or manifest_entry[0] not in {"100644", "100755"} or manifest_entry[3] > 1024 * 1024:
        raise Problem(422, "MANIFEST_MISSING", "仓库缺少有效的 course.json")
    raw = git_output("cat-file", "blob", manifest_entry[2])
    data = load_manifest(raw, slug)
    (package / "course.json").write_bytes(raw)
    total = count = 0
    for path, (mode, kind, sha, size) in entries.items():
        if not included(path, data["publish"]["include"]):
            continue
        safe_path(path)
        total, count = total + size, count + 1
        if mode not in {"100644", "100755"} or kind != "blob":
            raise Problem(422, "GIT_FILE_INVALID", "不允许发布符号链接或子模块")
        if (
            size > settings().max_file_bytes
            or total > settings().max_package_bytes
            or count > settings().max_files
        ):
            raise Problem(422, "PACKAGE_TOO_LARGE", "发布文件超过大小或数量限制")
        target = package / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git_output("cat-file", "blob", sha))
    heartbeat(job_id, owner)
    return package, commit


def run_once():
    owner = uid()
    with Session(engine(), expire_on_commit=False) as db:
        db.execute(
            update(SyncJob)
            .where(SyncJob.status == "running", SyncJob.heartbeat_at < now() - timedelta(minutes=5))
            .values(
                status="failed",
                stage="Worker 中断",
                error_code="WORKER_INTERRUPTED",
                error_message="更新进程中断，请重新同步；原版本继续服务",
                finished_at=now(),
            )
        )
        job = db.scalar(
            select(SyncJob)
            .where(SyncJob.status == "queued")
            .order_by(SyncJob.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not job:
            db.commit()
            return False
        job.status, job.stage, job.lease_owner = "running", "拉取并校验课程", owner
        job.started_at = job.heartbeat_at = now()
        job_id, snapshot = job.id, job.source_snapshot
        course = db.get(Course, job.course_id)
        slug = course.slug
        db.commit()
    storage = settings().storage_path
    (storage / "work").mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="fetch-", dir=storage / "work"))
    try:
        package, commit = checkout(snapshot, slug, root, job_id, owner)
        with Session(engine(), expire_on_commit=False) as db:
            try:
                publish_directory(db, job_id, package, commit, owner)
            except Exception as exc:
                fail_job(db, job_id, exc)
    except Exception as exc:
        with Session(engine()) as db:
            fail_job(db, job_id, exc)
    finally:
        shutil.rmtree(root)
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        while True:
            if not run_once():
                time.sleep(2)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
