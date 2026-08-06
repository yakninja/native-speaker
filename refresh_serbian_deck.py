#!/usr/bin/env python3
"""Refresh the Serbian Anki deck without modifying the source export.

The linguistic rewrite happens in review TSV batches. This script owns parsing,
merging, validation, TTS, Anki assembly, and verification so GUIDs and tags do
not need to be edited by hand.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Sequence


EXPECTED_RECORDS = 3658
# Gemini TTS rejected sustained 15 RPM during batch generation. Keep normal
# traffic at 12 RPM so transient retries have headroom inside the provider's
# rolling per-model quota rather than turning one retry into a burst of 429s.
DEFAULT_TTS_REQUESTS_PER_MINUTE = 12.0
# Retries are deliberately bounded: every attempt is paced and consumes the
# same provider quota as an initial request.
DEFAULT_TTS_RETRIES = 3
ANKI_HEADERS = (
    "#separator:tab",
    "#html:true",
    "#guid column:1",
    "#tags column:4",
)
REVIEW_FIELDS = (
    "index",
    "guid",
    "serbian_original",
    "russian_original",
    "serbian_final",
    "russian_final",
    "tags",
    "old_audio",
    "audio_file",
    "change_kind",
    "status",
    "note",
)
EDITABLE_REVIEW_FIELDS = (
    "serbian_final",
    "russian_final",
    "change_kind",
    "status",
    "note",
)
VALID_STATUSES = {"draft", "needs_review", "approved", "audio_done", "verified"}
READY_STATUSES = {"approved", "audio_done", "verified"}
LOCKED_STATUSES = {"approved", "audio_done", "verified"}
SOUND_RE = re.compile(r"\[sound:([^\]\r\n]+\.mp3)\]", re.IGNORECASE)
FINAL_FRONT_RE = re.compile(r"^(.*?)<br>\[sound:([^\]\r\n]+\.mp3)\]$", re.DOTALL)
CYRILLIC_RE = re.compile(r"[\u0400-\u052f]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
WORD_RE = re.compile(r"[^\W\d_]+(?:[-’'][^\W\d_]+)*", re.UNICODE)
SERBIAN_MASCULINE_FIRST_PERSON_RE = re.compile(
    r"(?i)(?:\b(?:nisam|sam)\s+[^.!?]{0,20}\b[a-zčćšžđ]+(?:ao|eo|io|uo)\b|"
    r"\b[a-zčćšžđ]+(?:ao|eo|io|uo)\s+sam\b|"
    r"\b(?:nisam|sam)\s+(?:zauzet|spreman|siguran|umoran|gladan|žedan)\b)"
)
RUSSIAN_MASCULINE_FIRST_PERSON_RE = re.compile(
    r"(?i)\bя\b[^,;.!?]{0,50}\b(?:решил|знал|хотел|получил|успел|занят|готов|был|"
    r"пришёл|ушёл|сделал|взял|увидел|сказал|купил|заказал|пошёл|поехал|рад)\b"
)
IJEKAVIAN_WORDS = {
    "bijelo",
    "bijela",
    "bijeli",
    "čovjek",
    "djeca",
    "dječak",
    "djevojka",
    "lijep",
    "lijepa",
    "lijepo",
    "mlijeko",
    "ovdje",
    "poslije",
    "prije",
    "rijeka",
    "svijet",
    "vrijeme",
}
SERBIAN_ASCII_TRANSLATION = str.maketrans(
    {
        "č": "c",
        "ć": "c",
        "š": "s",
        "ž": "z",
        "đ": "dj",
        "Č": "C",
        "Ć": "C",
        "Š": "S",
        "Ž": "Z",
        "Đ": "Dj",
    }
)


class DeckError(Exception):
    """A user-facing pipeline error."""


@dataclass(frozen=True)
class SourceNote:
    index: int
    guid: str
    front: str
    russian: str
    tags: str
    serbian_plain: str
    russian_plain: str
    old_audio: str


@dataclass(frozen=True)
class Issue:
    severity: str
    guid: str
    field: str
    message: str

    def render(self) -> str:
        location = ":".join(part for part in (self.guid, self.field) if part)
        return f"{self.severity.upper():7} {location or '-'}: {self.message}"


class _PlainTextParser(HTMLParser):
    BLOCK_TAGS = {"br", "div", "p", "li"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return normalize_spaces("".join(self.parts))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", normalize_text(value)).strip()


def html_to_plain(value: str) -> str:
    parser = _PlainTextParser()
    parser.feed(value)
    parser.close()
    return parser.text()


def plain_front_and_audio(front: str) -> tuple[str, str]:
    audio_names = SOUND_RE.findall(front)
    without_audio = SOUND_RE.sub("", front)
    return html_to_plain(without_audio), (audio_names[0] if audio_names else "")


def read_anki_export(path: Path, *, expected_records: int | None = EXPECTED_RECORDS) -> tuple[list[str], list[SourceNote]]:
    if not path.exists():
        raise DeckError(f"Anki source does not exist: {path}")

    headers: list[str] = []
    notes: list[SourceNote] = []
    data_started = False
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t", quotechar='"')
        for record_number, record in enumerate(reader, start=1):
            if not data_started and len(record) == 1 and record[0].startswith("#"):
                headers.append(record[0])
                continue
            data_started = True
            if len(record) != 4:
                raise DeckError(
                    f"source record {record_number} has {len(record)} fields; expected 4"
                )
            guid, front, russian, tags = record
            serbian_plain, old_audio = plain_front_and_audio(front)
            notes.append(
                SourceNote(
                    index=len(notes) + 1,
                    guid=guid,
                    front=front,
                    russian=russian,
                    tags=tags,
                    serbian_plain=serbian_plain,
                    russian_plain=html_to_plain(russian),
                    old_audio=old_audio,
                )
            )

    if tuple(headers) != ANKI_HEADERS:
        raise DeckError(
            "unexpected Anki headers; expected " + ", ".join(ANKI_HEADERS)
        )
    if expected_records is not None and len(notes) != expected_records:
        raise DeckError(
            f"source has {len(notes)} records; expected {expected_records}"
        )
    guids = [note.guid for note in notes]
    duplicates = [guid for guid, count in Counter(guids).items() if count > 1]
    if duplicates:
        raise DeckError(f"source contains duplicate GUIDs: {', '.join(duplicates[:10])}")
    return headers, notes


def read_review(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise DeckError(f"review table does not exist: {path}; run extract first")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t", quotechar='"')
        if reader.fieldnames != list(REVIEW_FIELDS):
            raise DeckError(
                f"unexpected review columns in {path}; expected {', '.join(REVIEW_FIELDS)}"
            )
        rows: list[dict[str, str]] = []
        for row_number, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise DeckError(
                    f"review row {row_number} does not have exactly {len(REVIEW_FIELDS)} fields"
                )
            rows.append({key: normalize_text(value) for key, value in row.items()})
        return rows


def atomic_write_review(path: Path, rows: Sequence[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as handle:
        temp_path = Path(handle.name)
        writer = csv.DictWriter(
            handle,
            fieldnames=REVIEW_FIELDS,
            delimiter="\t",
            quotechar='"',
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in REVIEW_FIELDS})
    temp_path.replace(path)


def atomic_write_anki(path: Path, headers: Sequence[str], records: Sequence[Sequence[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as handle:
        temp_path = Path(handle.name)
        for header in headers:
            handle.write(header + "\n")
        writer = csv.writer(
            handle,
            delimiter="\t",
            quotechar='"',
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )
        writer.writerows(records)
    temp_path.replace(path)


def source_row(note: SourceNote, existing: dict[str, str] | None = None) -> dict[str, str]:
    row = {field: "" for field in REVIEW_FIELDS}
    row.update(
        {
            "index": str(note.index),
            "guid": note.guid,
            "serbian_original": note.serbian_plain,
            "russian_original": note.russian_plain,
            "tags": note.tags,
            "old_audio": note.old_audio,
            "status": "draft",
        }
    )
    if existing:
        for field in (*EDITABLE_REVIEW_FIELDS, "audio_file"):
            row[field] = existing.get(field, "")
        if not row["status"]:
            row["status"] = "draft"
    return row


def command_extract(args: argparse.Namespace) -> int:
    source = Path(args.source)
    review = Path(args.review)
    _, notes = read_anki_export(source)
    existing_by_guid: dict[str, dict[str, str]] = {}
    if review.exists() and review.stat().st_size:
        existing_by_guid = {row["guid"]: row for row in read_review(review)}
        unknown = sorted(set(existing_by_guid) - {note.guid for note in notes})
        if unknown:
            raise DeckError(f"review table contains unknown GUIDs: {', '.join(unknown[:10])}")

    rows = [source_row(note, existing_by_guid.get(note.guid)) for note in notes]
    atomic_write_review(review, rows)
    preserved = sum(1 for row in rows if row["serbian_final"] or row["russian_final"])
    print(f"Extracted {len(rows)} notes to {review}")
    print(f"Preserved {preserved} previously edited rows")
    print(f"Source SHA-256: {sha256_file(source)}")
    return 0


def command_export_batch(args: argparse.Namespace) -> int:
    if not 1 <= args.limit <= 100:
        raise DeckError("batch limit must be between 1 and 100")
    rows = read_review(Path(args.review))
    wanted = set(args.status or ["draft"])
    selected = [row for row in rows if row["status"] in wanted]
    if args.start_index:
        selected = [row for row in selected if int(row["index"]) >= args.start_index]
    selected = selected[: args.limit]
    if not selected:
        raise DeckError("no review rows matched the requested batch")
    output = Path(args.output)
    atomic_write_review(output, selected)
    print(
        f"Exported {len(selected)} rows ({selected[0]['index']}–{selected[-1]['index']}) to {output}"
    )
    return 0


def command_merge_batch(args: argparse.Namespace) -> int:
    review_path = Path(args.review)
    master = read_review(review_path)
    batch = read_review(Path(args.batch))
    master_by_guid = {row["guid"]: row for row in master}
    seen: set[str] = set()
    changed = 0

    for incoming in batch:
        guid = incoming["guid"]
        if guid in seen:
            raise DeckError(f"batch contains duplicate GUID: {guid}")
        seen.add(guid)
        if guid not in master_by_guid:
            raise DeckError(f"batch contains unknown GUID: {guid}")
        current = master_by_guid[guid]
        for field in ("index", "serbian_original", "russian_original", "tags", "old_audio"):
            if incoming[field] != current[field]:
                raise DeckError(f"batch changed protected field {field} for GUID {guid}")
        proposed = {field: incoming[field] for field in EDITABLE_REVIEW_FIELDS}
        current_values = {field: current[field] for field in EDITABLE_REVIEW_FIELDS}
        if current["status"] in LOCKED_STATUSES and proposed != current_values and not args.allow_locked:
            raise DeckError(
                f"refusing to overwrite locked {current['status']} row {guid}; create a reviewed correction explicitly"
            )
        if proposed != current_values:
            for field, value in proposed.items():
                current[field] = value
            current["audio_file"] = ""
            changed += 1

    atomic_write_review(review_path, master)
    print(f"Merged {len(batch)} batch rows into {review_path}; {changed} changed")
    return 0


def count_words(text: str) -> int:
    return len(WORD_RE.findall(text))


def has_single_final_period(text: str) -> bool:
    return text.endswith(".") and not text.endswith("...")


def row_text_issues(row: dict[str, str], *, allow_incomplete: bool) -> list[Issue]:
    guid = row["guid"]
    issues: list[Issue] = []
    status = row["status"]
    if status not in VALID_STATUSES:
        issues.append(Issue("error", guid, "status", f"unknown status {status!r}"))
    serbian = row["serbian_final"]
    russian = row["russian_final"]
    if not serbian or not russian:
        if not allow_incomplete or status in READY_STATUSES:
            missing = " and ".join(
                name
                for name, value in (("serbian_final", serbian), ("russian_final", russian))
                if not value
            )
            issues.append(Issue("error", guid, missing, "required final text is blank"))
        return issues

    for field, value in (("serbian_final", serbian), ("russian_final", russian)):
        if value != normalize_text(value):
            issues.append(Issue("error", guid, field, "text is not Unicode NFC"))
        if "\t" in value or "\r" in value or "\n" in value:
            issues.append(Issue("error", guid, field, "contains a tab or raw newline"))
        if CONTROL_RE.search(value) or "\ufffd" in value:
            issues.append(Issue("error", guid, field, "contains a control/replacement character"))
        if "<" in value or ">" in value:
            issues.append(Issue("error", guid, field, "contains raw HTML-like markup"))
        if has_single_final_period(value) and "[period-ok]" not in row["note"]:
            issues.append(Issue("error", guid, field, "omit the sentence-final full stop"))

    if CYRILLIC_RE.search(serbian):
        issues.append(Issue("error", guid, "serbian_final", "contains Cyrillic; use Serbian Latin"))
    words = count_words(serbian)
    if words > 15 and "[length-ok]" not in row["note"]:
        issues.append(Issue("error", guid, "serbian_final", f"contains {words} words; maximum is 15"))
    elif words < 5 and "[length-ok]" not in row["note"]:
        issues.append(Issue("warning", guid, "serbian_final", f"contains only {words} words"))

    lowered_words = {word.casefold() for word in WORD_RE.findall(serbian)}
    dialect_hits = sorted(lowered_words & IJEKAVIAN_WORDS)
    if dialect_hits:
        issues.append(
            Issue(
                "warning",
                guid,
                "serbian_final",
                "possible Ijekavian forms: " + ", ".join(dialect_hits),
            )
        )
    if re.search(r"(?i)\bdj[a-zčćšžđ]*", serbian):
        issues.append(Issue("warning", guid, "serbian_final", "possible ASCII dj for đ"))
    if SERBIAN_MASCULINE_FIRST_PERSON_RE.search(serbian):
        issues.append(
            Issue(
                "warning",
                guid,
                "serbian_final",
                "possible masculine first-person form; the card speaker must be female",
            )
        )
    if RUSSIAN_MASCULINE_FIRST_PERSON_RE.search(russian):
        issues.append(
            Issue(
                "warning",
                guid,
                "russian_final",
                "possible masculine first-person form; the card speaker must be female",
            )
        )
    if status in READY_STATUSES and not row["change_kind"]:
        issues.append(Issue("error", guid, "change_kind", "approved row needs a change category"))
    return issues


def validate_review(
    source_notes: Sequence[SourceNote],
    review_rows: Sequence[dict[str, str]],
    *,
    allow_incomplete: bool,
) -> list[Issue]:
    issues: list[Issue] = []
    source_guids = [note.guid for note in source_notes]
    review_guids = [row["guid"] for row in review_rows]
    if len(review_rows) != len(source_notes):
        issues.append(
            Issue(
                "error",
                "",
                "guid",
                f"review has {len(review_rows)} rows; source has {len(source_notes)}",
            )
        )
    duplicates = [guid for guid, count in Counter(review_guids).items() if count > 1]
    if duplicates:
        issues.append(Issue("error", "", "guid", "duplicate GUIDs: " + ", ".join(duplicates[:10])))
    if review_guids != source_guids:
        issues.append(Issue("error", "", "guid", "GUID set or order differs from source"))

    source_by_guid = {note.guid: note for note in source_notes}
    incomplete = 0
    for position, row in enumerate(review_rows, start=1):
        guid = row["guid"]
        source = source_by_guid.get(guid)
        if source:
            if row["index"] != str(position) or row["index"] != str(source.index):
                issues.append(Issue("error", guid, "index", "source order/index changed"))
            if row["tags"] != source.tags:
                issues.append(Issue("error", guid, "tags", "tags differ from source"))
            if row["serbian_original"] != source.serbian_plain:
                issues.append(Issue("error", guid, "serbian_original", "protected source text changed"))
            if row["russian_original"] != source.russian_plain:
                issues.append(Issue("error", guid, "russian_original", "protected source text changed"))
            if row["old_audio"] != source.old_audio:
                issues.append(Issue("error", guid, "old_audio", "protected source audio changed"))
        if not row["serbian_final"] or not row["russian_final"]:
            incomplete += 1
        issues.extend(row_text_issues(row, allow_incomplete=allow_incomplete))
    if allow_incomplete and incomplete:
        issues.append(Issue("warning", "", "review", f"{incomplete} rows are still incomplete"))
    return issues


def print_issues(issues: Sequence[Issue], max_issues: int) -> tuple[int, int]:
    errors = sum(issue.severity == "error" for issue in issues)
    warnings = sum(issue.severity == "warning" for issue in issues)
    for issue in issues[:max_issues]:
        print(issue.render())
    if len(issues) > max_issues:
        print(f"... {len(issues) - max_issues} more issues omitted")
    print(f"Validation: {errors} errors, {warnings} warnings")
    return errors, warnings


def command_validate(args: argparse.Namespace) -> int:
    _, notes = read_anki_export(Path(args.source))
    rows = read_review(Path(args.review))
    issues = validate_review(notes, rows, allow_incomplete=args.allow_incomplete)
    errors, _ = print_issues(issues, args.max_issues)
    return 1 if errors else 0


def readable_audio_slug(serbian: str, max_length: int = 72) -> str:
    transliterated = normalize_text(serbian).translate(SERBIAN_ASCII_TRANSLATION)
    ascii_text = unicodedata.normalize("NFKD", transliterated).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_text.casefold()).strip("_")
    return slug[:max_length].rstrip("_") or "serbian_audio"


def audio_filename(guid: str, serbian: str) -> str:
    slug = readable_audio_slug(serbian)
    guid_hash = sha256_bytes(guid.encode("utf-8"))[:8]
    text_hash = sha256_bytes(normalize_text(serbian).encode("utf-8"))[:8]
    return f"sr_{slug}_{guid_hash}_{text_hash}.mp3"


def rendered_tts_input(serbian: str, append_period: bool) -> str:
    text = normalize_spaces(serbian)
    if append_period and text and text[-1] not in ".?!…":
        return text + "."
    return text


def load_manifest(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise DeckError(f"invalid manifest JSON on line {line_number}: {error}") from error
            if not isinstance(value, dict):
                raise DeckError(f"manifest line {line_number} is not an object")
            records.append(value)
    return records


def append_manifest(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def append_audio_review_event(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def validate_mp3(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        raise DeckError(f"missing or empty MP3: {path}")
    try:
        from pydub import AudioSegment

        AudioSegment.from_file(path, format="mp3")
    except Exception as error:
        raise DeckError(f"MP3 does not decode: {path}: {error}") from error


def latest_manifest_by_guid(records: Sequence[dict[str, object]]) -> dict[str, dict[str, object]]:
    latest: dict[str, dict[str, object]] = {}
    for record in records:
        guid = record.get("guid")
        if isinstance(guid, str):
            latest[guid] = record
    return latest


def tts_config(args: argparse.Namespace) -> dict[str, object]:
    return {
        "provider": "google-cloud-text-to-speech",
        "model": args.model,
        "voice": args.voice,
        "language_code": args.language_code,
        "speaking_rate": args.speaking_rate,
        "append_period": args.append_period,
        "audio_encoding": "MP3",
    }


def load_tts_overrides(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise DeckError(f"invalid TTS override JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise DeckError(f"TTS overrides must be a JSON object: {path}")
    overrides: dict[str, dict[str, str]] = {}
    for guid, override in value.items():
        if not isinstance(guid, str) or not isinstance(override, dict):
            raise DeckError(f"invalid TTS override entry in {path}")
        if not all(isinstance(key, str) and isinstance(item, str) for key, item in override.items()):
            raise DeckError(f"TTS override values must be strings for GUID {guid}")
        prompt = normalize_text(override.get("prompt", ""))
        if not prompt:
            raise DeckError(f"TTS override for GUID {guid} has no prompt")
        overrides[guid] = {key: normalize_text(item) for key, item in override.items()}
    return overrides


def effective_tts_config(
    base_config: dict[str, object], override: dict[str, str] | None
) -> dict[str, object]:
    config = dict(base_config)
    if override:
        config["prompt"] = override["prompt"]
    return config


def choose_pilot_rows(rows: Sequence[dict[str, str]], limit: int) -> list[dict[str, str]]:
    ready = [row for row in rows if row["status"] in READY_STATUSES]
    if len(ready) <= limit:
        return ready
    selected: list[dict[str, str]] = []
    selected_guids: set[str] = set()
    features = ("č", "ć", "š", "ž", "đ", "?", "!", "0")
    for feature in features:
        for row in ready:
            if row["guid"] not in selected_guids and (
                feature in row["serbian_final"]
                or (feature == "0" and re.search(r"\d", row["serbian_final"]))
            ):
                selected.append(row)
                selected_guids.add(row["guid"])
                break
    remaining = [row for row in ready if row["guid"] not in selected_guids]
    while remaining and len(selected) < limit:
        slot = min(len(remaining) - 1, len(remaining) // max(1, limit - len(selected)))
        row = remaining.pop(slot)
        selected.append(row)
        selected_guids.add(row["guid"])
    return sorted(selected[:limit], key=lambda row: int(row["index"]))


def load_google_credentials(client_secret_path: Path, token_path: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    credentials = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), scopes)
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    elif not credentials or not credentials.valid:
        if not client_secret_path.exists():
            raise DeckError(f"missing Google OAuth client secret: {client_secret_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), scopes)
        credentials = flow.run_local_server(port=0)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def build_google_synthesizer(args: argparse.Namespace):
    from google.cloud import texttospeech

    credentials = load_google_credentials(Path(args.client_secret), Path(args.token))
    client = texttospeech.TextToSpeechClient(credentials=credentials)
    voice_values: dict[str, object] = {
        "language_code": args.language_code,
        "name": args.voice,
    }
    if args.model:
        voice_values["model_name"] = args.model
    voice = texttospeech.VoiceSelectionParams(**voice_values)
    config_values: dict[str, object] = {
        "audio_encoding": texttospeech.AudioEncoding.MP3,
    }
    if args.speaking_rate is not None:
        config_values["speaking_rate"] = args.speaking_rate
    audio_config = texttospeech.AudioConfig(**config_values)

    def synthesize(text: str, prompt: str, output_path: Path) -> None:
        input_values = {"text": text}
        if prompt:
            input_values["prompt"] = prompt
        response = client.synthesize_speech(
            input=texttospeech.SynthesisInput(**input_values),
            voice=voice,
            audio_config=audio_config,
        )
        output_path.write_bytes(response.audio_content)

    return synthesize


def is_transient_tts_error(error: Exception) -> bool:
    try:
        from google.api_core import exceptions as google_exceptions

        transient_types = (
            google_exceptions.DeadlineExceeded,
            google_exceptions.InternalServerError,
            google_exceptions.ServiceUnavailable,
            google_exceptions.TooManyRequests,
        )
        return isinstance(error, transient_types)
    except ImportError:
        return False


def run_synthesis(args: argparse.Namespace, *, pilot: bool) -> int:
    if args.requests_per_minute <= 0:
        raise DeckError("--requests-per-minute must be greater than zero")
    if args.start_index is not None and args.start_index < 1:
        raise DeckError("--start-index must be at least 1")
    if (
        args.start_index is not None
        and args.end_index is not None
        and args.end_index < args.start_index
    ):
        raise DeckError("--end-index must not precede --start-index")
    source_path = Path(args.source)
    review_path = Path(args.review)
    media_dir = Path(args.media_dir)
    manifest_path = Path(args.manifest)
    tts_overrides = load_tts_overrides(Path(args.tts_overrides))
    _, notes = read_anki_export(source_path)
    rows = read_review(review_path)
    issues = validate_review(notes, rows, allow_incomplete=True)
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        print_issues(errors, args.max_issues)
        return 1

    candidates = [row for row in rows if row["status"] in READY_STATUSES]
    if args.start_index is not None:
        candidates = [row for row in candidates if int(row["index"]) >= args.start_index]
    if args.end_index is not None:
        candidates = [row for row in candidates if int(row["index"]) <= args.end_index]
    if args.guid:
        wanted = set(args.guid)
        candidates = [row for row in candidates if row["guid"] in wanted]
        missing = wanted - {row["guid"] for row in candidates}
        if missing:
            raise DeckError("requested GUIDs are not approved: " + ", ".join(sorted(missing)))
    if pilot:
        candidates = choose_pilot_rows(candidates, args.limit)
    elif args.limit is not None:
        candidates = candidates[: args.limit]
    if not candidates:
        raise DeckError("no approved rows are available for synthesis")

    base_config = tts_config(args)
    total_characters = sum(len(row["serbian_final"]) for row in candidates)
    print(
        f"Synthesis plan: {len(candidates)} requests, {total_characters} Serbian characters, "
        f"voice={args.voice}, model={args.model or 'provider default'}, language={args.language_code}, "
        f"rate={args.speaking_rate}, max_requests_per_minute={args.requests_per_minute}"
    )
    if args.dry_run:
        for row in candidates:
            override_marker = "\tpronunciation-override" if row["guid"] in tts_overrides else ""
            print(
                f"{row['guid']}\t{audio_filename(row['guid'], row['serbian_final'])}"
                f"\t{row['serbian_final']}{override_marker}"
            )
        return 0
    if not args.yes:
        raise DeckError("refusing paid TTS calls without --yes; run --dry-run first")

    media_dir.mkdir(parents=True, exist_ok=True)
    synthesize = build_google_synthesizer(args)
    manifest_records = load_manifest(manifest_path)
    latest = latest_manifest_by_guid(manifest_records)
    generated = cached = failed = 0
    by_guid = {row["guid"]: row for row in rows}
    minimum_request_interval = 60.0 / args.requests_per_minute
    last_request_started: float | None = None

    def wait_for_request_slot() -> None:
        nonlocal last_request_started
        if last_request_started is not None:
            remaining = minimum_request_interval - (time.monotonic() - last_request_started)
            if remaining > 0:
                time.sleep(remaining)
        last_request_started = time.monotonic()

    for candidate in candidates:
        guid = candidate["guid"]
        serbian = candidate["serbian_final"]
        override = tts_overrides.get(guid)
        prompt = override["prompt"] if override else ""
        config = effective_tts_config(base_config, override)
        config_hash = sha256_bytes(
            json.dumps(config, sort_keys=True, ensure_ascii=False).encode("utf-8")
        )
        tts_input = rendered_tts_input(serbian, args.append_period)
        filename = audio_filename(guid, serbian)
        output_path = media_dir / filename
        text_hash = sha256_bytes(normalize_text(serbian).encode("utf-8"))
        previous = latest.get(guid, {})
        cache_matches = (
            previous.get("status") in {"ok", "cached"}
            and previous.get("text_sha256") == text_hash
            and previous.get("config_sha256") == config_hash
            and previous.get("output_filename") == filename
        )
        if cache_matches:
            try:
                validate_mp3(output_path)
            except DeckError:
                cache_matches = False
        if cache_matches:
            cached += 1
            by_guid[guid]["audio_file"] = filename
            if by_guid[guid]["status"] == "approved":
                by_guid[guid]["status"] = "audio_done"
            cache_record: dict[str, object] = {
                "timestamp": utc_now(),
                "guid": guid,
                "source_sha256": sha256_file(source_path),
                "text_sha256": text_hash,
                "serbian_text": serbian,
                "tts_input": tts_input,
                "config": config,
                "config_sha256": config_hash,
                "tts_override": override,
                "output_filename": filename,
                "status": "cached",
                "audio_sha256": sha256_file(output_path),
                "bytes": output_path.stat().st_size,
            }
            append_manifest(manifest_path, cache_record)
            latest[guid] = cache_record
            print(f"cached    {guid} {filename}")
            continue

        temp_path = media_dir / (filename + ".tmp")
        if temp_path.exists():
            temp_path.unlink()
        record: dict[str, object] = {
            "timestamp": utc_now(),
            "guid": guid,
            "source_sha256": sha256_file(source_path),
            "text_sha256": text_hash,
            "serbian_text": serbian,
            "tts_input": tts_input,
            "config": config,
            "config_sha256": config_hash,
            "tts_override": override,
            "output_filename": filename,
        }
        try:
            attempts = 0
            while True:
                attempts += 1
                try:
                    wait_for_request_slot()
                    synthesize(tts_input, prompt, temp_path)
                    break
                except Exception as error:
                    if attempts > args.retries or not is_transient_tts_error(error):
                        raise
                    delay = min(2 ** (attempts - 1), 8)
                    print(f"retrying  {guid} in {delay}s after transient TTS error", file=sys.stderr)
                    time.sleep(delay)
            validate_mp3(temp_path)
            temp_path.replace(output_path)
            record.update(
                {
                    "status": "ok",
                    "attempts": attempts,
                    "audio_sha256": sha256_file(output_path),
                    "bytes": output_path.stat().st_size,
                }
            )
            by_guid[guid]["audio_file"] = filename
            if by_guid[guid]["status"] == "approved":
                by_guid[guid]["status"] = "audio_done"
            generated += 1
            print(f"generated {guid} {filename}")
            if args.play:
                from pydub import AudioSegment
                from pydub.playback import play

                play(AudioSegment.from_file(output_path, format="mp3"))
        except KeyboardInterrupt:
            if temp_path.exists():
                temp_path.unlink()
            atomic_write_review(review_path, rows)
            raise
        except Exception as error:
            failed += 1
            record.update({"status": "error", "error": str(error), "attempts": attempts})
            if temp_path.exists():
                temp_path.unlink()
            print(f"failed    {guid}: {error}", file=sys.stderr)
        append_manifest(manifest_path, record)
        latest[guid] = record
        atomic_write_review(review_path, rows)

    atomic_write_review(review_path, rows)
    print(f"Synthesis result: {generated} generated, {cached} cached, {failed} failed")
    return 1 if failed else 0


def command_pilot(args: argparse.Namespace) -> int:
    if not 1 <= args.limit <= 30:
        raise DeckError("pilot limit must be between 1 and 30")
    return run_synthesis(args, pilot=True)


def command_synthesize(args: argparse.Namespace) -> int:
    return run_synthesis(args, pilot=False)


def select_audio_review_rows(
    rows: Sequence[dict[str, str]],
    *,
    start_index: int,
    end_index: int | None,
    limit: int | None,
    include_verified: bool,
) -> list[dict[str, str]]:
    statuses = {"audio_done"}
    if include_verified:
        statuses.add("verified")
    selected = [
        row
        for row in rows
        if row["status"] in statuses
        and int(row["index"]) >= start_index
        and (end_index is None or int(row["index"]) <= end_index)
    ]
    return selected if limit is None else selected[:limit]


def append_review_note(existing: str, comment: str) -> str:
    entry = f"Audio review: {normalize_spaces(comment)}"
    return f"{existing} | {entry}" if existing else entry


def apply_audio_review_decision(
    row: dict[str, str], action: str, comment: str = ""
) -> None:
    if action == "accept":
        row["status"] = "verified"
        return
    if action == "change_request":
        comment = normalize_spaces(comment)
        if not comment:
            raise DeckError("a change request cannot be empty")
        row["note"] = append_review_note(row["note"], comment)
        row["status"] = "needs_review"
        return
    raise DeckError(f"unknown audio review action: {action}")


def play_audio_file(path: Path) -> None:
    player = shutil.which("afplay")
    command = [player, str(path)] if player else None
    if command is None:
        player = shutil.which("ffplay")
        if player:
            command = [player, "-nodisp", "-autoexit", "-loglevel", "error", str(path)]
    if command is None:
        raise DeckError("no audio player found; install ffplay or run this command on macOS with afplay")
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as error:
        raise DeckError(f"audio player failed for {path}") from error


def print_audio_review_card(
    row: dict[str, str], position: int, total: int, audio_path: Path
) -> None:
    print(f"\n[{position}/{total}] source #{row['index']} | {row['guid']}")
    print(f"Original Serbian: {row['serbian_original']}")
    print(f"Original Russian: {row['russian_original']}")
    print(f"New Serbian:      {row['serbian_final']}")
    print(f"New Russian:      {row['russian_final']}")
    print(f"Audio:   {audio_path.name}", flush=True)


def read_audio_review_key(prompt: str) -> str:
    if not sys.stdin.isatty():
        try:
            return input(prompt).strip().lower()
        except EOFError:
            return "x"

    import termios
    import tty

    descriptor = sys.stdin.fileno()
    previous_settings = termios.tcgetattr(descriptor)
    print(prompt, end="", flush=True)
    try:
        tty.setcbreak(descriptor)
        key = sys.stdin.read(1)
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous_settings)
    if key == "\x03":
        raise KeyboardInterrupt
    choice = "" if key in {"\r", "\n"} else key.lower()
    if choice in {"q", "x"}:
        print(choice, end="", flush=True)
        try:
            suffix = input().strip().lower()
        except EOFError:
            return "x"
        return choice if not suffix else choice + suffix
    print(choice)
    return choice


def save_audio_review_session(
    review_path: Path,
    rows: Sequence[dict[str, str]],
    event_path: Path,
    events: Sequence[dict[str, object]],
) -> None:
    atomic_write_review(review_path, rows)
    for event in events:
        append_audio_review_event(event_path, event)


def command_review_audio(args: argparse.Namespace) -> int:
    if args.start_index < 1:
        raise DeckError("start index must be at least 1")
    if args.end_index is not None and args.end_index < args.start_index:
        raise DeckError("end index must not precede start index")
    if args.limit is not None and args.limit < 1:
        raise DeckError("limit must be at least 1")

    review_path = Path(args.review)
    media_dir = Path(args.media_dir)
    event_path = Path(args.event_log)
    rows = read_review(review_path)
    selected = select_audio_review_rows(
        rows,
        start_index=args.start_index,
        end_index=args.end_index,
        limit=args.limit,
        include_verified=args.include_verified,
    )
    if not selected:
        print("No audio-complete cards match this review selection")
        return 0

    for row in selected:
        if not row["audio_file"]:
            raise DeckError(f"row {row['guid']} has no audio filename")
        validate_mp3(media_dir / row["audio_file"])

    if args.list_only:
        for position, row in enumerate(selected, start=1):
            print_audio_review_card(
                row, position, len(selected), media_dir / row["audio_file"]
            )
        print(f"\nListed {len(selected)} cards; no review state changed")
        return 0

    accepted = 0
    change_requests = 0
    skipped = 0
    pending_events: list[dict[str, object]] = []
    for position, row in enumerate(selected, start=1):
        audio_path = media_dir / row["audio_file"]
        print_audio_review_card(row, position, len(selected), audio_path)
        if not args.no_play:
            play_audio_file(audio_path)

        while True:
            choice = read_audio_review_key(
                "[Enter/a] accept  [r] replay  [c] request change  "
                "[s] skip  [q+Enter] save & quit  [x+Enter] discard & quit: "
            )

            if choice in {"", "a", "accept"}:
                previous_status = row["status"]
                apply_audio_review_decision(row, "accept")
                pending_events.append(
                    {
                        "timestamp": utc_now(),
                        "action": "accept",
                        "guid": row["guid"],
                        "index": int(row["index"]),
                        "previous_status": previous_status,
                        "status": row["status"],
                        "serbian": row["serbian_final"],
                        "russian": row["russian_final"],
                        "audio_file": row["audio_file"],
                    },
                )
                accepted += 1
                break
            if choice in {"r", "replay"}:
                if args.no_play:
                    print("Playback is disabled by --no-play")
                else:
                    play_audio_file(audio_path)
                continue
            if choice in {"c", "change"}:
                try:
                    comment = input("Change request: ").strip()
                except EOFError:
                    comment = ""
                if not comment:
                    print("Change request was empty; card remains open")
                    continue
                previous_status = row["status"]
                apply_audio_review_decision(row, "change_request", comment)
                pending_events.append(
                    {
                        "timestamp": utc_now(),
                        "action": "change_request",
                        "comment": normalize_spaces(comment),
                        "guid": row["guid"],
                        "index": int(row["index"]),
                        "previous_status": previous_status,
                        "status": row["status"],
                        "serbian": row["serbian_final"],
                        "russian": row["russian_final"],
                        "audio_file": row["audio_file"],
                    },
                )
                change_requests += 1
                break
            if choice in {"s", "skip"}:
                skipped += 1
                break
            if choice in {"q", "quit"}:
                save_audio_review_session(review_path, rows, event_path, pending_events)
                print(
                    f"Review saved: {accepted} accepted, "
                    f"{change_requests} change requests, {skipped} skipped"
                )
                return 0
            if choice in {"x", "exit"}:
                print(
                    f"Review discarded: {accepted} acceptances and "
                    f"{change_requests} change requests were not saved"
                )
                return 0
            print("Choose Enter/a, r, c, s, q+Enter, or x+Enter")

    save_audio_review_session(review_path, rows, event_path, pending_events)
    print(
        f"Review complete and saved: {accepted} accepted, "
        f"{change_requests} change requests, {skipped} skipped"
    )
    return 0


def escaped_card_text(text: str) -> str:
    return html.escape(normalize_text(text), quote=False)


def command_assemble(args: argparse.Namespace) -> int:
    headers, notes = read_anki_export(Path(args.source))
    rows = read_review(Path(args.review))
    issues = validate_review(notes, rows, allow_incomplete=args.allow_partial)
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        print_issues(errors, args.max_issues)
        return 1

    media_dir = Path(args.media_dir)
    records: list[list[str]] = []
    skipped = 0
    for row in rows:
        ready = (
            row["status"] in {"audio_done", "verified"}
            and row["serbian_final"]
            and row["russian_final"]
            and row["audio_file"]
        )
        if not ready:
            if args.allow_partial:
                skipped += 1
                continue
            raise DeckError(f"row {row['guid']} is not ready for assembly")
        audio_path = media_dir / row["audio_file"]
        validate_mp3(audio_path)
        front = f"{escaped_card_text(row['serbian_final'])}<br>[sound:{row['audio_file']}]"
        records.append(
            [row["guid"], front, escaped_card_text(row["russian_final"]), row["tags"]]
        )

    if not records:
        raise DeckError("no audio-complete records are available for assembly")
    atomic_write_anki(Path(args.output), headers, records)
    print(f"Assembled {len(records)} notes to {args.output}; skipped {skipped}")
    return 0


def read_output_records(path: Path) -> tuple[list[str], list[list[str]]]:
    if not path.exists():
        raise DeckError(f"assembled Anki file does not exist: {path}")
    headers: list[str] = []
    records: list[list[str]] = []
    data_started = False
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t", quotechar='"')
        for number, record in enumerate(reader, start=1):
            if not data_started and len(record) == 1 and record[0].startswith("#"):
                headers.append(record[0])
                continue
            data_started = True
            if len(record) != 4:
                raise DeckError(f"assembled record {number} has {len(record)} fields; expected 4")
            records.append(record)
    return headers, records


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temp_path = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temp_path.replace(path)


def command_verify(args: argparse.Namespace) -> int:
    source_path = Path(args.source)
    output_path = Path(args.output)
    source_headers, source_notes = read_anki_export(source_path)
    output_headers, output_rows = read_output_records(output_path)
    review_rows = read_review(Path(args.review))
    review_by_guid = {row["guid"]: row for row in review_rows}
    media_dir = Path(args.media_dir)
    issues: list[Issue] = []

    if output_headers != source_headers:
        issues.append(Issue("error", "", "headers", "output headers differ from source"))
    if not args.allow_partial and len(output_rows) != len(source_notes):
        issues.append(
            Issue(
                "error",
                "",
                "records",
                f"output has {len(output_rows)} records; expected {len(source_notes)}",
            )
        )
    source_by_guid = {note.guid: note for note in source_notes}
    output_guids = [record[0] for record in output_rows]
    expected_guids = [note.guid for note in source_notes]
    if args.allow_partial:
        positions = [expected_guids.index(guid) for guid in output_guids if guid in source_by_guid]
        if positions != sorted(positions):
            issues.append(Issue("error", "", "guid", "partial output is not in source order"))
    elif output_guids != expected_guids:
        issues.append(Issue("error", "", "guid", "output GUID set or order differs from source"))
    if len(output_guids) != len(set(output_guids)):
        issues.append(Issue("error", "", "guid", "output contains duplicate GUIDs"))

    audio_files: set[str] = set()
    for guid, front, russian, tags in output_rows:
        source = source_by_guid.get(guid)
        review = review_by_guid.get(guid)
        if not source or not review:
            issues.append(Issue("error", guid, "guid", "unknown output GUID"))
            continue
        if tags != source.tags:
            issues.append(Issue("error", guid, "tags", "output tags differ from source"))
        match = FINAL_FRONT_RE.fullmatch(front)
        if not match:
            issues.append(Issue("error", guid, "front", "front does not have exact text/audio shape"))
            continue
        serbian_html, filename = match.groups()
        if html.unescape(serbian_html) != review["serbian_final"]:
            issues.append(Issue("error", guid, "front", "Serbian text differs from review"))
        if html.unescape(russian) != review["russian_final"]:
            issues.append(Issue("error", guid, "back", "Russian text differs from review"))
        if filename != review["audio_file"]:
            issues.append(Issue("error", guid, "audio", "audio filename differs from review"))
        if filename in audio_files:
            issues.append(Issue("error", guid, "audio", "audio filename is duplicated"))
        audio_files.add(filename)
        try:
            validate_mp3(media_dir / filename)
        except DeckError as error:
            issues.append(Issue("error", guid, "audio", str(error)))

    manifest_path = Path(args.manifest)
    manifest_records = load_manifest(manifest_path)
    latest_manifest = latest_manifest_by_guid(manifest_records)
    if output_rows and not manifest_path.exists():
        issues.append(Issue("error", "", "manifest", "audio manifest is missing"))
    for guid, _, _, _ in output_rows:
        review = review_by_guid.get(guid)
        if not review:
            continue
        manifest = latest_manifest.get(guid)
        if not manifest:
            issues.append(Issue("error", guid, "manifest", "no audio manifest record"))
            continue
        if manifest.get("status") not in {"ok", "cached"}:
            issues.append(Issue("error", guid, "manifest", "latest audio manifest record failed"))
        if manifest.get("output_filename") != review["audio_file"]:
            issues.append(Issue("error", guid, "manifest", "manifest filename differs from review"))
        expected_text_hash = sha256_bytes(normalize_text(review["serbian_final"]).encode("utf-8"))
        if manifest.get("text_sha256") != expected_text_hash:
            issues.append(Issue("error", guid, "manifest", "manifest text hash differs from review"))
        audio_path = media_dir / review["audio_file"]
        if audio_path.exists() and manifest.get("audio_sha256") != sha256_file(audio_path):
            issues.append(Issue("error", guid, "manifest", "manifest audio hash differs from MP3"))
    manifest_ok = sum(record.get("status") == "ok" for record in manifest_records)
    manifest_cached = sum(record.get("status") == "cached" for record in manifest_records)
    manifest_failed = sum(record.get("status") == "error" for record in manifest_records)
    status_counts = Counter(row["status"] for row in review_rows)
    change_counts: Counter[str] = Counter()
    for row in review_rows:
        for kind in filter(None, (part.strip() for part in row["change_kind"].split(","))):
            change_counts[kind] += 1

    errors, warnings = print_issues(issues, args.max_issues)
    report = {
        "timestamp": utc_now(),
        "result": "failed" if errors else "ok",
        "errors": errors,
        "warnings": warnings,
        "source_records": len(source_notes),
        "output_records": len(output_rows),
        "source_unique_guids": len({note.guid for note in source_notes}),
        "output_unique_guids": len(set(output_guids)),
        "tags_exact": all(
            guid in source_by_guid and tags == source_by_guid[guid].tags
            for guid, _, _, tags in output_rows
        ),
        "status_counts": dict(sorted(status_counts.items())),
        "change_counts": dict(sorted(change_counts.items())),
        "audio_expected": len(output_rows),
        "audio_unique": len(audio_files),
        "audio_present": sum((media_dir / filename).exists() for filename in audio_files),
        "audio_missing": sum(not (media_dir / filename).exists() for filename in audio_files),
        "manifest_ok_events": manifest_ok,
        "manifest_cached_events": manifest_cached,
        "manifest_failed_events": manifest_failed,
        "source_sha256": sha256_file(source_path),
        "output_sha256": sha256_file(output_path),
        "manifest_sha256": sha256_file(manifest_path) if manifest_path.exists() else None,
        "preview": [
            {"guid": row[0], "front": row[1], "back": row[2], "tags": row[3]}
            for row in (output_rows[:2] + output_rows[-2:] if output_rows else [])
        ],
    }
    write_json(Path(args.report), report)
    print(f"Verification report: {args.report}")
    return 1 if errors else 0


def add_common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", default="srpski.txt")
    parser.add_argument("--review", default="generated/serbian_review.tsv")


def add_synthesis_arguments(parser: argparse.ArgumentParser, *, pilot: bool) -> None:
    add_common_paths(parser)
    parser.add_argument("--media-dir", default="generated/media")
    parser.add_argument("--manifest", default="generated/manifest.jsonl")
    parser.add_argument("--tts-overrides", default="serbian_tts_overrides.json")
    parser.add_argument("--model", default="gemini-2.5-flash-tts")
    parser.add_argument("--voice", default="Leda")
    parser.add_argument("--language-code", default="sr-RS")
    parser.add_argument("--speaking-rate", type=float, default=1.0)
    parser.add_argument("--append-period", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--guid", action="append", default=[])
    parser.add_argument("--start-index", type=int)
    parser.add_argument("--end-index", type=int)
    parser.add_argument("--limit", type=int, default=25 if pilot else None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="authorize the planned paid TTS requests")
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_TTS_RETRIES,
        help="transient retries per clip; every attempt is rate-limited",
    )
    parser.add_argument(
        "--requests-per-minute",
        type=float,
        default=DEFAULT_TTS_REQUESTS_PER_MINUTE,
        help="pace all paid TTS attempts with headroom below provider quota",
    )
    parser.add_argument("--play", action="store_true")
    parser.add_argument("--client-secret", default="client_secret.json")
    parser.add_argument("--token", default="token.json")
    parser.add_argument("--max-issues", type=int, default=100)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract", help="extract immutable Anki source into the review table")
    add_common_paths(extract)
    extract.set_defaults(func=command_extract)

    export_batch = subparsers.add_parser("export-batch", help="export at most 100 review rows for editing")
    export_batch.add_argument("--review", default="generated/serbian_review.tsv")
    export_batch.add_argument("--output", required=True)
    export_batch.add_argument("--status", action="append")
    export_batch.add_argument("--start-index", type=int)
    export_batch.add_argument("--limit", type=int, default=100)
    export_batch.set_defaults(func=command_export_batch)

    merge_batch = subparsers.add_parser("merge-batch", help="merge reviewed language fields into the master table")
    merge_batch.add_argument("--review", default="generated/serbian_review.tsv")
    merge_batch.add_argument("--batch", required=True)
    merge_batch.add_argument("--allow-locked", action="store_true")
    merge_batch.set_defaults(func=command_merge_batch)

    validate = subparsers.add_parser("validate", help="validate source preservation and reviewed text")
    add_common_paths(validate)
    validate.add_argument("--allow-incomplete", action="store_true")
    validate.add_argument("--max-issues", type=int, default=100)
    validate.set_defaults(func=command_validate)

    pilot = subparsers.add_parser("pilot", help="synthesize a representative pilot of at most 30 approved rows")
    add_synthesis_arguments(pilot, pilot=True)
    pilot.set_defaults(func=command_pilot)

    synthesize = subparsers.add_parser("synthesize", help="synthesize approved Serbian rows")
    add_synthesis_arguments(synthesize, pilot=False)
    synthesize.set_defaults(func=command_synthesize)

    review_audio = subparsers.add_parser(
        "review-audio", help="play synthesized cards sequentially and record review decisions"
    )
    review_audio.add_argument("--review", default="generated/serbian_review.tsv")
    review_audio.add_argument("--media-dir", default="generated/media")
    review_audio.add_argument("--event-log", default="generated/audio-review.jsonl")
    review_audio.add_argument("--start-index", type=int, default=1)
    review_audio.add_argument("--end-index", type=int)
    review_audio.add_argument("--limit", type=int)
    review_audio.add_argument("--include-verified", action="store_true")
    review_audio.add_argument(
        "--no-play", action="store_true", help="show cards and prompts without playing audio"
    )
    review_audio.add_argument(
        "--list-only", action="store_true", help="show the review queue without playing or prompting"
    )
    review_audio.set_defaults(func=command_review_audio)

    assemble = subparsers.add_parser("assemble", help="assemble reviewed text and audio references into Anki TSV")
    add_common_paths(assemble)
    assemble.add_argument("--media-dir", default="generated/media")
    assemble.add_argument("--output", default="generated/srpski-updated.txt")
    assemble.add_argument("--allow-partial", action="store_true")
    assemble.add_argument("--max-issues", type=int, default=100)
    assemble.set_defaults(func=command_assemble)

    verify = subparsers.add_parser("verify", help="verify assembled TSV, media, GUIDs, tags, and checksums")
    add_common_paths(verify)
    verify.add_argument("--media-dir", default="generated/media")
    verify.add_argument("--manifest", default="generated/manifest.jsonl")
    verify.add_argument("--output", default="generated/srpski-updated.txt")
    verify.add_argument("--report", default="generated/verification.json")
    verify.add_argument("--allow-partial", action="store_true")
    verify.add_argument("--max-issues", type=int, default=100)
    verify.set_defaults(func=command_verify)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DeckError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
