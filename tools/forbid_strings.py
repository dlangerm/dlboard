"""
Forbid certain strings in Python files at precommit time.

Note: This script is pre-commit so we can't use any dependencies outside of the std lib.

A line that has to use a forbidden string opts out with a `# dlboard-ignore[forbid-string-ignore]` comment. An ignore
comment on a line with nothing to excuse is an error, and `--fix` removes it, the way ruff removes an unused `noqa`.
"""

import argparse
import io
import re
import sys
import tokenize
import typing
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

IGNORE_FORBID_LINE: typing.Final = re.compile(r"#\s*dlboard-ignore\[forbid-string-ignore\]")
# Test code may use anything a test needs, like `monkeypatch.setattr` or a `print` while debugging.
TEST_FILE_NAME: typing.Final = re.compile(r".*_test\.py|conftest\.py")


@dataclass(frozen=True, slots=True, kw_only=True)
class ForbiddenString:
    """
    A string that is forbidden in the codebase.

    A match is reported on the line where its `at` group starts, if the pattern has one, else where the match starts.
    """

    pattern: re.Pattern[str]
    message: str


# The builtins are matched on their own: `(?<![\w.])` skips methods such as `console.print(` or `model.eval()`.
FORBIDDEN_STRINGS: typing.Final = (
    ForbiddenString(
        pattern=re.compile(r"(?<![\w.])print\("),
        message="Use `logging` instead of `print` for logging.",
    ),
    # don't use setattr
    ForbiddenString(
        pattern=re.compile(r"(?<![\w.])setattr\("),
        message="Set attributes using dot notation or `dataclasses.replace` instead of `setattr`.",
    ),
    # don't use object.__setattr__ directly, use dataclasses.replace instead
    ForbiddenString(
        pattern=re.compile(r"\bobject\.__setattr__\("),
        message="Set attributes using dot notation or `dataclasses.replace` instead of `object.__setattr__`.",
    ),
    # don't use pdb.set_trace
    ForbiddenString(
        pattern=re.compile(r"\bpdb\.set_trace\("),
        message="Use `breakpoint()` instead of `pdb.set_trace`.",
    ),
    # don't allow getattr in general
    ForbiddenString(
        pattern=re.compile(r"(?<![\w.])getattr\("),
        message="Use dot notation instead of `getattr`.",
    ),
    # don't allow eval in general
    ForbiddenString(
        pattern=re.compile(r"(?<![\w.])eval\("),
        message="Use `ast.literal_eval` instead of `eval`.",
    ),
    # don't allow exec in general
    ForbiddenString(
        pattern=re.compile(r"(?<![\w.])exec\("),
        message="Use `exec` only in very specific cases, and avoid it if possible.",
    ),
    # don't allow execfile in general
    ForbiddenString(
        pattern=re.compile(r"(?<![\w.])execfile\("),
        message="Use `execfile` only in very specific cases, and avoid it if possible.",
    ),
    # don't read the process environment directly, use pydantic settings instead
    ForbiddenString(
        pattern=re.compile(r"\bos\.environ\b"),
        message="Use pydantic settings instead of reading the process environment.",
    ),
    # don't allow os.system in general, use subprocess instead
    ForbiddenString(
        pattern=re.compile(r"\bos\.system\("),
        message="Use `subprocess` instead of `os.system`.",
    ),
    # description in Field should be an attribute docstring, which pydantic reads as the description. The call may
    # span lines, and a Field whose arguments nest parentheses two levels deep is not searched.
    ForbiddenString(
        pattern=re.compile(r"\bField\((?:[^()]|\([^()]*\))*?(?P<at>\bdescription=[\"'(])"),
        message="Use an attribute docstring on the field instead of `description` in `Field`.",
    ),
    # don't use the os module's path helpers, use pathlib instead
    ForbiddenString(
        pattern=re.compile(r"\bos\.path\b"),
        message="Use `pathlib` rather than the path helpers in the `os` module.",
    ),
)


def _ignore_comments(source: str) -> dict[int, tokenize.TokenInfo]:
    """The ignore comments in `source`, by line number. Only real comments count, not the same text inside a string."""
    return {
        token.start[0]: token
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT and IGNORE_FORBID_LINE.match(token.string)
    }


def _without_ignore_comments(lines: list[str], ignores: dict[int, tokenize.TokenInfo]) -> str:
    """
    `lines` with each of `ignores` taken out.

    Other text the comment carried beside its marker stays, and a line that was nothing but the comment goes.
    """
    kept: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        token = ignores.get(line_number)
        if token is None:
            kept.append(line)
            continue
        code = line[: token.start[1]]
        ending = line[len(line.rstrip("\r\n")) :]
        rest = IGNORE_FORBID_LINE.sub("", token.string, count=1).strip()
        if rest:
            kept.append(f"{code}# {rest}{ending}")
        elif code.strip():
            kept.append(f"{code.rstrip()}{ending}")
    return "".join(kept)


def _line_of(source: str, match: re.Match[str]) -> int:
    """The line a match is reported on: where its `at` group starts, if it has one, else where the match starts."""
    position = match.start("at") if "at" in match.re.groupindex else match.start()
    return source.count("\n", 0, position) + 1


def _violations(source: str) -> list[tuple[int, ForbiddenString]]:
    """Each line of `source` that breaks a rule, with the rule it breaks, in line order."""
    found = [
        (line_number, forbidden)
        for forbidden in FORBIDDEN_STRINGS
        for line_number in {_line_of(source, match) for match in forbidden.pattern.finditer(source)}
    ]
    return sorted(found, key=lambda violation: violation[0])


def _check(file_path: Path, *, fix: bool) -> Iterator[str]:
    """
    Each forbidden string in `file_path` that no ignore comment excuses, and each ignore comment that excuses nothing.

    Test code isn't checked, so an ignore comment there excuses nothing either. With `fix`, the unused ignore comments
    are removed from the file instead of reported.
    """
    source = file_path.read_text(encoding="utf-8", errors="replace")
    # Split on "\n" alone, the way the tokenizer counts lines: `splitlines` also breaks on form feeds and U+2028.
    lines = io.StringIO(source).readlines()
    try:
        ignores = _ignore_comments(source)
    except (tokenize.TokenError, SyntaxError) as error:
        yield f"{file_path}: can't read its comments, so no ignore comment is honored: {error}"
        ignores = {}

    forbidden_lines: set[int] = set()
    if TEST_FILE_NAME.fullmatch(file_path.name) is None:
        for line_number, forbidden in _violations(source):
            forbidden_lines.add(line_number)
            if line_number not in ignores:
                yield (
                    f"{file_path}:{line_number}: Found forbidden string "
                    f"'{forbidden.pattern.pattern}': {forbidden.message}"
                )

    unused = {
        line_number: token for line_number, token in ignores.items() if line_number not in forbidden_lines
    }
    if not unused:
        return
    if fix:
        file_path.write_text(_without_ignore_comments(lines, unused), encoding="utf-8")
        sys.stdout.write(f"{file_path}: removed {len(unused)} unused ignore comment(s)\n")
        return
    for line_number in unused:
        yield (
            f"{file_path}:{line_number}: Unused ignore comment: nothing on this line is reported, "
            "so there is nothing to ignore. Run with `--fix` to remove it."
        )


def _main() -> None:
    """
    Check the given Python files for forbidden strings and unused ignore comments.

    Args:
        source_files: The files to check. pre-commit passes the staged ones.
        --fix: Remove the unused ignore comments from the files, instead of reporting them.

    """
    parser = argparse.ArgumentParser(description="Check for forbidden strings in the given Python files.")
    parser.add_argument(
        "source_files",
        nargs="*",
        type=Path,
        help="Files to check for forbidden strings.",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Remove the ignore comments that excuse nothing, instead of reporting them.",
    )
    args = parser.parse_args()

    errors = [error for file_path in args.source_files for error in _check(file_path, fix=args.fix)]
    if errors:
        sys.stderr.write("\n".join(errors) + "\n")
        sys.exit(1)


if __name__ == "__main__":
    _main()
