#!/usr/bin/env python3
"""Checks on the action.yml manifests themselves.

GitHub parses a composite action's whole `run:` block looking for expressions
before bash ever sees it, so a literal one written in a comment is enough to
make the action fail to load -- with a template error at a line number, not
anything about the comment. `yaml.safe_load` does not catch it: the file is
valid YAML. Broke `main` on 2026-09-06.
"""
import re
import unittest
from collections.abc import Iterator
from pathlib import Path

ACTIONS = Path(__file__).resolve().parent.parent.parent
RUN_BLOCK = re.compile(r"^(\s*)run: \|")


def run_block_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield (lineno, line) for every line inside a `run: |` block."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = RUN_BLOCK.match(line)
        if not match:
            continue
        indent = len(match.group(1))
        for j in range(i + 1, len(lines)):
            body = lines[j]
            if body.strip() and len(body) - len(body.lstrip()) <= indent:
                break
            yield j + 1, body


class TestActionManifests(unittest.TestCase):
    def manifests(self) -> list[Path]:
        found = sorted(ACTIONS.glob("*/action.yml"))
        self.assertTrue(found, f"no action.yml under {ACTIONS}")
        return found

    def test_no_github_expression_inside_a_run_block(self) -> None:
        for manifest in self.manifests():
            for lineno, line in run_block_lines(manifest.read_text()):
                self.assertNotIn(
                    "${" + "{", line,
                    f"{manifest.parent.name}/action.yml:{lineno} writes a GitHub "
                    "expression inside run:; pass the value through env: instead",
                )

    def test_every_input_is_forwarded_through_env(self) -> None:
        # The other half of the same rule: an input is only usable if the step
        # puts it in env:, so a declared-but-unwired input is a silent no-op.
        for manifest in self.manifests():
            text = manifest.read_text()
            # Only the `inputs:` block: `runs:` has two-space keys of its own.
            block = text.split("\ninputs:\n", 1)[1].split("\nruns:", 1)[0]
            declared = set(re.findall(r"^  ([a-z][a-z0-9-]*):$", block, re.M))
            wired = set(re.findall(r"\$\{\{ inputs\.([a-z0-9-]+) \}\}", text))
            self.assertEqual(
                declared - wired, set(),
                f"{manifest.parent.name}/action.yml declares inputs it never "
                f"passes to the script: {sorted(declared - wired)}",
            )


if __name__ == "__main__":
    unittest.main()
