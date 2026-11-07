import hashlib
import json
import mimetypes
import re
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator
from PIL import Image, UnidentifiedImageError

from .config import settings
from .errors import Problem

SCHEMA_PATH = Path(__file__).parent / "schemas/course.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text())
VALIDATOR_VERSION = "1.0.0-" + hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()[:12]
PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*\Z")
FORBIDDEN = {
    "_template",
    "refer",
    "teacher",
    "private",
    "scripts",
    "tests",
    "logs",
    "node_modules",
    "__pycache__",
    "credentials.json",
    "credentials.yaml",
    "secrets.json",
    "secrets.yaml",
    "id_rsa",
    "id_ed25519",
}
CSS_URL = re.compile(r"url\(\s*['\"]?([^\s)'\"]+)", re.I)
CSS_IMPORT = re.compile(r"@import\s+['\"]([^'\"]+)", re.I)


def invalid(message, path="course.json"):
    raise Problem(422, "COURSE_MANIFEST_INVALID", message, [{"path": path}])


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            invalid(f"JSON 存在重复键：{key}")
        result[key] = value
    return result


def load_manifest(raw: bytes, slug: str) -> dict:
    if len(raw) > 1024 * 1024:
        invalid("course.json 超过 1 MiB")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicate_keys)
    except (ValueError, UnicodeError):
        invalid("course.json 不是有效的 UTF-8 JSON")
    error = next(Draft202012Validator(SCHEMA).iter_errors(data), None)
    if error:
        invalid(error.message, ".".join(map(str, error.absolute_path)) or "course.json")
    if data["slug"] != slug:
        invalid("清单 slug 与登记的课程不一致", "slug")
    entities = {}
    for kind in ("images", "creators", "groups", "chapters", "lessons", "resources"):
        entities[kind] = {item["id"]: item for item in data[kind]}
        if len(entities[kind]) != len(data[kind]):
            invalid(f"{kind} 存在重复 ID", kind)

    def reference(kind, ids, location):
        if not set(ids) <= entities[kind].keys():
            invalid(f"{location} 引用了不存在的 {kind}", location)

    for chapter in data["chapters"]:
        parent = chapter.get("parent_id")
        if parent:
            reference("chapters", [parent], chapter["id"])
            if parent == chapter["id"] or entities["chapters"][parent].get("parent_id"):
                invalid("章节不能成环或超过两层", chapter["id"])
    entries = [data["entry"]]
    for lesson in data["lessons"]:
        reference("groups", [lesson["group_id"]], lesson["id"])
        reference("chapters", lesson["chapter_ids"], lesson["id"])
        reference("creators", lesson["creator_ids"], lesson["id"])
        if lesson.get("cover_image_id"):
            reference("images", [lesson["cover_image_id"]], lesson["id"])
        if lesson.get("entry"):
            entries.append(lesson["entry"])
    if len(set(entries)) != len(entries):
        invalid("课程与课时入口不能重复")
    for resource in data["resources"]:
        reference("lessons", resource["lesson_ids"], resource["id"])
    for path in data["publish"]["include"]:
        safe_path(path.rstrip("/"))
        if path.startswith("lessons/") and len(PurePosixPath(path).parts) < (4 if path.endswith("/") else 5):
            invalid("课时发布范围必须精确到课时内容子目录", path)

    def nonblank(value):
        if isinstance(value, str) and not value.strip():
            invalid("文字字段不能仅包含空白")
        elif isinstance(value, dict):
            for child in value.values():
                nonblank(child)
        elif isinstance(value, list):
            for child in value:
                nonblank(child)

    nonblank(data)
    return data


def safe_path(path: str):
    parts = PurePosixPath(path).parts
    if (
        not PATH.fullmatch(path)
        or any(p.lower() in FORBIDDEN for p in parts)
        or any(p.lower().endswith((".pem", ".key", ".env")) for p in parts)
        or parts[0] not in {"web", "lessons", "assets"}
    ):
        invalid("不允许发布此路径", path)


def included(path, includes):
    return any(path.startswith(item) if item.endswith("/") else path == item for item in includes)


def selected_files(root: Path, manifest: dict) -> list[Path]:
    selected = set()
    for item in manifest["publish"]["include"]:
        source = root / item
        for ancestor in [source, *source.parents]:
            if ancestor == root:
                break
            if ancestor.is_symlink():
                invalid("发布路径不能包含符号链接", item)
        if not source.exists() or source.is_dir() != item.endswith("/"):
            invalid("白名单路径不存在或目录缺少结尾斜杠", item)
        candidates = source.rglob("*") if source.is_dir() else [source]
        for path in candidates:
            if path.is_symlink():
                invalid("不允许发布符号链接", str(path.relative_to(root)))
            if path.is_file():
                relative = str(path.relative_to(root))
                safe_path(relative)
                selected.add(path)
                if len(selected) > settings().max_files:
                    invalid("发布文件数量超过限制")
    return sorted(selected)


def validate_package(root: Path, manifest: dict) -> list[dict]:
    files = selected_files(root, manifest)
    allowed = {str(p.relative_to(root)) for p in files}
    total = 0
    result = []
    for path in files:
        size = path.stat().st_size
        total += size
        if size > settings().max_file_bytes or total > settings().max_package_bytes:
            invalid("发布包或单个文件超过大小限制", str(path.relative_to(root)))
        with path.open("rb") as handle:
            prefix = handle.read(100)
            if prefix.startswith(b"version https://git-lfs.github.com/spec/"):
                invalid("Git LFS 文件尚未展开", str(path.relative_to(root)))
            handle.seek(0)
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in {".m", ".py", ".sh"}:
            mime = "application/octet-stream"
        result.append(
            {"path": str(path.relative_to(root)), "sha256": checksum, "size_bytes": size, "media_type": mime}
        )
    required = [manifest["entry"]] + [
        lesson["entry"] for lesson in manifest["lessons"] if lesson.get("entry")
    ]
    required += [r["path"] for r in manifest["resources"]]
    images = manifest["images"] + [c["avatar"] for c in manifest["creators"] if c.get("avatar")]
    required += [image["path"] for image in images]
    for path in required:
        if path not in allowed:
            invalid("清单引用的文件不在发布范围内", path)
    for item in images:
        try:
            with Image.open(root / item["path"]) as image:
                if image.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                    invalid("不支持的图片格式", item["path"])
                image.verify()
                if any(item.get(key, value) != value for key, value in zip(("width", "height"), image.size)):
                    invalid("图片声明尺寸与文件不一致", item["path"])
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            invalid("图片损坏或超过像素限制", item["path"])
    validate_links(root, allowed)
    return result


def validate_links(root: Path, allowed: set[str]):
    def check(origin, url, external=False):
        url = url.strip()
        if not url or url.startswith("#"):
            return
        parsed = urlsplit(url)
        if parsed.scheme in {"data", "blob"}:
            return
        if parsed.scheme or parsed.netloc:
            if external and parsed.scheme in {"https", "mailto"}:
                return
            invalid("课程运行资源必须为本地相对路径", origin)
        if parsed.path.startswith("/") or "\\" in parsed.path:
            invalid("课程引用不能使用根绝对路径", origin)
        target = (root / origin).parent.joinpath(unquote(parsed.path)).resolve()
        if not target.is_relative_to(root) or str(target.relative_to(root)) not in allowed:
            invalid(f"{origin}：引用不存在或超出发布范围：{url}", origin)

    def css(origin, source):
        for expression in (CSS_URL, CSS_IMPORT):
            for url in expression.findall(source):
                check(origin, url)

    class Parser(HTMLParser):
        def __init__(self, origin):
            super().__init__()
            self.origin = origin
            self.style = False

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "base":
                invalid("课程不能使用 base 标签", self.origin)
            if tag == "style":
                self.style = True
            for key in ("src", "href", "poster", "data"):
                if attrs.get(key):
                    check(self.origin, attrs[key], external=tag == "a" and key == "href")
            if attrs.get("srcset"):
                for candidate in attrs["srcset"].split(","):
                    if candidate.strip():
                        check(self.origin, candidate.strip().split()[0])
            if attrs.get("style"):
                css(self.origin, attrs["style"])

        def handle_endtag(self, tag):
            if tag == "style":
                self.style = False

        def handle_data(self, data):
            if self.style:
                css(self.origin, data)

    for path in allowed:
        suffix = Path(path).suffix.lower()
        if suffix not in {".html", ".css", ".js", ".mjs"}:
            continue
        try:
            content = (root / path).read_text(encoding="utf-8")
        except UnicodeError:
            invalid("网页文本必须使用 UTF-8", path)
        if suffix == ".html":
            Parser(path).feed(content)
        elif suffix == ".css":
            css(path, content)
        if suffix in {".html", ".js", ".mjs"}:
            if re.search(r"serviceWorker\s*\.\s*register", content):
                invalid("v1 不允许 Service Worker", path)
            if re.search(r"(?:fetch|WebSocket|EventSource)\s*\(\s*['\"](?:/|https?:)", content):
                invalid("不允许硬编码网络 API 或绝对资源路径", path)
        if suffix in {".js", ".mjs"}:
            for url in re.findall(r"(?<![\w$])(?:from\s*|import\s*\(?\s*)['\"]([^'\"]+)['\"]", content):
                check(path, url)
            for url in re.findall(r"new\s+URL\(\s*['\"]([^'\"]+)['\"]\s*,\s*import\.meta\.url", content):
                check(path, url)
