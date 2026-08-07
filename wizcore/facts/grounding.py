"""The grounding gate — the one that matters most.

Every named project, client, number and claim in generated output must trace
back to the site repo via the facts snapshot. Anything citing a statistic that
is not in the source data is rejected outright, and no amount of the draft being
otherwise good overrides that.

This is the difference between agents that market WizCodes and agents that
invent things about WizCodes in public.

## Why this lives in wizcore

It started in the Content Poster. It belongs here because it passes the
admission test in the README: **two copies of this drifting would be a bug, not
duplication.** The Content Poster publishes to an account that cannot un-post;
the Outreach agent emails a named individual who may personally know the client
being invented. Same failure, same check — and two versions that disagreed would
mean one agent silently permitting what the other rejects.

Its inputs come from `wizcore.facts.snapshot`, which is already here, so nothing
about the move added a dependency.

## Why it checks the corpus rather than a list of allowed numbers

A curated set would miss the same figure written differently — "1000+" against
"1,000", "11 countries" against "11". So the check asks whether the digits
appear *anywhere* in the source data, and only uses the curated set to explain a
rejection. Generous on purpose: the target is an invented "we increased
conversions by 47%", not "3 steps".
"""
from __future__ import annotations

import re

# Numbers that carry no factual claim. Small counts, years, times of day and
# percentages-of-nothing appear constantly in ordinary prose, and flagging them
# would make the gate so noisy it would be switched off — which is how a real
# invented statistic gets through.
_SAFE_NUMBERS = {str(n) for n in range(0, 13)}
_YEAR = re.compile(r"^(19|20)\d{2}$")

# Figures worth checking: anything with a %, a currency symbol, a multiplier, a
# unit, or four or more digits.
_CLAIM_NUMBER = re.compile(
    r"(?<![\w.])"
    r"(?:[$£€]\s?\d[\d,.]*\s?[kmb]?"          # $40k, £1,200
    r"|\d[\d,.]*\s?%"                          # 47%
    r"|\d[\d,.]*\s?x\b"                        # 3x
    r"|\d[\d,.]*\s?(?:ms|s|sec|seconds|minutes|hours|days|weeks|months|years)\b"
    r"|\d{4,}"                                 # 1000, 25000
    r"|\d[\d,.]*\s?\+)",                       # 1000+
    re.I,
)

# Capitalised multi-word phrases that look like a product or client name.
_PROPER_NOUN = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:[A-Z][a-zA-Z0-9]+)*)\b")

# Words that are capitalised for grammar or are common tech, not claims of ours.
_NOT_A_CLIENT = {
    "I", "We", "Our", "The", "This", "That", "It", "A", "An", "And", "But", "So",
    "If", "When", "What", "Why", "How", "You", "Your", "They", "There", "Here",
    "Most", "One", "Two", "Three", "No", "Not", "Every", "Each", "After", "Before",
    "React", "Next", "Node", "Python", "Django", "Flutter", "Swift", "Kotlin",
    "AWS", "Google", "Apple", "Meta", "Facebook", "Instagram", "LinkedIn", "Threads",
    "Android", "iOS", "API", "APIs", "SaaS", "AI", "ML", "UI", "UX", "SEO", "MVP",
    "WordPress", "Shopify", "Wix", "Squarespace", "Stripe", "Figma", "GitHub",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
}


def check(text: str, snapshot) -> list[str]:
    """Return a list of reasons the text is not grounded. Empty means it passes."""
    reasons: list[str] = []
    corpus = snapshot.facts_corpus()
    known = {n.lower() for n in snapshot.known_names()}

    for raw in _CLAIM_NUMBER.findall(text):
        figure = raw.strip()
        digits = re.sub(r"[^\d]", "", figure)
        if not digits or digits in _SAFE_NUMBERS or _YEAR.match(digits):
            continue
        # Try the figure as written, then bare digits, then with thousands
        # separators — "1,000" in a draft against "1000" in the source is the
        # same fact and must not be rejected.
        variants = {
            figure.lower(),
            digits,
            f"{int(digits):,}" if digits.isdigit() and len(digits) > 3 else digits,
        }
        if not any(v in corpus for v in variants if v):
            reasons.append(
                f"figure {figure!r} does not appear anywhere in the site facts - "
                "every number must come from the repo"
            )

    for name in set(_PROPER_NOUN.findall(text)):
        if name in _NOT_A_CLIENT or len(name) < 4:
            continue
        # Only flag names that look like OUR project or client being asserted.
        # A prospect's industry ("Leeds", "Dental") is not a claim about us.
        if name.lower() in corpus or name.lower() in known:
            continue
        if _looks_like_our_claim(text, name):
            reasons.append(
                f"{name!r} is presented as a WizCodes project or client but is not "
                "in the site repo"
            )
    return reasons


def _looks_like_our_claim(text: str, name: str) -> bool:
    """True when `name` sits next to a first-person claim of having built it.

    Without this the gate would flag every capitalised word in an example, and a
    gate that rejects good posts gets turned off.
    """
    window = re.search(
        r"(?:we|our|us|wizcodes)\b[^.!?]{0,80}\b" + re.escape(name)
        + r"|" + re.escape(name) + r"\b[^.!?]{0,60}\b(?:we|our|us|wizcodes)\b",
        text,
        re.I,
    )
    return bool(window)
