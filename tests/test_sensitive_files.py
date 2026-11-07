import pytest

from courseware.contract import load_manifest, validate_package
from courseware.errors import Problem


def test_common_credential_files_are_not_published_even_under_an_allowed_directory(demo):
    data = load_manifest((demo / "course.json").read_bytes(), "courseware-demo")
    (demo / "web/credentials.json").write_text('{"test": "fixture, not a real credential"}')
    with pytest.raises(Problem, match="不允许发布此路径"):
        validate_package(demo, data)
