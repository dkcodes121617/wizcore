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


def check(text: str, snapshot, sources: list | None = None) -> list[str]:
    """Reasons the text is not grounded. Empty means it passes.

    ## Two corpora, two rules, one gate

    Timely content breaks the original single-corpus rule, and relaxing the rule
    would trade the system's best safety property for reach. So the gate splits
    by *what the claim is about* instead:

    | Claim about | Must trace to | Rule |
    |---|---|---|
    | **WizCodes** — our projects, clients, results | the site-repo snapshot | absolute, unchanged |
    | **the world** — anyone else's product or statistic | a captured source extract | must exist, and be cited |

    `sources` is a list of captured evidence — anything with an `extract`
    attribute or key, typically `content.trend_sources` rows. Pass it and an
    external figure becomes publishable *if and only if* it appears in
    something we actually fetched and stored.

    Note the direction: with no `sources`, behaviour is exactly as before —
    every external figure is rejected. Supplying sources does not weaken the
    gate, it gives a legitimate way to satisfy it. An uncited external number is
    still rejected, exactly as an invented WizCodes number is.
    """
    reasons: list[str] = []
    corpus = snapshot.facts_corpus()
    known = {n.lower() for n in snapshot.known_names()}
    cited = _source_corpus(sources)

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
        if any(v in corpus for v in variants if v):
            continue
        if cited and any(v in cited for v in variants if v):
            continue    # substantiated by a captured source
        reasons.append(
            f"figure {figure!r} appears in neither the site facts nor any captured "
            "source - every number must trace to the repo or to something we fetched"
            + (" and stored" if cited else " (no sources were supplied)")
        )

    for name in set(_PROPER_NOUN.findall(text)):
        if name in _NOT_A_CLIENT or len(name) < 4:
            continue
        # Only flag names that look like OUR project or client being asserted.
        # A prospect's industry ("Leeds", "Dental") is not a claim about us.
        if name.lower() in corpus or name.lower() in known:
            continue
        if _looks_like_our_claim(text, name):
            # Deliberately NOT satisfiable by a captured source. A third party's
            # press release can substantiate "their product does X"; nothing
            # external can substantiate "we built X". That claim can only come
            # from our own repo, so this branch stays single-corpus forever.
            reasons.append(
                f"{name!r} is presented as a WizCodes project or client but is not "
                "in the site repo"
            )
    return reasons


def _source_corpus(sources: list | None) -> str:
    """Flatten captured evidence into one lowercase blob for substring checks.

    Accepts dicts (database rows) or objects with an `extract`, so a caller can
    pass `content.trend_sources` rows straight through without shaping them.
    """
    if not sources:
        return ""
    parts: list[str] = []
    for source in sources:
        for field in ("extract", "title", "publisher"):
            value = (
                source.get(field) if isinstance(source, dict)
                else getattr(source, field, None)
            )
            if value:
                parts.append(str(value))
    return " \n".join(parts).lower()


def uncited_claims(text: str, snapshot, sources: list | None = None) -> list[str]:
    """The figures in `text` that no source substantiates.

    Same detection as `check`, but returns the offending figures rather than
    prose. The angle synthesiser uses this to decide what still needs a source
    fetched, which is how "capture what you need to cite" becomes a loop the
    system can close on its own instead of a rule someone has to follow.
    """
    corpus = snapshot.facts_corpus()
    cited = _source_corpus(sources)
    missing: list[str] = []
    for raw in _CLAIM_NUMBER.findall(text):
        figure = raw.strip()
        digits = re.sub(r"[^\d]", "", figure)
        if not digits or digits in _SAFE_NUMBERS or _YEAR.match(digits):
            continue
        variants = {figure.lower(), digits}
        if any(v in corpus for v in variants if v):
            continue
        if cited and any(v in cited for v in variants if v):
            continue
        missing.append(figure)
    return missing


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
