"""Read-time format auto-correction — deterministic, reviewable, replayable.

Where the *content* detectors fix messy values, this fixes a messy *file shape* at
read time: the wrong delimiter (a ``;``-separated file read as one column) and a
non-UTF-8 encoding (Excel's Western "Save as CSV"). Every correction is:

* **Deterministic** — an encoding fallback ladder (utf-8 → cp1252, which cannot
  fail to decode) and a header-consensus delimiter vote among a fixed candidate
  set. No probabilistic charset sniffing, so the result never drifts across
  dependency/locale versions.
* **Reviewable + replayable** — the chosen delimiter/encoding are pinned into the
  recipe's ``read:`` section, so :func:`cleanframe.apply_recipe` re-reads the file
  identically and never re-sniffs.
* **Fail-loud on ambiguity** — if two delimiters split the header equally well, it
  refuses (raises) rather than silently guessing, mirroring the library's
  never-silently-corrupt contract.
"""

from __future__ import annotations

import csv as _csv
from dataclasses import dataclass, field
from pathlib import Path

from .errors import CleanFrameError

_CSV_SUFFIXES = (".csv", ".txt", ".tsv")
_DELIMITER_CANDIDATES = (",", ";", "\t", "|")
_HEADER_SAMPLE_LINES = 20
#: Only the first chunk is read for detection so a multi-GB file isn't loaded to RAM.
_DETECT_BYTES = 65536


@dataclass
class ReadReport:
    """What the read-time corrector detected and changed."""

    encoding: str = "utf-8"
    delimiter: str = ","
    skipped_blank_lines: int = 0
    notes: list[str] = field(default_factory=list)

    def as_read_binding(self) -> dict:
        """The subset to pin into a recipe's ``read:`` section for replay."""
        out: dict = {}
        if self.encoding not in ("utf-8", "utf-8-sig"):
            out["encoding"] = self.encoding
        if self.delimiter != ",":
            out["sep"] = self.delimiter
        return out


def _decode(path: Path) -> tuple[str, str]:
    """Return (sample_text, encoding) via a deterministic ladder: utf-8-sig then cp1252.

    Only the first :data:`_DETECT_BYTES` are read, so detection stays O(1) memory on
    huge files. A multi-byte UTF-8 char truncated at the read boundary is not treated
    as a decode failure.
    """
    with open(path, "rb") as fh:
        raw = fh.read(_DETECT_BYTES)
    if b"\x00" in raw:
        from .errors import CleanFrameError

        raise CleanFrameError(
            f"{path.name} looks binary (contains NUL bytes). Pass an explicit "
            "encoding= if this really is text, or convert the file to UTF-8 CSV first."
        )
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError as exc:
        if exc.start >= len(raw) - 4:  # a char split at the chunk boundary, not a bad file
            return raw[: exc.start].decode("utf-8-sig"), "utf-8"
        # cp1252 maps every byte, so this cannot fail — a safe, deterministic fallback
        # for Windows/Excel CSV exports that are not valid UTF-8.
        return raw.decode("cp1252"), "cp1252"


def _pick_delimiter(lines: list[str]) -> str:
    """Vote for the delimiter that splits every header/sample line into the same
    (>1) number of fields. Comma wins ties-with-comma; a tie between two non-comma
    delimiters is refused."""
    scores: dict[str, int] = {}
    for cand in _DELIMITER_CANDIDATES:
        try:
            counts = [len(row) for row in _csv.reader(lines, delimiter=cand)]
        except _csv.Error:
            continue
        if counts and counts[0] > 1 and all(c == counts[0] for c in counts):
            scores[cand] = counts[0]
    if not scores:
        return ","  # nothing splits cleanly → treat as a single-column file
    if "," in scores:
        return ","  # comma already works → least-surprising choice
    best = max(scores.values())
    winners = sorted(c for c, s in scores.items() if s == best)
    if len(winners) > 1:
        pretty = [repr(w) for w in winners]
        raise CleanFrameError(
            f"Ambiguous delimiter: {', '.join(pretty)} each split the header into {best} "
            "columns. Pass sep=... (or --sep) explicitly."
        )
    return winners[0]


def detect_csv_options(path: str | Path) -> tuple[dict, ReadReport]:
    """Detect encoding + delimiter (+ leading blank lines) for a CSV-family file.

    Returns ``(read_options, report)`` where ``read_options`` is ready to pass to
    :func:`pandas.read_csv` (``encoding``/``sep``/``skiprows``). Raises
    :class:`~cleanframe.errors.CleanFrameError` on an ambiguous delimiter.
    """
    path = Path(path)
    text, encoding = _decode(path)
    lines = text.splitlines()

    skip = 0
    while skip < len(lines) and not lines[skip].strip():
        skip += 1

    sample = [ln for ln in lines[skip : skip + _HEADER_SAMPLE_LINES] if ln.strip()]
    # .tsv is tab by definition — don't second-guess it.
    delimiter = "\t" if path.suffix.lower() == ".tsv" else _pick_delimiter(sample)

    report = ReadReport(encoding=encoding, delimiter=delimiter, skipped_blank_lines=skip)
    if encoding == "cp1252":
        report.notes.append("decoded as Windows-1252/cp1252 (file was not valid UTF-8)")
    if delimiter not in (",", "\t"):
        report.notes.append(f"detected delimiter {delimiter!r} (not a comma)")
    if skip:
        report.notes.append(f"skipped {skip} leading blank line(s) before the header")

    options: dict = {"encoding": "utf-8-sig" if encoding == "utf-8" else encoding}
    if delimiter != ",":
        options["sep"] = delimiter
    if skip:
        options["skiprows"] = skip
    return options, report


def is_csv_family(path: str | Path) -> bool:
    return Path(path).suffix.lower() in _CSV_SUFFIXES


__all__ = ["ReadReport", "detect_csv_options", "is_csv_family"]
