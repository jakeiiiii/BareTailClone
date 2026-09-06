"""Character set handling for log files.

BareTail reads Unicode, UTF-8, ANSI and ASCII files with Windows/DOS (CR/LF),
Unix (LF) or bare CR line endings.  Two things make that harder than calling
``bytes.decode``:

* We read *windows* out of the middle of a multi-gigabyte file, so a multi-byte
  sequence can straddle the edge of a chunk.
* UTF-16 has two-byte code units, so byte offsets must stay aligned and a
  newline is a two-byte pattern rather than a single ``\n``.

``Codec`` captures both concerns so the rest of the engine can work in byte
offsets without caring which encoding is in play.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass

__all__ = [
    "Codec",
    "CODECS",
    "ANSI",
    "ASCII",
    "UTF8",
    "UTF16LE",
    "UTF16BE",
    "detect",
    "by_label",
    "split_lines",
    "expand_tabs",
]


@dataclass(frozen=True)
class Codec:
    """A supported character set.

    ``unit`` is the number of bytes per code unit; every byte offset the engine
    produces for this codec is a multiple of it.
    """

    label: str
    python_name: str
    bom: bytes
    unit: int

    @property
    def lf(self) -> bytes:
        """The byte pattern for U+000A in this encoding."""
        return "\n".encode(self.python_name)

    @property
    def cr(self) -> bytes:
        """The byte pattern for U+000D in this encoding."""
        return "\r".encode(self.python_name)

    def decode(self, data: bytes, errors: str = "replace") -> str:
        """Decode a complete buffer, never raising on malformed input.

        Log files routinely contain truncated or binary garbage; showing
        replacement characters is far better than refusing to display the line.
        """
        return data.decode(self.python_name, errors)

    def incremental_decoder(self, errors: str = "replace"):
        """A decoder that carries state across chunk boundaries.

        Use this when feeding successive chunks of the same stream, so a
        sequence split across the boundary still decodes correctly.
        """
        return codecs.getincrementaldecoder(self.python_name)(errors)

    def align(self, offset: int) -> int:
        """Round ``offset`` down to a code-unit boundary."""
        return offset - (offset % self.unit)


# ``utf-16-le``/``be`` rather than ``utf-16`` so decode() never expects a BOM
# and never emits one; the BOM is stripped by the reader before decoding.
ASCII = Codec("ASCII", "ascii", b"", 1)
ANSI = Codec("ANSI", "cp1252", b"", 1)
UTF8 = Codec("UTF-8", "utf-8", codecs.BOM_UTF8, 1)
UTF16LE = Codec("UTF-16 LE", "utf-16-le", codecs.BOM_UTF16_LE, 2)
UTF16BE = Codec("UTF-16 BE", "utf-16-be", codecs.BOM_UTF16_BE, 2)

CODECS = (UTF8, UTF16LE, UTF16BE, ANSI, ASCII)

# Longest BOM first, so UTF-8's 3-byte mark is not shadowed by a 2-byte one.
_BOM_ORDER = (UTF8, UTF16BE, UTF16LE)


def by_label(label: str) -> Codec:
    """Look up a codec by its display label, falling back to ANSI."""
    for codec in CODECS:
        if codec.label == label:
            return codec
    return ANSI


def _looks_like_utf8(sample: bytes) -> bool:
    """True if ``sample`` decodes cleanly as UTF-8.

    The sample is an arbitrary prefix of the file, so a valid character may be
    cut in half at the end.  An incremental decoder tolerates exactly that --
    it buffers an incomplete sequence at the tail without complaint -- while
    still rejecting a malformed sequence anywhere earlier.  Trimming bytes and
    retrying would instead accept ANSI text such as ``caf\\xe9\\n``, whose
    prefix ``caf`` is trivially valid UTF-8.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        decoder.decode(sample, False)
    except UnicodeDecodeError:
        return False
    return True


def _looks_like_utf16(sample: bytes) -> tuple[Codec, int] | None:
    """Detect BOM-less UTF-16 from the NUL pattern of ASCII-range text.

    Western log text in UTF-16 alternates real bytes with NULs.  Which half
    holds the NUL tells us the endianness.  Fewer than four bytes is not
    enough evidence, and a file with no NULs at all is not UTF-16.
    """
    usable = len(sample) - (len(sample) % 2)
    if usable < 4:
        return None
    pairs = [sample[i : i + 2] for i in range(0, usable, 2)]
    high_nul = sum(1 for p in pairs if p[1] == 0)
    low_nul = sum(1 for p in pairs if p[0] == 0)
    threshold = len(pairs) * 0.7
    if high_nul >= threshold and high_nul > low_nul:
        return UTF16LE, 0
    if low_nul >= threshold and low_nul > high_nul:
        return UTF16BE, 0
    return None


def detect(head: bytes, default: Codec = ANSI) -> tuple[Codec, int]:
    """Identify the encoding of a file from its opening bytes.

    Returns the codec and the length of the byte-order mark, which the caller
    must skip; content offsets in the rest of the engine are absolute, so the
    BOM length is where the first line starts rather than zero.

    A BOM is decisive.  Without one we fall back to structure: the NUL pattern
    of UTF-16, then valid UTF-8, then ``default`` for anything else.
    """
    for codec in _BOM_ORDER:
        if codec.bom and head.startswith(codec.bom):
            return codec, len(codec.bom)

    utf16 = _looks_like_utf16(head)
    if utf16 is not None:
        return utf16

    if head and _looks_like_utf8(head):
        # Pure-ASCII content is valid UTF-8 and decodes identically, so
        # reporting UTF-8 costs nothing and handles later non-ASCII appends.
        return UTF8, 0

    return default, 0


def split_lines(text: str) -> list[str]:
    """Split on CRLF, LF or bare CR, discarding the terminators.

    ``str.splitlines`` also breaks on form feed, NEL, and the Unicode line and
    paragraph separators, which appear in real log payloads and would split a
    line the file itself did not.  Only the three ASCII conventions count here.

    A trailing terminator does not produce an empty final element, so a file
    ending in a newline yields exactly its number of lines.
    """
    if not text:
        return []
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalised.endswith("\n"):
        normalised = normalised[:-1]
    return normalised.split("\n")


def expand_tabs(text: str, width: int) -> str:
    """Expand TABs to the next multiple of ``width``.

    ``str.expandtabs`` already implements the column arithmetic; a width of
    zero or less means the caller has disabled expansion.
    """
    if width <= 0:
        return text
    return text.expandtabs(width)
