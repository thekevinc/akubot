from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from .privacy import sanitize_example


@dataclass(frozen=True)
class ExportChannel:
    channel_id: str
    file: Path


def discover(root: Path) -> list[ExportChannel]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("Select the directory containing your unzipped Discord data package.")
    messages = next((item for item in root.iterdir() if item.is_dir() and item.name.lower() == "messages" and not item.is_symlink()), root)
    found = []
    for folder in sorted(messages.iterdir()):
        if folder.is_symlink() or not folder.is_dir():
            continue
        channel_id = folder.name.removeprefix("c")
        if not channel_id.isdecimal():
            continue
        for name in ("messages.json", "messages.csv"):
            path = folder / name
            if path.is_file() and not path.is_symlink() and root in path.resolve().parents:
                found.append(ExportChannel(channel_id, path))
                break
    if not found:
        raise ValueError("No messages/<channel ID>/messages.json or messages.csv files were found.")
    return found


def records(path: Path):
    if path.suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            yield from csv.DictReader(stream)
        return
    if path.stat().st_size > 50_000_000:
        raise ValueError("A selected JSON file exceeds 50 MB. Split it locally before importing.")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = data.get("messages", data.get("Messages"))
    if not isinstance(data, list):
        raise ValueError("Expected a list of messages in the official export.")
    yield from data


def prepare(root: Path, output: Path, channel_ids: set[str], owner_id: str) -> tuple[int, int]:
    if not channel_ids or not all(value.isdecimal() for value in channel_ids):
        raise ValueError("Select at least one numeric --channel-id explicitly.")
    if not owner_id.isdecimal():
        raise ValueError("Provide your numeric --owner-id to filter messages that include author metadata.")
    channels = discover(root)
    selected = [channel for channel in channels if channel.channel_id in channel_ids]
    if {channel.channel_id for channel in selected} != channel_ids:
        raise ValueError("One or more selected channel IDs are absent from the export.")
    # Never overwrite the source or a previous review file.
    if root.resolve() == output.resolve() or root.resolve() in output.resolve().parents:
        raise ValueError("Save review candidates outside the source export directory.")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    seen: set[str] = set()
    skipped = 0
    try:
        with output.open("x", encoding="utf-8") as stream:
            output.chmod(0o600)
            for channel in selected:
                for row in records(channel.file):
                    if not isinstance(row, dict):
                        skipped += 1
                        continue
                    author = row.get("author", row.get("Author"))
                    if isinstance(author, dict):
                        author = author.get("id", author.get("ID"))
                    author = row.get("author_id", row.get("AuthorID", author))
                    has_author = any(key in row for key in ("author", "Author", "author_id", "AuthorID"))
                    if has_author and (author is None or str(author) != owner_id):
                        skipped += 1
                        continue
                    text = row.get("Contents", row.get("contents", row.get("content", "")))
                    if not isinstance(text, str):
                        skipped += 1
                        continue
                    text = sanitize_example(text)
                    if not 2 <= len(text) <= 350 or text in seen or not any(char.isalpha() for char in text):
                        skipped += 1
                        continue
                    seen.add(text)
                    stream.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
    except Exception:
        # Do not leave a partially prepared file looking complete after an error.
        # Existing files are never removed when exclusive creation fails.
        if 'stream' in locals():
            output.unlink(missing_ok=True)
        raise
    return len(seen), skipped


def read_reviewed(path: Path) -> list[str]:
    if path.stat().st_size > 20_000_000:
        raise ValueError("The reviewed file exceeds 20 MB.")
    texts = []
    with path.open(encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                raise ValueError(f"Invalid JSON on line {line_number} of the reviewed file.") from None
            if not isinstance(row, dict) or set(row) != {"text"} or not isinstance(row["text"], str) or not 2 <= len(row["text"]) <= 350:
                raise ValueError(f"Line {line_number} must contain only a text field of 2 to 350 characters.")
            texts.append(sanitize_example(row["text"]))
    return texts

