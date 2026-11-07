"""Installed-package service lifecycle; API and importer share one supervisor."""
import fcntl
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path
from urllib.request import urlopen

import psutil


def config_path(value=None):
    return Path(value or os.environ.get("CW_CONFIG_FILE", Path.home() / ".farfarfun/courseware/backend/config.json")).expanduser().resolve()


def load_config(path):
    if path.is_file():
        data = json.loads(path.read_text())
        for key, value in data.items():
            if key.startswith("CW_"):
                os.environ.setdefault(key, str(value))
    os.environ["CW_CONFIG_FILE"] = str(path)


def initialize(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        with path.open("x") as handle:
            os.chmod(path, 0o600)
            json.dump({
                "CW_DATABASE_URL": "postgresql+psycopg://courseware@127.0.0.1:5432/courseware",
                "CW_SECRET_KEY": secrets.token_urlsafe(48),
                "CW_PUBLIC_ORIGIN": "http://127.0.0.1:8817",
                "CW_STORAGE_PATH": str(path.parent / "storage"),
                "CW_SECURE_COOKIES": "false",
            }, handle, indent=2)
        print(f"已生成 {path}；请配置 PostgreSQL 连接后再次执行 init。")
        return
    load_config(path)
    from alembic import command
    from alembic.config import Config

    from .config import settings

    location = Path(__file__).parent / "migrations"
    if not location.is_dir():
        location = Path(__file__).resolve().parents[2] / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(location))
    (settings().storage_path / "releases").mkdir(parents=True, exist_ok=True)
    (settings().storage_path / "work").mkdir(parents=True, exist_ok=True)
    command.upgrade(cfg, "head")
    print("数据库迁移完成")


def owned_process(state):
    try:
        process = psutil.Process(state["pid"])
        if process.create_time() == state["created_at"] and process.status() != psutil.STATUS_ZOMBIE:
            return process
    except (psutil.Error, KeyError):
        pass
    return None


def read_state(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def state_for(process):
    return {"pid": process.pid, "created_at": process.create_time()}


def healthy(state):
    if not owned_process(state) or not all(owned_process(p) for p in state.get("children", [])):
        return False
    try:
        with urlopen(state["health_url"], timeout=1) as response:
            return response.status == 200
    except (OSError, KeyError):
        return False


def server(args, path):
    run_dir = path.parent / "run"
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_path = run_dir / "server.json"
    state = read_state(state_path)
    process = owned_process(state)
    if args.action == "status":
        print(json.dumps({"running": bool(process), "healthy": healthy(state), "version": state.get("version"), "config": str(path), "pid": state.get("pid")}, ensure_ascii=False))
        return
    if args.action in {"stop", "restart"}:
        if process:
            process.terminate()
            try:
                process.wait(timeout=15)
            except psutil.TimeoutExpired:
                raise RuntimeError("服务尚未停止，请检查 server.log") from None
        if args.action == "stop":
            print("已停止")
            return
    elif process:
        raise RuntimeError("服务已在运行，请先 stop 或使用 restart")
    if args.action != "run":
        with (run_dir / "server.log").open("ab") as log:
            child = subprocess.Popen(
                [sys.executable, "-m", "courseware.cli", "server", "run", "--host", args.host,
                 "--port", str(args.port), "--config", str(path)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            )
        for _ in range(100):
            state = read_state(state_path)
            if state.get("pid") == child.pid and healthy(state):
                print(f"courseware-api {state['version']} 已启动，API 和 Worker 正常 (PID {child.pid})")
                return
            if child.poll() is not None:
                raise RuntimeError(f"启动失败，请查看 {run_dir / 'server.log'}")
            time.sleep(0.1)
        child.terminate()
        child.wait(timeout=15)
        raise RuntimeError(f"启动超时，请查看 {run_dir / 'server.log'}")
    run(args, path, state_path)


def run(args, path, state_path):
    from sqlalchemy import text

    from .config import settings
    from .db import engine

    with (state_path.parent / "server.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("另一个服务进程正在启动或运行") from None
        with socket.socket() as probe:
            probe.bind((args.host, args.port))
        with engine().connect() as db:
            db.execute(text("SELECT 1 FROM alembic_version"))
        if not (settings().storage_path / "releases").is_dir():
            raise RuntimeError("发布目录尚未初始化，请先执行 courseware-api init")
        stopping = False

        def stop(*_):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        children = []
        try:
            for command in [
                [sys.executable, "-m", "uvicorn", "courseware.main:app", "--host", args.host, "--port", str(args.port)],
                [sys.executable, "-m", "courseware.worker"],
            ]:
                children.append(subprocess.Popen(command))
            host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
            state = state_for(psutil.Process()) | {
                "children": [state_for(psutil.Process(p.pid)) for p in children],
                "version": version("courseware-api"), "health_url": f"http://{host}:{args.port}/health/ready",
            }
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state))
            temporary.replace(state_path)
            while not stopping:
                if any(p.poll() is not None for p in children):
                    raise RuntimeError("API 或 Worker 已退出；停止整组服务，请查看日志")
                time.sleep(0.2)
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
            for child in children:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            if read_state(state_path).get("pid") == os.getpid():
                state_path.unlink(missing_ok=True)
