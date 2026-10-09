#!/usr/bin/env python3
"""Check skill packages and Git-visible files before distribution.

The credential patterns are a basic accidental-disclosure check, not a complete
secret scanner. They never print matching contents.
"""

from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]\n]*\]\(([^\s)]+)\)")
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
PRIVATE_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "personal absolute path": re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/"),
}
PRIVATE_PARTS = {".harness", ".venv", "private", "__pycache__", "node_modules"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_mapping(text, label):
    data = yaml.safe_load(text)
    require(isinstance(data, dict), f"{label}: expected a YAML mapping")
    return data


def check_links(path, boundary):
    # This repository uses inline Markdown links. Remote URLs and anchors are
    # deliberately excluded; this does not claim to validate external websites.
    for target in LINK.findall(path.read_text(encoding="utf-8")):
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        resolved = (path.parent / unquote(parsed.path)).resolve()
        require(resolved.is_relative_to(boundary), f"{path.relative_to(ROOT)}: link leaves package: {target}")
        require(resolved.exists(), f"{path.relative_to(ROOT)}: missing link target: {target}")


def check_skill(folder):
    manifest = folder / "SKILL.md"
    require(manifest.is_file(), f"{folder.name}: missing SKILL.md")
    text = manifest.read_text(encoding="utf-8")
    require(text.startswith("---\n"), f"{folder.name}: missing frontmatter")
    sections = text.split("\n---\n", 1)
    require(len(sections) == 2, f"{folder.name}: unterminated frontmatter")
    metadata = read_mapping(sections[0][4:], folder.name)
    name = metadata.get("name")
    description = metadata.get("description")
    require(isinstance(name, str) and NAME.fullmatch(name) and len(name) <= 64,
            f"{folder.name}: invalid skill name")
    require(name == folder.name, f"{folder.name}: name must match directory")
    require(isinstance(description, str) and 0 < len(description.strip()) <= 1024,
            f"{name}: invalid description")
    require("<" not in description and ">" not in description,
            f"{name}: description must not contain angle brackets")
    require(sections[1].strip(), f"{name}: empty instructions")

    ui_file = folder / "agents" / "openai.yaml"
    if ui_file.exists():
        ui = read_mapping(ui_file.read_text(encoding="utf-8"), str(ui_file))
        interface = ui.get("interface")
        require(isinstance(interface, dict), f"{name}: missing interface metadata")
        for key in ("display_name", "short_description", "default_prompt"):
            require(isinstance(interface.get(key), str) and interface[key].strip(),
                    f"{name}: missing interface.{key}")
        require(25 <= len(interface["short_description"]) <= 64,
                f"{name}: short_description must be 25–64 characters")
        require(f"${name}" in interface["default_prompt"],
                f"{name}: default_prompt must name the skill")
    for path in folder.rglob("*.md"):
        check_links(path, folder.resolve())


def check_public_files():
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    ).decode().split("\0")
    for filename in sorted(set(filter(None, paths))):
        path = ROOT / filename
        require(not path.is_symlink(), f"{filename}: symlinks are not distributed")
        require(path.resolve().is_relative_to(ROOT), f"{filename}: leaves repository")
        if not path.exists():  # A locally deleted tracked file is not distributed.
            continue
        parts = set(Path(filename).parts)
        require(not parts.intersection(PRIVATE_PARTS), f"{filename}: private/generated file")
        require(not path.name.startswith(".env") or path.name == ".env.example",
                f"{filename}: environment file")
        require(path.name not in {"settings.local.json", ".DS_Store"}, f"{filename}: local settings")
        text = path.read_text(encoding="utf-8")
        for label, pattern in PRIVATE_PATTERNS.items():
            require(not pattern.search(text), f"{filename}: possible {label}")
        if path.suffix == ".md" and "skills" not in parts:
            check_links(path, ROOT)
    return len(set(filter(None, paths)))


def main():
    folders = sorted(path for path in (ROOT / "skills").iterdir() if path.is_dir())
    require(folders, "No skill packages found")
    count = check_public_files()
    for folder in folders:
        check_skill(folder)
    print(f"Validated {len(folders)} skill package(s), {count} public file(s); local links and basic disclosure checks passed.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, yaml.YAMLError, subprocess.CalledProcessError) as error:
        print(f"Validation failed: {error}", file=sys.stderr)
        sys.exit(1)
