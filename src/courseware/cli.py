import argparse
import getpass
import hashlib
import json
import os
from importlib.metadata import version
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import hasher
from .contract import load_manifest, validate_package
from .db import engine
from .errors import Problem
from .models import Course, SyncJob, User, now, uid
from .publishing import fail_job, publish_directory, require_idle


def import_local(root, username, listed=False):
    """Explicit operator-only preview: snapshots working-tree content, never exposed through HTTP."""
    root = Path(root).resolve()
    raw = (root / "course.json").read_bytes()
    data = load_manifest(raw, json.loads(raw)["slug"])
    files = validate_package(root, data)
    snapshot_hash = hashlib.sha256(raw + json.dumps(files, sort_keys=True).encode()).hexdigest()
    owner = uid()
    with Session(engine(), expire_on_commit=False) as db:
        user = db.scalar(
            select(User).where(User.username == username, User.role == "admin", User.status == "active")
        )
        if not user:
            raise Problem(422, "ADMIN_NOT_FOUND", "请指定启用的管理员用户名")
        course = db.scalar(select(Course).where(Course.slug == data["slug"]).with_for_update())
        if not course:
            course = Course(slug=data["slug"])
            db.add(course)
            db.flush()
        require_idle(db, course.id)
        job = SyncJob(
            course_id=course.id,
            requested_by=user.id,
            trigger="cli",
            status="running",
            source_snapshot={"kind": "local-preview", "manifest_path": "course.json"},
            lease_owner=owner,
            heartbeat_at=now(),
            started_at=now(),
            stage="校验本地预览",
        )
        db.add(job)
        db.commit()
        try:
            release = publish_directory(db, job.id, root, snapshot_hash, owner)
            if listed:
                course.listing_status = "listed"
                db.commit()
            return {
                "course": course.slug,
                "release_id": release.id,
                "job_id": job.id,
                "source": "local-preview",
            }
        except Exception as exc:
            fail_job(db, job.id, exc)
            raise


def main():
    parser = argparse.ArgumentParser(prog="courseware-api")
    parser.add_argument("--version", action="version", version=version("courseware-api"))
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-admin")
    create.add_argument("username")
    create.add_argument("--display-name", default="平台管理员")
    create.add_argument("--if-not-exists", action="store_true")
    imp = sub.add_parser("import-local", help="显式导入受信任本地工作树作为预览；生产使用 Git 同步")
    imp.add_argument("path")
    imp.add_argument("--username", required=True)
    imp.add_argument("--list", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("path")
    sub.add_parser("worker")
    sub.add_parser("worker-once")
    export = sub.add_parser("export-openapi")
    export.add_argument("path")
    initialize = sub.add_parser("init", help="生成独立配置；配置数据库后再次执行迁移")
    initialize.add_argument("--config", default=argparse.SUPPRESS)
    service = sub.add_parser("server")
    actions = service.add_subparsers(dest="action", required=True)
    for name in ("start", "run", "stop", "restart", "status"):
        action = actions.add_parser(name)
        action.add_argument("--config", default=argparse.SUPPRESS)
        action.add_argument("--host", default="127.0.0.1")
        action.add_argument("--port", type=int, default=8008)
    args = parser.parse_args()
    from . import service

    path = service.config_path(args.config)
    service.load_config(path)
    try:
        if args.command == "init":
            service.initialize(path)
        elif args.command == "server":
            service.server(args, path)
        elif args.command == "create-admin":
            import re

            if not re.fullmatch(r"[a-z][a-z0-9._-]{2,79}", args.username):
                parser.error("用户名需为 3–80 位小写 ASCII 标识")
            with Session(engine()) as db:
                if db.scalar(select(User).where(User.username == args.username)):
                    if args.if_not_exists:
                        print("管理员账号已存在，密码保持不变")
                        return
                    parser.error("用户名已存在")
                password = os.environ.get("CW_ADMIN_PASSWORD") or getpass.getpass(
                    "管理员密码（至少 12 字符）："
                )
                if not 12 <= len(password) <= 200:
                    parser.error("密码长度需为 12–200 字符")
                db.add(
                    User(
                        username=args.username,
                        display_name=args.display_name,
                        password_hash=hasher.hash(password),
                        role="admin",
                        must_change_password=False,
                    )
                )
                db.commit()
                print("管理员已创建")
        elif args.command == "import-local":
            print(json.dumps(import_local(args.path, args.username, args.list), ensure_ascii=False))
        elif args.command == "validate":
            root = Path(args.path).resolve()
            raw = (root / "course.json").read_bytes()
            data = load_manifest(raw, json.loads(raw)["slug"])
            files = validate_package(root, data)
            print(f"校验通过：{data['title']}，{len(files)} 个发布文件")
        elif args.command == "export-openapi":
            from .main import app

            Path(args.path).write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n")
        else:
            from .worker import main as worker_main
            from .worker import run_once

            run_once() if args.command == "worker-once" else worker_main()
    except Problem as exc:
        parser.exit(1, f"{exc.code}: {exc.message}\n")
    except RuntimeError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
