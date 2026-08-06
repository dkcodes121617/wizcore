"""Entity identity — the one function that must never be re-implemented.

`entity_key` is how the system knows that the dental practice found on Reddit on
Monday and in Google Places on Thursday is **one business with two leads**, and
therefore gets pitched once.

That guarantee is not enforced by any constraint. `UNIQUE (source, source_uid)`
catches the same Reddit thread twice; it cannot catch the same *company* twice.
The only thing standing between the system and a prospect receiving two
different cold emails from WizCodes is that the Lead Finder and the Outreach
agent compute this string identically — in two separate processes, in two
separate containers, on two separate schedules.

So: one implementation, imported by both. If these ever disagree by a stripped
`www.` or a trailing dot, nothing raises, nothing logs, and the failure is
visible only to the prospect who got emailed twice.

## The three forms, in priority order

    domain:acme.com          a registrable domain (eTLD+1) — the strongest signal
    email:jane@gmail.com     a normalised address, when the domain is not a business
    reddit:some_user         `source:author` — the fallback when nothing else is known

## Why free-mail providers get special handling

`jane@gmail.com` and `john@gmail.com` are two different prospects. Keying them
both to `domain:gmail.com` would collapse every consumer-mailbox lead in the
system into a single entity, and the 90-day cooldown on that one entity would
then suppress all of them after the first contact. The free-provider check below
is what prevents that, and it is the single most consequential branch here.
"""
from __future__ import annotations

import re

import tldextract

# `suffix_list_urls=()` pins tldextract to the snapshot bundled in the wheel.
# The default behaviour fetches the Public Suffix List over the network on first
# use and caches it — which means a cold Modal container's first entity_key call
# depends on an HTTP request to a third party. A scheduled agent must not have
# that failure mode, and a stale-by-months suffix list is harmless for the
# domains this system actually sees.
_extract = tldextract.TLDExtract(suffix_list_urls=())

# Mailbox providers where the domain identifies the provider, not a business.
# Deliberately conservative: a domain wrongly listed here splits one business
# into many entities (annoying, recoverable), while a domain wrongly *absent*
# merges many businesses into one (silent, and suppresses real prospects).
_FREE_MAIL = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in",
        "ymail.com", "rocketmail.com", "hotmail.com", "hotmail.co.uk", "outlook.com",
        "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "mac.com",
        "protonmail.com", "proton.me", "pm.me", "gmx.com", "gmx.net", "gmx.de",
        "mail.com", "zoho.com", "zohomail.com", "yandex.com", "yandex.ru",
        "fastmail.com", "hushmail.com", "tutanota.com", "tuta.io", "mail.ru",
        "inbox.com", "rediffmail.com", "qq.com", "163.com", "126.com", "naver.com",
        "hey.com", "duck.com", "web.de", "t-online.de", "orange.fr", "free.fr",
        "comcast.net", "verizon.net", "att.net", "sbcglobal.net", "bellsouth.net",
        "btinternet.com", "sky.com", "virginmedia.com", "libero.it", "seznam.cz",
    }
)

# Hosts that are never a prospect's own business identity.
_NON_BUSINESS_HOSTS = frozenset(
    {
        "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
        "reddit.com", "youtube.com", "tiktok.com", "pinterest.com", "medium.com",
        "github.com", "gitlab.com", "news.ycombinator.com", "ycombinator.com",
        "stackoverflow.com", "stackexchange.com", "google.com", "goo.gl",
        "bit.ly", "t.co", "linktr.ee", "wa.me", "whatsapp.com", "t.me",
        "sites.google.com", "business.site", "wixsite.com", "blogspot.com",
        "wordpress.com", "weebly.com", "squarespace.com", "godaddysites.com",
        "myshopify.com", "amazonaws.com", "web.app", "firebaseapp.com",
        "netlify.app", "vercel.app", "pages.dev", "workers.dev", "github.io",
    }
)

_WS = re.compile(r"\s+")


def registrable_domain(value: str) -> str:
    """eTLD+1 for a URL, host or bare domain. '' if there isn't one.

    Handles the shapes that actually arrive from the sources: bare hosts
    ('acme.com'), full URLs, protocol-relative URLs, and hosts with a port or a
    trailing dot.
    """
    if not value:
        return ""
    value = _WS.sub("", value).strip().lower().rstrip(".")
    if not value:
        return ""
    # tldextract handles a bare host fine, but a URL with no scheme and a path
    # ('acme.com/contact') needs one or the path is read as part of the host.
    if value.startswith("//"):          # protocol-relative
        value = "http:" + value
    elif "//" not in value:             # bare host, or host + path
        value = "http://" + value
    ext = _extract(value)
    if not (ext.domain and ext.suffix):
        return ""
    return f"{ext.domain}.{ext.suffix}"


def is_free_mail(domain: str) -> bool:
    return domain.lower() in _FREE_MAIL


def is_business_domain(domain: str) -> bool:
    """False for mailbox providers, social networks, shorteners and site builders.

    A site-builder host is excluded because `acme.wixsite.com` collapses to
    `wixsite.com`, which would merge every Wix-hosted prospect into one entity —
    and Wix-hosted businesses are precisely the weak-website pipeline's target,
    so this is not a rare edge case for this system.
    """
    d = domain.lower()
    return bool(d) and d not in _FREE_MAIL and d not in _NON_BUSINESS_HOSTS


def normalize_email(email: str) -> str:
    """Lowercase, trim, drop a `+tag`, and drop dots for Gmail only.

    This is the **matching** form, used for `core.contacts.value_norm` and for
    `core.suppressions`. It is deliberately aggressive: the cost of
    over-normalising is that two addresses are treated as one mailbox, while the
    cost of under-normalising is emailing someone who has already unsubscribed
    under a `+tag` variant. The second is the one that ends a sending domain.

    Never send to this value — send to the address as given. `core.contacts`
    keeps both: `value_norm` for matching, `display` for delivery.
    """
    if not email:
        return ""
    email = _WS.sub("", email).strip().lower().strip("<>").rstrip(".")
    if email.count("@") != 1:
        return ""
    local, _, host = email.partition("@")
    if not local or not host:
        return ""
    local = local.split("+", 1)[0]
    # Gmail ignores dots in the local part; almost nobody else does.
    if host in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
        host = "gmail.com"
    if not local:
        return ""
    return f"{local}@{host}"


def normalize_linkedin(url: str) -> str:
    """Canonical `linkedin.com/in/<slug>` or `linkedin.com/company/<slug>`."""
    if not url:
        return ""
    m = re.search(r"linkedin\.com/(in|company|school)/([^/?#\s]+)", url.strip(), re.I)
    if not m:
        return ""
    return f"linkedin.com/{m.group(1).lower()}/{m.group(2).lower().rstrip('/')}"


def normalize_phone(phone: str, default_country_code: str = "") -> str:
    """Best-effort E.164. Returns '' when the result would not be dialable.

    No phonenumbers dependency: this system uses phone numbers as a dedup and
    suppression key, never to place a call, so digit normalisation is enough and
    a 10 MB metadata package is not worth carrying into three container images.
    """
    if not phone:
        return ""
    digits = re.sub(r"[^\d+]", "", phone.strip())
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+") and default_country_code:
        digits = f"+{default_country_code.lstrip('+')}{digits.lstrip('0')}"
    if not digits.startswith("+"):
        return ""
    body = digits[1:]
    # E.164 allows 8-15 digits including the country code. Anything outside that
    # is an extension, a partial capture, or a scrape artefact.
    return digits if body.isdigit() and 8 <= len(body) <= 15 else ""


def entity_key(
    *,
    domain: str = "",
    url: str = "",
    email: str = "",
    source: str = "",
    author: str = "",
) -> str:
    """The canonical business identity. Raises if nothing usable was supplied.

    Priority is deliberate — strongest evidence of a distinct *business* wins:

      1. an explicit domain, or a domain parsed out of a URL
      2. the domain of an email address, when that domain is a business
      3. the normalised email address itself (free-mail providers)
      4. `source:author`

    Raising on an empty result is intentional. A silent fallback such as
    `"unknown"` would put every unidentifiable lead into a single entity, and
    that entity's 90-day cooldown would then suppress all of them after one
    contact — a data-loss bug that looks exactly like "the queue is quiet".
    """
    for candidate in (domain, url):
        d = registrable_domain(candidate)
        if d and is_business_domain(d):
            return f"domain:{d}"

    norm_email = normalize_email(email)
    if norm_email:
        host = norm_email.split("@", 1)[1]
        d = registrable_domain(host)
        if d and is_business_domain(d):
            return f"domain:{d}"
        return f"email:{norm_email}"

    src = re.sub(r"[^a-z0-9_]+", "", (source or "").strip().lower())
    who = (author or "").strip().lower().lstrip("@/")
    # Reddit hands back authors as '/u/name' and subreddits as '/r/name'. Left
    # in, the key becomes 'reddit:u/name' while the same person arriving from
    # the API as 'name' becomes 'reddit:name' — two entities, one human, and the
    # dedup this function exists for is defeated by a two-character prefix.
    who = re.sub(r"^(?:u|r|user|profile)/", "", who)
    who = re.sub(r"[^a-z0-9_.-]+", "_", who).strip("_.-")
    if src and who:
        return f"{src}:{who}"

    raise ValueError(
        "entity_key needs at least one of: a business domain/url, an email, "
        f"or source+author (got domain={domain!r} url={url!r} email={email!r} "
        f"source={source!r} author={author!r})"
    )


def try_entity_key(**kwargs) -> str | None:
    """`entity_key` for call sites that legitimately expect misses.

    A source that yields candidates with nothing identifiable in them should skip
    those rows, not abort the run.
    """
    try:
        return entity_key(**kwargs)
    except ValueError:
        return None
