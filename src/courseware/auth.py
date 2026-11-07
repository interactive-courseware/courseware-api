import hashlib
import hmac
import secrets
import time
from datetime import timedelta
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .dto import AuthView
from .errors import Problem
from .models import AuditEvent, LoginAttempt, SessionToken, User, now, uid

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])
DB = Annotated[Session, Depends(get_db)]
hasher = PasswordHasher()
DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))
COOKIE = "cw_session"
ANON_COOKIE = "cw_login"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class LoginInput(Input):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class PasswordInput(Input):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=12, max_length=200)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def sign(value: str) -> str:
    return hmac.new(settings().secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()


def verify_password(encoded: str, password: str) -> bool:
    try:
        return hasher.verify(encoded, password)
    except VerificationError:
        return False


def csrf_token(session: SessionToken) -> str:
    return sign("csrf:" + session.id)


def check_origin(request: Request):
    if request.headers.get("origin") != settings().public_origin:
        raise Problem(403, "CSRF_INVALID", "请求来源无效，请刷新页面后重试")


def current_user(request: Request, db: DB) -> User:
    token = request.cookies.get(COOKIE)
    session = (
        db.scalar(select(SessionToken).where(SessionToken.token_hash == digest(token))) if token else None
    )
    timestamp = now()
    if (
        not session
        or session.revoked_at
        or session.expires_at <= timestamp
        or session.last_seen_at + timedelta(hours=settings().idle_hours) <= timestamp
    ):
        raise Problem(401, "SESSION_REQUIRED", "请先登录")
    user = db.get(User, session.user_id)
    if not user or user.status != "active":
        raise Problem(401, "SESSION_REQUIRED", "账号已停用，请联系管理员")
    request.state.session = session
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), csrf_token(session)):
            raise Problem(403, "CSRF_INVALID", "登录状态已变化，请刷新页面")
    if session.last_seen_at + timedelta(minutes=5) < timestamp:
        session.last_seen_at = timestamp
        db.commit()
    return user


def member(user: Annotated[User, Depends(current_user)]) -> User:
    if user.must_change_password:
        raise Problem(403, "PASSWORD_CHANGE_REQUIRED", "请先修改初始密码")
    return user


def admin(user: Annotated[User, Depends(member)]) -> User:
    if user.role != "admin":
        raise Problem(403, "ADMIN_REQUIRED", "此操作需要管理员权限")
    return user


Member = Annotated[User, Depends(member)]
Admin = Annotated[User, Depends(admin)]


def user_dto(user: User):
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "status": user.status,
        "must_change_password": user.must_change_password,
    }


def revoke_sessions(db: Session, user_id: str):
    db.execute(
        update(SessionToken)
        .where(SessionToken.user_id == user_id, SessionToken.revoked_at.is_(None))
        .values(revoked_at=now())
    )


def set_cookie(response: Response, name: str, token: str, seconds: int):
    response.set_cookie(
        name,
        token,
        max_age=seconds,
        httponly=True,
        secure=settings().secure_cookies,
        samesite="lax",
        path="/",
    )


@router.get("/csrf")
def anonymous_csrf(response: Response):
    context = f"{int(time.time())}:{secrets.token_urlsafe(32)}"
    value = context + ":" + sign("login:" + context)
    set_cookie(response, ANON_COOKIE, value, 600)
    return {"csrf_token": sign("anonymous:" + value)}


@router.post("/login", response_model=AuthView)
def login(body: LoginInput, request: Request, response: Response, db: DB):
    check_origin(request)
    context = request.cookies.get(ANON_COOKIE, "")
    try:
        timestamp, nonce, signature = context.split(":")
        valid = (
            0 <= time.time() - int(timestamp) <= 600
            and hmac.compare_digest(signature, sign(f"login:{timestamp}:{nonce}"))
            and hmac.compare_digest(request.headers.get("x-csrf-token", ""), sign("anonymous:" + context))
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise Problem(403, "CSRF_INVALID", "登录页面已过期，请刷新后重试")
    username = body.username.strip().lower()
    ip = request.client.host if request.client else "unknown"
    # Serialize attempts across API processes, rather than keeping per-process counters.
    db.execute(text("SELECT pg_advisory_xact_lock(8101)"))
    cutoff = now() - timedelta(minutes=15)
    db.execute(delete(LoginAttempt).where(LoginAttempt.created_at < cutoff))
    count = db.scalar(
        select(func.count())
        .select_from(LoginAttempt)
        .where(or_(LoginAttempt.username == username, LoginAttempt.ip == ip))
    )
    if count >= 10:
        raise Problem(429, "LOGIN_RATE_LIMITED", "登录尝试过于频繁，请 15 分钟后再试")
    user = db.scalar(select(User).where(User.username == username).with_for_update())
    valid_password = verify_password(user.password_hash if user else DUMMY_HASH, body.password)
    if not user or not valid_password or user.status != "active":
        db.add(LoginAttempt(username=username, ip=ip))
        db.commit()
        raise Problem(401, "LOGIN_FAILED", "用户名或密码不正确")
    old = request.cookies.get(COOKIE)
    if old:
        db.execute(
            update(SessionToken).where(SessionToken.token_hash == digest(old)).values(revoked_at=now())
        )
    if hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hasher.hash(body.password)
    token = secrets.token_urlsafe(48)
    session = SessionToken(
        id=uid(),
        user_id=user.id,
        token_hash=digest(token),
        expires_at=now() + timedelta(hours=settings().session_hours),
    )
    db.add(session)
    db.commit()
    set_cookie(response, COOKIE, token, settings().session_hours * 3600)
    response.delete_cookie(ANON_COOKIE, path="/")
    return {"user": user_dto(user), "csrf_token": csrf_token(session)}


@router.get("/me", response_model=AuthView)
def me(request: Request, user: Annotated[User, Depends(current_user)]):
    return {"user": user_dto(user), "csrf_token": csrf_token(request.state.session)}


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: DB, user: Annotated[User, Depends(current_user)]):
    request.state.session.revoked_at = now()
    db.commit()
    response.delete_cookie(COOKIE, path="/")


@router.put("/password", status_code=204)
def change_password(
    body: PasswordInput, response: Response, db: DB, user: Annotated[User, Depends(current_user)]
):
    user = db.scalar(select(User).where(User.id == user.id).with_for_update())
    if not verify_password(user.password_hash, body.current_password):
        raise Problem(400, "PASSWORD_INVALID", "当前密码不正确")
    if body.new_password == body.current_password:
        raise Problem(422, "PASSWORD_UNCHANGED", "新密码不能与当前密码相同")
    user.password_hash = hasher.hash(body.new_password)
    user.must_change_password = False
    revoke_sessions(db, user.id)
    db.add(AuditEvent(actor_user_id=user.id, action="password_changed"))
    db.commit()
    response.delete_cookie(COOKIE, path="/")
