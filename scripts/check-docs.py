#!/usr/bin/env python3
"""Check local Markdown links and heading anchors without network access."""

from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import unquote


def prose(text: str) -> str:
    return re.sub(r"```.*?```|~~~.*?~~~", "", text, flags=re.S)


def anchors(path: Path) -> set[str]:
    text = path.read_text()
    result = set(re.findall(r'''(?:id|name)=["']([^"']+)''', text))
    counts: dict[str, int] = {}
    for title in re.findall(r"^#{1,6}\s+(.+?)\s*#*$", prose(text), re.M):
        title = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", title)
        title = re.sub(r"<[^>]+>", "", title).replace("`", "").replace("*", "").lower()
        slug = "".join(
            char for char in title
            if char in "-_ " or unicodedata.category(char)[0] in "LN"
        ).replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        result.add(f"{slug}-{count}" if count else slug)
    return result


def check(root: Path) -> int:
    files = [root / "README.md"]
    for name in ("docs", "skills"):
        files.extend(sorted((root / name).rglob("*.md")))
    failures = []
    checked = 0
    headings: dict[Path, set[str]] = {}
    for source in files:
        if not source.exists():
            continue
        text = prose(source.read_text())
        targets = re.findall(r'\]\(([^\s)]+)(?:\s+"[^"]*")?\)', text)
        targets.extend(re.findall(r'''<img\b[^>]*\bsrc=["']([^"']+)''', text))
        for target in targets:
            target = target.strip("<>")
            if re.match(r"[a-zA-Z][a-zA-Z\d+.-]*:", target) or target.startswith("//"):
                continue
            path, _, anchor = unquote(target).partition("#")
            destination = (source.parent / path).resolve() if path else source.resolve()
            checked += 1
            error = None
            if not destination.exists():
                error = "missing file"
            elif anchor and destination.suffix == ".md":
                if destination not in headings:
                    headings[destination] = anchors(destination)
                if anchor not in headings[destination]:
                    error = "missing heading"
            if error:
                failures.append(f"{source.relative_to(root)}: {target} ({error})")
    for failure in failures:
        print(failure)
    print(f"{checked} local links checked; {len(failures)} broken")
    return bool(failures)


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    raise SystemExit(check(root))
