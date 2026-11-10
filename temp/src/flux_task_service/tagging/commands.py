"""Match executable command tokens, never instructions or arbitrary output text."""

import re
import shlex
from pathlib import PurePosixPath

from .models import TaggingPolicy

IGNORED_FLAGS = {
    "--help",
    "-h",
    "--version",
    "--collect-only",
    "--listTests",
    "--list-tests",
}
SEPARATORS = {";", "&&", "||", "|", "&", "\n"}
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def command_segments(command: str, depth: int = 0) -> list[list[str]]:
    """Unwrap common literal shell/env/runner wrappers without executing anything."""
    # Substitutions and here-docs can contain code/examples, not executed tests.
    if depth > 4 or "$(" in command or "`" in command or "<<" in command:
        return []
    try:
        lexer = shlex.shlex(
            command.replace("\n", ";"), posix=True, punctuation_chars=";&|<>"
        )
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
    except ValueError:
        return []
    groups: list[list[str]] = [[]]
    for token in tokens:
        if token in SEPARATORS:
            groups.append([])
        else:
            groups[-1].append(token)
    result = []
    for group in groups:
        while group and ENV_ASSIGNMENT.match(group[0]):
            group = group[1:]
        if not group:
            continue
        program = PurePosixPath(group[0]).name
        if program == "env":
            group = group[1:]
            while group and (ENV_ASSIGNMENT.match(group[0]) or group[0] == "--"):
                group = group[1:]
        if not group:
            continue
        program = PurePosixPath(group[0]).name
        if program in {"timeout", "gtimeout"}:
            group = group[1:]
            while group and group[0].startswith("-"):
                flag = group.pop(0)
                if flag in {"-s", "--signal", "-k", "--kill-after"} and group:
                    group.pop(0)
                elif flag not in {
                    "--",
                    "--foreground",
                    "--preserve-status",
                    "-v",
                    "--verbose",
                } and not flag.startswith(("--signal=", "--kill-after=")):
                    group = []
                    break
            if not group or not re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", group[0]):
                continue
            group = group[1:]
            if not group:
                continue
            program = PurePosixPath(group[0]).name
        if program in {"bash", "sh", "zsh"}:
            index = next(
                (
                    i
                    for i, x in enumerate(group[1:], 1)
                    if x.startswith("-") and "c" in x
                ),
                None,
            )
            if index is not None and index + 1 < len(group):
                result.extend(command_segments(group[index + 1], depth + 1))
            continue
        if program == "uv" and group[1:2] == ["run"]:
            group = group[2:]
            while group and group[0] in {"--", "--locked", "--frozen", "--no-sync"}:
                group = group[1:]
        elif program in {"npx", "pnpm", "yarn"} and group[1:2] == ["exec"]:
            group = group[2:]
        elif program == "npx":
            group = group[1:]
            if group and group[0] in {"--yes", "-y", "--no-install"}:
                group = group[1:]
        if group:
            group[0] = PurePosixPath(group[0]).name
            result.append(group)
    return result


def classify(tokens: list[str], policy: TaggingPolicy) -> tuple[str, str] | None:
    """Return (activity, type/rule); shell examples and help requests do not match."""
    if not tokens or any(flag in tokens for flag in IGNORED_FLAGS):
        return None
    for rule in policy.command_rules:
        if tokens[: len(rule.prefix)] == rule.prefix:
            return "test", rule.test_type
    program, args = tokens[0], tokens[1:]
    if is_review_command(tokens):
        return "test", "other"
    if program in {"pytest", "jest", "vitest", "mocha", "nosetests"}:
        return "test", "unit"
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", program) and args[:2] in (
        ["-m", "pytest"],
        ["-m", "unittest"],
    ):
        return "test", "unit"
    if program in {"go", "cargo", "dotnet"} and args[:1] == ["test"]:
        return "test", "unit"
    if program == "node" and "--test" in args:
        return "test", "unit"
    if program in {"playwright", "cypress"} and args[:1] in (["test"], ["run"]):
        return "test", "gui"
    if program in {"npm", "pnpm", "yarn", "emo"}:
        script = (
            args[1]
            if args[:1] in (["run"], ["run-script"]) and len(args) > 1
            else (args[0] if args else "")
        )
        if script in {"test", "test:unit"}:
            return "test", "unit"
        if script in {"lint", "typecheck", "build", "format"}:
            return "check", script
    if program == "go" and args[:1] == ["build"]:
        return "check", "build"
    if program in {"tsc", "mypy", "pyright"}:
        return "check", "typecheck"
    if program in {"eslint", "ruff"}:
        return "check", "lint"
    if program in {"oxfmt", "prettier", "black"}:
        return "check", "format"
    if program == "git" and args[:1] in (["commit"], ["push"]):
        return "git", args[0]
    return None


def is_review_command(tokens: list[str]) -> bool:
    return bool(
        len(tokens) > 1
        and re.fullmatch(r"python(?:3(?:\.\d+)?)?", tokens[0])
        and re.search(
            r"(?:^|/)bits-code-guard(?:-doubao)?/scripts/"
            r"(?:start|finish|generate_report)\.py$",
            tokens[1],
        )
    )
