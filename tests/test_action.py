"""action.yml must load on GitHub.

GitHub evaluates ``${{ ... }}`` anywhere in an action's metadata, descriptions
included, and refuses the whole file when an expression names a context an
action cannot see (``secrets`` above all). A local run of the step's script
never loads the manifest, so this is the only place such a mistake is caught
before a push.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ACTION = Path(__file__).parents[1] / "action.yml"
_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
# Contexts an action's metadata may use (`secrets` is not one of them).
_ALLOWED = {"inputs", "steps", "github", "runner", "env", "job", "strategy", "matrix"}
_FUNCTIONS = {"always", "success", "failure", "cancelled"}


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for value in node.values() for s in _strings(value)]
    if isinstance(node, list):
        return [s for value in node for s in _strings(value)]
    return []


def test_expressions_only_use_contexts_an_action_can_see() -> None:
    manifest = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    for text in _strings(manifest):
        for expression in _EXPRESSION.findall(text):
            names = set(re.findall(r"(?<![\w.'-])([A-Za-z_][\w-]*)\s*[.(]", expression))
            assert names <= _ALLOWED | _FUNCTIONS, f"{expression.strip()!r} in action.yml"


def test_descriptions_contain_no_expressions() -> None:
    manifest = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    sections = [manifest, *manifest["inputs"].values(), *manifest["outputs"].values()]
    for section in sections:
        assert "${{" not in str(section.get("description", "")), section["description"]
