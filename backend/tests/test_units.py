import pytest

from bisect_agent.gitops import GitError, touches_dependencies, validate_repo_url
from bisect_agent.nebius import _split_reasoning
from bisect_agent.router import ROUTES, Router, Tier, parse_json
from bisect_agent.tavily import parse_dependency_changes


def test_every_route_has_a_tier():
    assert set(ROUTES.values()) == {Tier.REASON, Tier.FAST}
    with pytest.raises(KeyError):
        Router.tier_for("not_a_task")


def test_parse_json_variants():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": [1, 2]} hope that helps') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        parse_json("no json here")


def test_reasoning_split():
    assert _split_reasoning({"content": "<think>hmm</think>{\"x\":1}"}) == ('{"x":1}', "hmm")
    assert _split_reasoning({"content": "ok", "reasoning_content": "r"}) == ("ok", "r")


def test_url_validation():
    assert validate_repo_url("github.com/psf/requests") == "https://github.com/psf/requests.git"
    assert validate_repo_url("https://github.com/psf/requests/tree/main") == "https://github.com/psf/requests.git"
    for bad in ("file:///etc", "https://evil.example/x/y", "git@github.com:a/b", "https://github.com/onlyowner"):
        with pytest.raises(GitError):
            validate_repo_url(bad)


def test_dependency_detection_and_parsing():
    assert touches_dependencies(["src/a.py", "requirements.txt", "pyproject.toml"]) == ["requirements.txt", "pyproject.toml"]
    diff = """diff --git a/requirements.txt b/requirements.txt
--- a/requirements.txt
+++ b/requirements.txt
@@ -1,3 +1,3 @@
-urllib3==1.26.18
+urllib3==2.0.0
 six>=1.0
diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -5,2 +5,2 @@
-    "chardet>=3.0.2,<5",
+    "chardet>=3.0.2,<6",
"""
    assert parse_dependency_changes(diff) == [
        {"package": "chardet", "old": ">=3.0.2,<5", "new": ">=3.0.2,<6"},
        {"package": "urllib3", "old": "==1.26.18", "new": "==2.0.0"},
    ]


def test_edit_locator_survives_reformatting():
    from bisect_agent.agent import _replace_once
    head = ("            elif (ttype not in (T.Whitespace, T.Comment.Single,\n"
            "                                T.Comment.Multiline)\n"
            "                  and not (ttype is T.Keyword)):\n"
            "                # Reset if we see a non-whitespace, non-comment\n"
            "                # token\n"
            "                self._seen_begin = False\n")
    # Same code as it looked before a later "code cleanup" re-wrapped it.
    find = ("            elif (ttype not in (T.Whitespace, T.Comment.Single, T.Comment.Multiline)\n"
            "                  and not (ttype is T.Keyword)):\n"
            "                # Reset if we see a non-whitespace, non-comment token\n"
            "                self._seen_begin = False")
    out = _replace_once(head, find, "            pass")
    assert out is not None and out.strip() == "pass"
    assert _replace_once("a = 1\nb = 2\na = 1\n", "a = 1", "x") is None  # ambiguous: refuse


def test_behavioural_file_classes():
    from bisect_agent.agent import _counterfactual_status, _is_code
    assert _is_code("pathspec/patterns/gitwildmatch.py")
    for path in ("pathspec/_meta.py", "tests/test_gitwildmatch.py", "conftest.py", "src/pkg/_version.py", "CHANGES.rst"):
        assert not _is_code(path), path
    assert [_counterfactual_status(x) for x in ("passed", "failed", "error", "timeout")] == \
        ["confirmed", "not_confirmed", "inconclusive", "inconclusive"]
