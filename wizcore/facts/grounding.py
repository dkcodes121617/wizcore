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

## How a figure is matched, and why it is by TOKEN not by substring

A curated list of allowed numbers would miss the same figure written differently
— "1000+" against "1,000", "under 200ms" against "<200ms". So the corpus is the
authority, and a draft's figure is grounded when it matches a number that
actually appears in it.

The first version asked whether the digits appeared *anywhere* in the corpus, as
a substring. Against 26 projects, 13 testimonials and a 70 KB playbook that is
not a weak check, it is **no check at all for any short figure**: "15%" passes
because "15" occurs inside some unrelated "2015" or "1500", and so does almost
every other two-digit number. Measured — `tools/e2e.py` found both
"commissions dropped to 15%" and a bar chart of invented percentages passing a
gate whose entire purpose is to stop exactly those.

So the corpus is tokenised once into the set of numbers it contains, and a
figure has to equal one of them after normalising away separators, currency and
units. Still generous about *format*, and no longer generous about arithmetic.
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


# Any run of digits with optional separators. Tokenising the corpus with this
# and comparing digit strings is what makes "1,000" and "1000" the same fact and
# "15" and "2015" different ones.
_NUMBER_TOKEN = re.compile(r"\d[\d,.\s]*\d|\d")


def _number_set(corpus: str) -> set[str]:
    """Every number the corpus contains, as bare digit strings.

    Both with and without a trailing zero-group, so "1.5" contributes "15" and
    "1.5" contributes "15" — the point is that "1,240" and "1240" and "1 240"
    all reduce to the same key while "15" never reduces to "1500".
    """
    out: set[str] = set()
    for raw in _NUMBER_TOKEN.findall(corpus):
        digits = re.sub(r"[^\d]", "", raw)
        if digits:
            out.add(digits)
            out.add(digits.lstrip("0") or "0")
    return out


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
    # Figures check against the NARROWER corpus. `facts_corpus()` includes the
    # 71 KB brand playbook, whose incidental numbers made almost every two-digit
    # figure look grounded. Names still use the full corpus — a project
    # mentioned only in the playbook is real; a number there is not a fact.
    corpus_numbers = _number_set(snapshot.numbers_corpus())
    cited_numbers = _number_set(cited) if cited else set()

    for raw in _CLAIM_NUMBER.findall(text):
        figure = raw.strip()
        digits = re.sub(r"[^\d]", "", figure)
        if not digits or digits in _SAFE_NUMBERS or _YEAR.match(digits):
            continue
        # Compared as a whole number, not as a substring. "1,000" in a draft
        # against "1000" in the source is the same fact; "15" against "2015" is
        # not, and the substring version could not tell them apart.
        variants = {digits, digits.lstrip("0") or "0"}
        if variants & corpus_numbers:
            continue
        if cited_numbers and variants & cited_numbers:
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
    corpus_numbers = _number_set(snapshot.numbers_corpus())
    cited_numbers = _number_set(_source_corpus(sources))
    missing: list[str] = []
    for raw in _CLAIM_NUMBER.findall(text):
        figure = raw.strip()
        digits = re.sub(r"[^\d]", "", figure)
        if not digits or digits in _SAFE_NUMBERS or _YEAR.match(digits):
            continue
        # Token-matched, exactly as in `check`. These two must agree: the angle
        # synthesiser uses this to decide which figures still need a source
        # fetched, and if it disagreed with the gate it would either fetch
        # sources nothing needed or ship a post the gate then rejects.
        variants = {digits, digits.lstrip("0") or "0"}
        if variants & corpus_numbers or (cited_numbers and variants & cited_numbers):
            continue
        missing.append(figure)
    return missing


# A capital letter inside the word — CuePilot, ClarivueXAI, TinyTalkHub. This is
# what a product name looks like and an ordinary word does not.
_CAMEL = re.compile(r"[a-z][A-Z]")

# The tight constructions that actually assert ownership. Used for ordinary
# capitalised words, where the wide window below produces false positives.
_OWNED = (
    r"our\s+(?:client|customer|project|product|app|platform|tool|work\s+(?:for|with))\s+"
    r"(?:the\s+)?{name}"
    r"|we\s+(?:built|made|shipped|launched|created|developed|designed|delivered)\s+"
    r"(?:the\s+|a\s+|an\s+)?{name}"
    r"|{name}\s*,?\s+(?:is\s+)?(?:our|one\s+of\s+our)\s+"
    r"(?:client|customer|project|product|app|platform)"
)


def _looks_like_our_claim(text: str, name: str) -> bool:
    """True when `name` sits next to a first-person claim of having built it.

    Without this the gate would flag every capitalised word in an example, and a
    gate that rejects good posts gets turned off.

    ## Two widths, because two kinds of word carry different evidence

    A **CamelCase** name is a product name almost by definition, so anywhere
    within a sentence of "we / our" is enough to treat it as a claim.

    An **ordinary capitalised word** is usually a place, a sentence-initial
    verb, or a common noun. Measured against a review sweep, the wide window
    rejected two perfectly good posts: `Denver` in "a clinic in Denver" and
    `Confirms` at the start of a sentence. Neither claims anything about us, and
    both cost a published post.

    So an ordinary word has to sit in a construction that actually asserts
    ownership — "our client X", "we built X", "X is one of our projects". That
    is narrower, and the thing it stops catching was never a real claim.
    """
    quoted = re.escape(name)
    if _CAMEL.search(name):
        window = (
            rf"(?:we|our|us|wizcodes)\b[^.!?]{{0,80}}\b{quoted}"
            rf"|{quoted}\b[^.!?]{{0,60}}\b(?:we|our|us|wizcodes)\b"
        )
    else:
        window = _OWNED.format(name=quoted)
    return bool(re.search(window, text, re.I))
