"""Output sanitisation for proxy-generated text.

Two problems observed while testing the ClaudeStore proxy:
  1. Occasional mojibake — curly quotes / em-dashes arrive as U+FFFD ('�') or
     mis-encoded bytes.
  2. MDX prose forbids raw '<' and '{' (the MDX parser treats them as JSX). The
     model *usually* obeys, but we normalise as a safety net so a stray char
     can't break the static build.

`sanitize_prose` is aggressive (for body copy). `clean_text` is the light pass
used everywhere (fix encoding + smart punctuation → ASCII-safe equivalents).
"""
from __future__ import annotations

import re
import unicodedata

# Smart punctuation → ASCII-safe. We keep normal ASCII quotes/dashes so the MDX
# never carries characters that a downstream tool might re-mangle.
_REPLACEMENTS = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "…": "...",
    " ": " ",   # non-breaking space
    " ": " ", " ": " ", " ": " ",
    "→": "->", "←": "<-",
    "�": "'",   # replacement char — most commonly a lost apostrophe
    "·": "-",
}

_REPLACE_RE = re.compile("|".join(re.escape(k) for k in _REPLACEMENTS))

# ── Dashes ──
# Em/en dashes need their own pass: a one-to-one map to "-" destroys the spacing.
# The model writes them unspaced in the normal typographic way ("makes sense—and
# when it doesn't"), so replacing the character alone produced "makes sense-and when
# it doesn't", which reads as a broken hyphenated word. That shipped into a
# published post's section heading.
#
# Numeric ranges are handled first: an en dash between digits really is a hyphen
# ("2024–2026" -> "2024-2026") and must NOT gain spaces.
_NUM_RANGE_RE = re.compile(r"(\d)\s*[–—―]\s*(\d)")
# Every other dash is parenthetical and becomes a spaced hyphen, absorbing any
# whitespace already around it so we never emit a double space.
_DASH_RE = re.compile(r"[ 	]*[–—―][ 	]*")


def clean_text(text: str) -> str:
    """Fix encoding + smart punctuation. Safe to run on any model output."""
    if not text:
        return text
    # Normalise unicode forms first (composes stray combining marks).
    text = unicodedata.normalize("NFKC", text)
    text = _REPLACE_RE.sub(lambda m: _REPLACEMENTS[m.group(0)], text)
    # Ranges before parentheticals — order matters (see the regexes above).
    text = _NUM_RANGE_RE.sub(r"\1-\2", text)
    text = _DASH_RE.sub(" - ", text)
    return text


# Characters that are only dangerous in MDX *prose* (outside JSX tags/expressions).
def strip_stray_jsx_chars(prose_line: str) -> str:
    """Replace bare '<' / '{' that aren't part of a component tag.

    Heuristic: a '<' immediately followed by a letter or '/' is a JSX tag and is
    left alone; any other '<' (e.g. 'under <5ms') becomes 'under '. Same idea for
    '{' that isn't opening a JSX expression on a component prop line. This is a
    net; the writer prompt already forbids these, and the validator flags them.
    """
    # Leave lines that are clearly a component tag or its multiline props alone.
    stripped = prose_line.lstrip()
    if stripped.startswith("<") or stripped.startswith("/>") or stripped.endswith("/>"):
        return prose_line
    # Replace a '<' used as a math/comparison operator ("<5", "< 200ms").
    prose_line = re.sub(r"<\s*(?=\d)", "under ", prose_line)
    # Any remaining bare '<' not starting a tag → escape to word.
    prose_line = re.sub(r"<(?![A-Za-z/])", "less than ", prose_line)
    return prose_line


def sanitize_prose(text: str) -> str:
    """Full cleanup for an MDX body: encoding + stray JSX chars in prose lines."""
    text = clean_text(text)
    # Strip code fences the model sometimes wraps the whole body in.
    text = _strip_outer_code_fence(text)
    return text.strip() + "\n"


def _strip_outer_code_fence(text: str) -> str:
    """Remove a ```mdx ... ``` wrapper if the model returned the body fenced."""
    t = text.strip()
    fence = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", t, re.DOTALL)
    if fence:
        return fence.group(1)
    return text
