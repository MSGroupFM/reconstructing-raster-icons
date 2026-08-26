#!/usr/bin/env python3
"""Validate the release-facing skill entrypoint and discovery metadata."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path, PureWindowsPath
import re
import sys
from urllib.parse import unquote, urlsplit


EXPECTED_NAME = "reconstructing-raster-icons"
ALLOWED_FRONTMATTER_KEYS = frozenset({"name", "description"})
ALLOWED_INTERFACE_KEYS = frozenset(
    {
        "display_name",
        "short_description",
        "default_prompt",
        "icon_small",
        "icon_large",
        "brand_color",
    }
)
LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(([^)\n]+)\)")
PLACEHOLDER_RE = re.compile(
    r"\[TODO(?::[^\]\n]*)?\]|\bTODO\b|\{\{[^{}\n]+\}\}|"
    r"<(?:PLACEHOLDER|INSERT[^>\n]*|REPLACE[^>\n]*|YOUR[^>\n]*)>",
    re.IGNORECASE,
)


@dataclass(frozen=True, order=True)
class Issue:
    code: str
    detail: str


def _read_text(path: Path, relative: str, issues: list[Issue]) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        issues.append(Issue("file.missing", relative))
    except UnicodeError:
        issues.append(Issue("file.encoding", f"{relative}: expected UTF-8"))
    except OSError as error:
        issues.append(Issue("file.read", f"{relative}: {error.__class__.__name__}"))
    return None


def _plain_scalar(raw: str, *, relative: str, line_number: int, issues: list[Issue]) -> str | None:
    value = raw.strip()
    if not value:
        issues.append(Issue("frontmatter.scalar", f"{relative}:{line_number}: empty value"))
        return None
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            issues.append(Issue("frontmatter.scalar", f"{relative}:{line_number}: invalid quoted value"))
            return None
        if not isinstance(decoded, str):
            issues.append(Issue("frontmatter.scalar", f"{relative}:{line_number}: expected string"))
            return None
        return decoded
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            issues.append(Issue("frontmatter.scalar", f"{relative}:{line_number}: invalid quoted value"))
            return None
        return value[1:-1].replace("''", "'")
    if value[0] in "[{&*!>|@`" or value.endswith(":"):
        issues.append(Issue("frontmatter.scalar", f"{relative}:{line_number}: complex YAML is not allowed"))
        return None
    return value


def _validate_frontmatter(text: str, issues: list[Issue]) -> None:
    relative = "SKILL.md"
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        issues.append(Issue("frontmatter.missing", f"{relative}: expected opening ---"))
        return
    try:
        closing_index = lines.index("---", 1)
    except ValueError:
        issues.append(Issue("frontmatter.missing", f"{relative}: expected closing ---"))
        return

    values: dict[str, str] = {}
    for index, line in enumerate(lines[1:closing_index], start=2):
        if not line.strip():
            continue
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*):[ \t]*(.*)", line)
        if not match:
            issues.append(Issue("frontmatter.syntax", f"{relative}:{index}"))
            continue
        key, raw_value = match.groups()
        if key in values:
            issues.append(Issue("frontmatter.duplicate_key", f"{relative}: {key}"))
            continue
        parsed = _plain_scalar(raw_value, relative=relative, line_number=index, issues=issues)
        if parsed is not None:
            values[key] = parsed

    for key in sorted(set(values) - ALLOWED_FRONTMATTER_KEYS):
        issues.append(Issue("frontmatter.unknown_key", f"{relative}: {key}"))
    for key in sorted(ALLOWED_FRONTMATTER_KEYS - set(values)):
        issues.append(Issue("frontmatter.missing_key", f"{relative}: {key}"))
    if values.get("name") not in {None, EXPECTED_NAME}:
        issues.append(Issue("frontmatter.name", f"{relative}: expected {EXPECTED_NAME}"))
    if "description" in values and not values["description"].strip():
        issues.append(Issue("frontmatter.description", f"{relative}: expected nonempty description"))


def _decode_quoted_yaml_string(raw: str) -> str | None:
    value = raw.strip()
    if len(value) < 2:
        return None
    if value.startswith('"') and value.endswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, str) else None
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return None


def _validate_agents(text: str, issues: list[Issue]) -> None:
    relative = "agents/openai.yaml"
    sections: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        top = re.fullmatch(r"([a-z_][a-z0-9_]*):", line)
        if top:
            current = top.group(1)
            if current in sections:
                issues.append(Issue("agents.duplicate_key", f"{relative}: {current}"))
            sections.setdefault(current, {})
            continue
        nested = re.fullmatch(r"  ([a-z_][a-z0-9_]*):[ \t]*(.+)", line)
        if current is None or not nested:
            issues.append(Issue("agents.syntax", f"{relative}:{line_number}"))
            continue
        key, value = nested.groups()
        if key in sections[current]:
            issues.append(Issue("agents.duplicate_key", f"{relative}: {current}.{key}"))
        else:
            sections[current][key] = value.strip()

    for section in sorted(set(sections) - {"interface", "policy"}):
        issues.append(Issue("agents.unknown_section", f"{relative}: {section}"))

    interface = sections.get("interface", {})
    for key in sorted(set(interface) - ALLOWED_INTERFACE_KEYS):
        issues.append(Issue("agents.unknown_key", f"{relative}: interface.{key}"))
    for key in ("display_name", "short_description", "default_prompt"):
        if key not in interface:
            issues.append(Issue("agents.missing_key", f"{relative}: interface.{key}"))

    decoded: dict[str, str] = {}
    for key, raw in sorted(interface.items()):
        value = _decode_quoted_yaml_string(raw)
        if value is None:
            issues.append(Issue("agents.string_unquoted", f"{relative}: interface.{key}"))
        else:
            decoded[key] = value

    short_description = decoded.get("short_description")
    if short_description is not None and not 25 <= len(short_description) <= 64:
        issues.append(Issue("agents.short_description", f"{relative}: expected 25..64 characters"))
    default_prompt = decoded.get("default_prompt")
    if default_prompt is not None and "$reconstructing-raster-icons" not in default_prompt:
        issues.append(Issue("agents.default_prompt", f"{relative}: missing $reconstructing-raster-icons"))

    policy = sections.get("policy", {})
    for key in sorted(set(policy) - {"allow_implicit_invocation"}):
        issues.append(Issue("agents.unknown_key", f"{relative}: policy.{key}"))
    implicit = policy.get("allow_implicit_invocation")
    if implicit is None:
        issues.append(Issue("agents.missing_key", f"{relative}: policy.allow_implicit_invocation"))
    elif implicit != "true":
        issues.append(Issue("agents.implicit_invocation", f"{relative}: expected true"))


def _markdown_target(raw: str) -> str:
    target = raw.strip()
    if target.startswith("<"):
        closing = target.find(">")
        return target[1:closing] if closing != -1 else target
    return target.split(maxsplit=1)[0]


def _validate_links(root: Path, text: str, issues: list[Issue]) -> None:
    relative = "SKILL.md"
    resolved_root = root.resolve()
    for match in LINK_RE.finditer(text):
        raw_target = _markdown_target(match.group(1))
        if not raw_target or raw_target.startswith("#"):
            continue
        parsed = urlsplit(raw_target)
        if parsed.scheme in {"http", "https", "mailto"}:
            continue
        target = unquote(parsed.path)
        if (
            parsed.scheme
            or Path(target).is_absolute()
            or PureWindowsPath(target).is_absolute()
            or target.startswith("~")
        ):
            issues.append(Issue("link.absolute", f"{relative}: {raw_target}"))
            continue
        resolved = (resolved_root / target).resolve()
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            issues.append(Issue("link.outside_root", f"{relative}: {raw_target}"))
            continue
        if not resolved.is_file():
            issues.append(Issue("link.missing", f"{relative}: {raw_target}"))


def _validate_placeholders(root: Path, texts: dict[Path, str], issues: list[Issue]) -> None:
    for path, text in sorted(texts.items(), key=lambda item: item[0].as_posix()):
        match = PLACEHOLDER_RE.search(text)
        if match:
            relative = path.relative_to(root).as_posix()
            issues.append(Issue("placeholder.unresolved", f"{relative}: {match.group(0)}"))


def validate_skill(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    if not root.is_dir():
        return [Issue("path.invalid", "skill root is not a directory")]

    public_texts: dict[Path, str] = {}
    skill_path = root / "SKILL.md"
    skill_text = _read_text(skill_path, "SKILL.md", issues)
    if skill_text is not None:
        public_texts[skill_path] = skill_text
        _validate_frontmatter(skill_text, issues)
        _validate_links(root, skill_text, issues)

    agents_path = root / "agents" / "openai.yaml"
    agents_text = _read_text(agents_path, "agents/openai.yaml", issues)
    if agents_text is not None:
        public_texts[agents_path] = agents_text
        _validate_agents(agents_text, issues)

    references = root / "references"
    if references.is_dir():
        for path in sorted(references.rglob("*.md")):
            relative = path.relative_to(root).as_posix()
            text = _read_text(path, relative, issues)
            if text is not None:
                public_texts[path] = text

    _validate_placeholders(root, public_texts, issues)
    return sorted(set(issues))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True, help="skill root to validate")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    issues = validate_skill(args.path)
    if issues:
        for issue in issues:
            print(f"ERROR {issue.code}: {issue.detail}", file=sys.stderr)
        return 1
    print("Skill is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
