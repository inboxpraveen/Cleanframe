"""Read-time format auto-correction — deterministic, reviewable, replayable.

Where the *content* detectors fix messy values, this fixes a messy *file shape* at
read time: the wrong delimiter (a ``;``-separated file read as one column) and a
non-UTF-8 encoding (Excel's Western "Save as CSV"). Every correction is:

* **Deterministic** — an encoding ladder (utf-8 → cp1252) and a header-consensus
  delimiter vote among a fixed candidate set. No probabilistic charset sniffing
  decides what is *accepted*, so the result never drifts across dependency/locale
  versions. An optional ``charset-normalizer`` only ranks candidates in the error
  message when a file looks like a multi-byte / non-Latin encoding.
* **Reviewable + replayable** — the chosen delimiter/encoding are pinned into the
  recipe's ``read:`` section, so :func:`cleanframe.apply_recipe` re-reads the file
  identically and never re-sniffs.
* **Fail-loud on ambiguity** — if two delimiters split the header equally well, or
  a file looks like Shift-JIS/GBK/Big5/EUC-KR/Cyrillic bytes that a Western codec
  would turn into mojibake, it refuses (raises) rather than silently guessing,
  mirroring the library's never-silently-corrupt contract.
"""

from __future__ import annotations

import codecs
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
        if self.skipped_blank_lines:
            out["blank_lines"] = self.skipped_blank_lines
        return out


#: Fewer high bytes than this is too little evidence to tell a Western accent from
#: the start of a multi-byte character; the file is read as cp1252/latin-1 with a note.
_MIN_HIGH_BYTES = 4
#: Multi-byte codecs a Western-looking decode is checked against.
_MULTIBYTE_CODECS = ("cp932", "gb18030", "big5", "cp949")


def _high_stats(raw: bytes) -> tuple[int, float, float]:
    """(high-byte count, fraction with a high neighbour, fraction in runs of 3+)."""
    n = len(raw)
    high = [b >= 0x80 for b in raw]
    total = sum(high)
    if not total:
        return 0, 0.0, 0.0
    adjacent = 0
    long_run = 0
    i = 0
    while i < n:
        if not high[i]:
            i += 1
            continue
        j = i
        while j < n and high[j]:
            j += 1
        run = j - i
        if run >= 2:
            adjacent += run
        if run >= 3:
            long_run += run
        i = j
    return total, adjacent / total, long_run / total


def _strict_decodes(raw: bytes, codec: str) -> bool:
    """Does ``raw`` decode under ``codec`` (a char split at the chunk end is tolerated)?"""
    try:
        raw.decode(codec)
        return True
    except UnicodeDecodeError as exc:
        if exc.start >= len(raw) - 4:
            return False if exc.start == 0 else _strict_decodes(raw[: exc.start], codec)
        return False
    except LookupError:  # pragma: no cover - codec missing from this Python
        return False


def _candidate_encodings(raw: bytes) -> list[str]:
    """Best-effort ranked guesses for the error message. Never decides acceptance."""
    try:  # optional: pip install cleanframe-engine[detect]
        from charset_normalizer import from_bytes

        ranked = [m.encoding for m in list(from_bytes(raw))[:3]]
        if ranked:
            return [str(e) for e in ranked]
    except Exception:  # noqa: BLE001 - optional dependency, advisory only
        pass
    fixed = [c for c in _MULTIBYTE_CODECS + ("cp1251",) if _strict_decodes(raw, c)]
    return fixed or ["cp1252"]


def _looks_non_western(raw: bytes) -> bool:
    """True when the high bytes read as multi-byte / non-Latin text, not Western accents.

    Western text puts an isolated accent inside ASCII words (``Zürich``), so its high
    bytes almost never neighbour each other. Shift-JIS/GBK/Big5/EUC-KR text and
    single-byte Cyrillic/Greek put them side by side. A neighbour test alone would
    misfire on smart quotes, so it also needs a multi-byte codec to decode the sample
    cleanly, or long runs of high bytes.
    """
    total, adjacent, long_run = _high_stats(raw)
    if total < _MIN_HIGH_BYTES or adjacent < 0.5:
        return False
    if long_run >= 0.5:
        return True
    return any(_strict_decodes(raw, c) for c in _MULTIBYTE_CODECS)


def _decode(path: Path, encoding: str | None = None) -> tuple[str, str]:
    """Return (sample_text, encoding) via a deterministic ladder.

    utf-8-sig, then cp1252 — but a non-UTF-8 file whose bytes look like a multi-byte
    or non-Latin encoding is *refused*, not decoded into mojibake. latin-1 is only
    the last rung for a file cp1252 cannot decode (its five undefined bytes), and
    says so. An explicit ``encoding`` skips the ladder.

    Only the first :data:`_DETECT_BYTES` are read, so detection stays O(1) memory on
    huge files. A multi-byte UTF-8 char truncated at the read boundary is not treated
    as a decode failure.
    """
    with open(path, "rb") as fh:
        raw = fh.read(_DETECT_BYTES)
    if encoding is not None:
        try:
            return raw.decode(encoding, errors="ignore"), encoding
        except LookupError as exc:
            raise CleanFrameError(f"Unknown encoding {encoding!r}: {exc}") from exc
    for bom, codec in (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if raw.startswith(bom):
            return raw.decode(codec, errors="ignore"), codec
    if b"\x00" in raw:
        raise CleanFrameError(
            f"{path.name} looks binary (contains NUL bytes, and no UTF-16/32 byte-order "
            "mark). Pass an explicit encoding= if this really is text, or convert the "
            "file to UTF-8 CSV first."
        )
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError as exc:
        if exc.start >= len(raw) - 4:  # a char split at the chunk boundary, not a bad file
            return raw[: exc.start].decode("utf-8-sig"), "utf-8"
    if _looks_non_western(raw):
        guesses = ", ".join(_candidate_encodings(raw))
        raise CleanFrameError(
            f"{path.name} is not UTF-8 and its bytes look like a multi-byte or non-Latin "
            "encoding (Shift-JIS / GBK / Big5 / EUC-KR / Cyrillic …); decoding it as a "
            "Western codec would produce garbled text. Likely encodings: "
            f"{guesses}. Pass encoding=... (CLI --encoding) to choose one."
        )
    # cp1252 leaves five byte values undefined, so latin-1 (which maps all 256) is the
    # final rung for a file that is merely not UTF-8 and has too little evidence to
    # tell from Western text. It is reported, never silent.
    for codec in ("cp1252", "latin-1"):
        try:
            return raw.decode(codec), codec
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1"  # pragma: no cover


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


def suggest_header_row_from_counts(counts: list[int]) -> int | None:
    """Pick the header line from per-line populated-field counts (see below)."""
    for i, width in enumerate(counts):
        if width < 2:
            continue
        following = [c for c in counts[i + 1 : i + 11] if c]
        if not following:
            return i or None
        modal = max(sorted(set(following)), key=following.count)
        if width == modal:
            return i or None
    return None


def suggest_header_row(lines: list[str], delimiter: str = ",") -> int | None:
    """Guess which physical line holds the header when title rows sit above it.

    Returns the 0-based line index of the first line that has at least two populated
    fields and as many as the modal width of the lines after it, or ``None`` when line 0
    already is that line (no title rows) or nothing fits. A suggestion only — callers
    put it in an error/warning message and never apply it.
    """
    try:
        rows = list(_csv.reader(lines, delimiter=delimiter))
    except _csv.Error:
        return None
    return suggest_header_row_from_counts([sum(1 for c in row if c.strip()) for row in rows])


def detect_csv_options(
    path: str | Path,
    *,
    encoding: str | None = None,
    sep: str | None = None,
    header_row: int | None = None,
) -> tuple[dict, ReadReport]:
    """Detect encoding + delimiter (+ leading blank lines) for a CSV-family file.

    Returns ``(read_options, report)`` where ``read_options`` is ready to pass to
    :func:`pandas.read_csv` (``encoding``/``sep``/``skiprows``). Raises
    :class:`~cleanframe.errors.CleanFrameError` on an ambiguous delimiter or a file
    that looks like a multi-byte encoding. An explicit ``encoding``/``sep`` skips that
    part of detection (so passing them always gets past a refusal), and ``header_row``
    (0-based physical line of the header) replaces blank-line skipping.
    """
    path = Path(path)
    text, encoding = _decode(path, encoding)
    lines = text.splitlines()

    if header_row is not None:
        skip = header_row
    else:
        skip = 0
        while skip < len(lines) and not lines[skip].strip():
            skip += 1

    sample = [ln for ln in lines[skip : skip + _HEADER_SAMPLE_LINES] if ln.strip()]
    if sep is not None:
        delimiter = sep
    elif path.suffix.lower() == ".tsv":  # tab by definition — don't second-guess it
        delimiter = "\t"
    else:
        delimiter = _pick_delimiter(sample)

    report = ReadReport(encoding=encoding, delimiter=delimiter, skipped_blank_lines=skip)
    if encoding == "latin-1":
        report.notes.append(
            "decoded as latin-1: the file is not valid UTF-8 and cp1252 cannot decode it "
            "either (too few non-ASCII bytes to identify the real encoding). Check the "
            "text, or pass encoding=... (CLI --encoding)"
        )
    elif encoding not in ("utf-8", "utf-8-sig"):
        report.notes.append(f"decoded as {encoding} (file was not valid UTF-8)")
    if delimiter not in (",", "\t"):
        report.notes.append(f"detected delimiter {delimiter!r} (not a comma)")
    if skip and header_row is None:
        report.notes.append(f"skipped {skip} leading blank line(s) before the header")

    options: dict = {"encoding": "utf-8-sig" if encoding == "utf-8" else encoding}
    if delimiter != ",":
        options["sep"] = delimiter
    if skip:
        options["blank_lines"] = skip
    return options, report


def is_csv_family(path: str | Path) -> bool:
    return Path(path).suffix.lower() in _CSV_SUFFIXES


__all__ = ["ReadReport", "detect_csv_options", "is_csv_family", "suggest_header_row",
           "suggest_header_row_from_counts"]
