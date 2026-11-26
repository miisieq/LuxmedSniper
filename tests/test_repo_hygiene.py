"""Guards against committing personal data.

The facility dumps pair real doctors' names with the clinic and service they work at, and they
come from behind the patient portal's login. `.gitignore` keeps them out, but a `git add -f`, a
renamed file or a future dump under a name the pattern does not match would all slip through.
This asserts the property itself - no tracked file carries doctor names - rather than trusting
the filename convention.
"""

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def tracked_json_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.json"],  # noqa: S607 - git off PATH is the point
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / name for name in result.stdout.split("\0") if name]


def walk(node):
    """Every dict in a decoded JSON document, at any depth."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


@pytest.mark.skipif(not (REPO_ROOT / ".git").exists(), reason="not a git checkout")
def test_no_tracked_json_file_carries_doctor_names():
    offenders = []
    for path in tracked_json_files():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # not our dumps; nothing to check
        if any("doctors" in node for node in walk(document)):
            offenders.append(path.relative_to(REPO_ROOT))

    assert offenders == [], (
        f"these tracked files carry doctors' names: {offenders}. "
        "Facility dumps stay local - see the luxmed-ids entry in .gitignore."
    )
