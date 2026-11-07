import pytest

from courseware.contract import validate_links
from courseware.errors import Problem


def test_katex_commands_are_not_module_imports(tmp_path):
    (tmp_path / "web").mkdir()
    script = tmp_path / "web/katex.min.js"
    script.write_text(r'const commands=["\\xtofrom","\\xrightleftarrows"];const $from="ordinary text";')
    validate_links(tmp_path, {"web/katex.min.js"})


@pytest.mark.parametrize("statement", [
    'import "./dependency.js";',
    'import{x}from"./dependency.js";',
    'export{x}from"./dependency.js";',
    'import("./dependency.js");',
])
def test_real_javascript_imports_must_exist_in_publication(tmp_path, statement):
    (tmp_path / "web").mkdir()
    (tmp_path / "web/main.js").write_text(statement)
    with pytest.raises(Problem, match=r"web/main.js.*dependency.js"):
        validate_links(tmp_path, {"web/main.js"})
    (tmp_path / "web/dependency.js").write_text('export const x = 1;')
    validate_links(tmp_path, {"web/main.js", "web/dependency.js"})
