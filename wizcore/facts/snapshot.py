"""The 'real WizCodes facts' grounding context.

Seeded from the blog agent, whose own header states the reason and is worth
repeating rather than paraphrasing:

    The #1 hallucination risk (confirmed in proxy testing) is the model
    inventing numbers, client names, and sectors.

Every agent in this system has that risk, in a place where it is worse than a
bad blog post:

  - **Content Poster** — `case_study`, `testimonial` and `blog_teaser` posts
    *are* this snapshot. Without it the agent invents a client, in public, on an
    account that cannot un-post.
  - **Lead Finder** — needs the canonical service names to map a lead to a
    service line. Hardcoding them in three agents means the day a fourth service
    is added, two agents are silently wrong.
  - **Outreach** — `grounding_project` must be a real project with a real URL.
    "We built exactly this for CuePilot" only lands if that page exists.

The extractors below are carried over verbatim, deliberately. They never execute
TypeScript — they pull durable fields with tolerant regex, so a formatting
change in the source degrades to a missing field instead of crashing a
scheduled run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from wizcore.facts.site import SiteReader

# Paths inside the site repo. Kept here rather than in each agent's config: they
# are a property of the website's layout, and three agents holding three copies
# is three things to update when a file moves.
SITE_CONFIG = "src/config/site.ts"
PROJECTS = "src/data/projects.ts"
OPEN_SOURCE = "src/data/openSource.ts"
TESTIMONIALS = "src/data/testimonials.ts"
POSTS_REGISTRY = "src/content/blog/posts.ts"
SOCIAL_STATS = "src/data/socialStats.ts"
PLAYBOOK = "details.md"


@dataclass
class ProjectFact:
    id: str = ""
    name: str = ""
    category: str = ""
    industry: str = ""
    client: str = ""
    client_country: str = ""
    description: str = ""
    tech: list[str] = field(default_factory=list)
    slug: str = ""
    hide_status: bool = False

    @property
    def url(self) -> str:
        return f"https://wizcodes.site/work/{self.slug}" if self.slug else ""


@dataclass
class TestimonialFact:
    name: str = ""
    country: str = ""
    platform: str = ""
    text: str = ""
    role: str = ""
    company: str = ""


@dataclass
class FactsSnapshot:
    brand_name: str = "WizCodes"
    tagline: str = ""
    url: str = "https://wizcodes.site"
    contact_email: str = ""
    locality: str = ""
    region: str = ""
    country: str = ""
    founding_year: str = ""
    services: list[dict] = field(default_factory=list)      # {name, slug, oneLineDescription}
    projects: list[ProjectFact] = field(default_factory=list)
    open_source: list[dict] = field(default_factory=list)   # {name, description, downloads}
    testimonials: list[TestimonialFact] = field(default_factory=list)
    existing_posts: list[dict] = field(default_factory=list)  # {slug, title, description, tags}
    # Figures a chart may draw. {id, label, value, unit, scope, source, ...}
    # `scope` is 'ours' (derived from this repo) or 'industry' (someone else's
    # published figure, which must carry an attribution to be usable at all).
    curated_stats: list[dict] = field(default_factory=list)
    playbook_excerpt: str = ""

    # ── derived, and derived the same way the site derives it ──
    @property
    def served_countries(self) -> list[str]:
        """Union of project client countries and testimonial countries.

        `src/data/stats.ts` computes `countriesServed` exactly this way, from
        exactly these two sources. Any other derivation here would let an agent
        publish a number that contradicts the number on the website — which is a
        worse failure than publishing no number, because it is checkable.
        """
        seen: list[str] = []
        for value in [p.client_country for p in self.projects] + [
            t.country for t in self.testimonials
        ]:
            if value and value not in seen:
                seen.append(value)
        return seen

    @property
    def countries_served(self) -> int:
        return len(self.served_countries)

    def service_names(self) -> list[str]:
        """The canonical service labels — the only allowed forms anywhere.

        These must stay identical to `core.leads.service_line`'s CHECK
        constraint. Verified against the live site and the live schema: both are
        'Web Development', 'Mobile Apps', 'AI Automation'.
        """
        return [s["name"] for s in self.services]

    def project_by_slug(self, slug: str) -> ProjectFact | None:
        return next((p for p in self.projects if p.slug == slug), None)

    def find_grounding_project(
        self, industry: str = "", service_line: str = "", tech: str = ""
    ) -> ProjectFact | None:
        """The most relevant REAL project to cite, or None.

        Returning None is a legitimate answer and callers must handle it: an
        outreach email with no grounding project should be skipped, never sent
        with an invented one. That is the whole point of this function existing
        instead of a prompt asking the model to "pick a relevant project".
        """
        pool = [p for p in self.projects if p.slug and not p.hide_status]
        if not pool:
            return None

        def score(p: ProjectFact) -> int:
            s = 0
            if industry and industry.lower() in (p.industry or "").lower():
                s += 10
            if industry and industry.lower() in (p.description or "").lower():
                s += 3
            if service_line and service_line.lower() in (p.category or "").lower():
                s += 5
            if tech and any(tech.lower() in t.lower() for t in p.tech):
                s += 4
            if p.client:
                s += 2   # a named client is stronger proof than an unnamed build
            return s

        best = max(pool, key=score)
        return best if score(best) > 0 else pool[0]

    # ── grounding-validator inputs ──
    def known_names(self) -> set[str]:
        """Every proper noun an output is allowed to assert as ours."""
        names = {self.brand_name}
        for p in self.projects:
            if p.name:
                names.add(p.name)
            if p.client:
                names.add(p.client)
        for o in self.open_source:
            if o.get("name"):
                names.add(o["name"])
        for t in self.testimonials:
            if t.name:
                names.add(t.name)
            if t.company:
                names.add(t.company)
        return names

    def facts_corpus(self) -> str:
        """Every fact as one lowercase blob, for substring checks.

        The grounding gate needs to answer "does this figure appear anywhere in
        the source data?". Matching against a curated set of numbers would miss
        anything phrased differently ("1000+" vs "1,000"), so the gate checks
        the corpus and uses `known_numbers()` only to explain a rejection.
        """
        parts: list[str] = [self.brand_name, self.tagline, str(self.countries_served)]
        parts += [s.get("name", "") + " " + s.get("oneLineDescription", "") for s in self.services]
        for p in self.projects:
            parts += [p.name, p.client, p.client_country, p.industry, p.category,
                      p.description, p.slug, " ".join(p.tech)]
        for o in self.open_source:
            parts += [str(o.get("name", "")), str(o.get("description", "")),
                      str(o.get("downloads", ""))]
        for t in self.testimonials:
            parts += [t.name, t.country, t.company, t.role, t.text]
        for post in self.existing_posts:
            parts += [post.get("title", ""), post.get("description", ""), post.get("slug", "")]
        # Chart values live here too, or the grounding gate would reject the
        # very figures the curated file exists to authorise.
        for stat in self.curated_stats:
            parts += [str(stat.get("value", "")), str(stat.get("label", "")),
                      str(stat.get("source", ""))]
        parts.append(self.playbook_excerpt)
        return " \n".join(x for x in parts if x).lower()

    def chartable_stats(self) -> list[dict]:
        """Curated stats that are safe to publish.

        Three ways a stat is dropped, and all three are silent by design — an
        unusable stat means one fewer chart, never a failed run:

          * no readable value (an identifier this reader could not resolve)
          * an `industry` figure with no publisher or no URL, which would be
            published uncited — the same rule the trend layer applies to any
            external number, applied to a hand-maintained file
          * anything else with no label to put under it

        Our own figures need no attribution because the repo *is* the
        attribution.
        """
        out = []
        for stat in self.curated_stats:
            if stat.get("value") is None or not stat.get("label"):
                continue
            if stat.get("scope") == "ours":
                out.append(stat)
            elif stat.get("source") and stat.get("source_url"):
                out.append(stat)
        return out

    def stats_block(self) -> str:
        """The chartable figures, as a prompt block. Empty when there are none.

        Empty is a normal state and the caller must handle it: with no stats
        the chart recipes are simply not eligible, which is the correct
        behaviour and much better than a model filling in plausible numbers.
        """
        stats = self.chartable_stats()
        if not stats:
            return ""
        lines = [
            "FIGURES YOU MAY PUT IN A CHART (these and no others - a chart value",
            "that does not appear here is rejected exactly like an invented",
            "statistic in prose):",
        ]
        for s in stats:
            cite = ""
            if s.get("scope") == "industry":
                cite = f"  [source: {s.get('source')}, {s.get('as_of', 'undated')}]"
            lines.append(f"  - {s['value']}{s.get('unit', '')} {s.get('label', '')}{cite}")
        return "\n".join(lines)

    def numbers_corpus(self) -> str:
        """The corpus a published FIGURE may come from — narrower than `facts_corpus`.

        Everything except `playbook_excerpt`, and that exclusion is the whole
        point. `details.md` is 71 KB of brand, voice and SEO guidance, and it is
        full of incidental numbers: section numbers, word-count targets, worked
        examples. Including it put **108 of 121** numbers in the grounding
        corpus, among them 47, 15 and 78 — so "we increased conversions by 47%"
        and "commissions dropped to 15%" both passed a gate whose entire purpose
        is to stop them. Measured, not theorised: `tools/e2e.py` caught both.

        Names still check against the full corpus. A project mentioned only in
        the playbook is a real project; a number mentioned only there is not a
        publishable fact.
        """
        parts: list[str] = [str(self.countries_served), str(len(self.testimonials))]
        if self.founding_year:
            parts.append(self.founding_year)
        for p in self.projects:
            parts.append(p.description)
        for o in self.open_source:
            parts += [str(o.get("downloads", "")), str(o.get("description", ""))]
        for t in self.testimonials:
            parts.append(t.text)
        for post in self.existing_posts:
            parts += [post.get("title", ""), post.get("description", "")]
        for stat in self.curated_stats:
            parts.append(str(stat.get("value", "")))
        return " \n".join(x for x in parts if x).lower()

    def known_numbers(self) -> set[str]:
        """Every figure that appears in the source data, as written.

        The grounding gate compares numbers found in a draft against this set.
        It is intentionally generous — the goal is catching an invented
        "we increased conversions 47%", not policing "3 steps".
        """
        numbers: set[str] = {str(self.countries_served), str(len(self.testimonials))}
        if self.founding_year:
            numbers.add(self.founding_year)
        for o in self.open_source:
            for n in re.findall(r"\d[\d,]*\+?", str(o.get("downloads", ""))):
                numbers.add(n)
        for p in self.projects:
            for n in re.findall(r"\d[\d,]*", p.description or ""):
                numbers.add(n)
        return numbers

    # ── prompt-ready rendering ──
    def to_prompt_block(
        self,
        max_playbook_chars: int = 12000,
        include_posts: bool = True,
        include_playbook: bool = True,
        include_testimonials: bool = False,
    ) -> str:
        """A compact, human-readable block to inject as grounding facts."""
        lines: list[str] = []
        # The model has no reliable sense of "now" and will otherwise default to
        # its training-era year — which is exactly how two published posts ended
        # up titled "...in 2025" while being published in July 2026. State the
        # date explicitly and say which year any year-reference must use.
        today = date.today()
        lines.append(f"TODAY'S DATE: {today.isoformat()}")
        lines.append(
            f"CURRENT YEAR: {today.year}. Any year mentioned in a title, heading, slug, "
            f"or statement about current prices/trends must be {today.year}. Never write "
            f"{today.year - 1} as though it were the current year."
        )
        lines.append("")
        lines.append(f"BRAND: {self.brand_name} - {self.tagline}")
        lines.append(f"SITE: {self.url}")
        if self.locality:
            lines.append(f"BASED IN: {self.locality}, {self.region}, {self.country}")
        if self.projects or self.testimonials:
            lines.append(
                f"CLIENTS IN {self.countries_served} COUNTRIES: "
                + ", ".join(self.served_countries)
            )
        lines.append("")
        lines.append("SERVICES (canonical names + slugs - only these labels/paths exist):")
        for s in self.services:
            lines.append(f"  - {s['name']} (/services/{s['slug']}): {s.get('oneLineDescription','')}")
        lines.append("")
        lines.append("REAL PROJECTS you may reference by name (do NOT invent others; do NOT")
        lines.append("claim live/store status for entries marked [no-status]):")
        for p in self.projects:
            tag = " [no-status]" if p.hide_status else ""
            loc = f" - {p.client}, {p.client_country}" if p.client else ""
            path = f" (/work/{p.slug})" if p.slug else ""
            ind = f" | {p.industry}" if p.industry else ""
            lines.append(f"  - {p.name} [{p.category}{ind}]{loc}{path}{tag}: {p.description}")
            if p.tech:
                lines.append(f"      tech: {', '.join(p.tech)}")
        lines.append("")
        if self.open_source:
            lines.append("OPEN SOURCE (real, on /open-source):")
            for o in self.open_source:
                dl = f" - {o['downloads']} downloads" if o.get("downloads") else ""
                lines.append(f"  - {o['name']}{dl}: {o.get('description','')}")
            lines.append("")
        if include_testimonials and self.testimonials:
            lines.append("REAL CLIENT TESTIMONIALS (verbatim - never reword into a claim,")
            lines.append("never attribute a quote to anyone not listed here):")
            for t in self.testimonials:
                who = f"{t.name}, {t.country}"
                if t.role:
                    who += f" ({t.role}"
                    who += f", {t.company})" if t.company else ")"
                lines.append(f'  - {who} [{t.platform}]: "{t.text}"')
            lines.append("")
        # Conversion + reference pages. Without this the model only knows about
        # /services, /work and /blog, so output never links to the pages that
        # actually convert.
        lines.append("OTHER PAGES YOU MAY LINK TO (these exist; do not invent others):")
        lines.append("  - /get-started: the free-prototype offer + request form (best CTA target)")
        lines.append("  - /pricing: how fixed-scope pricing works vs hourly/retainer/freelancer")
        lines.append("  - /faq: NDAs, ownership, invoicing currency, support after handover")
        lines.append("  - /testimonials: all real client messages")
        lines.append("  - /contact, /about, /work, /blog, /open-source")
        lines.append("")
        if include_posts and self.existing_posts:
            lines.append("EXISTING BLOG POSTS (real, publishable links):")
            for post in self.existing_posts:
                lines.append(f"  - /blog/{post['slug']}: {post['title']}")
        if include_playbook and self.playbook_excerpt:
            lines.append("")
            lines.append("BRAND/SEO PLAYBOOK (voice, USPs, guardrails - follow these):")
            lines.append(_playbook_sections(self.playbook_excerpt, max_playbook_chars))
        return "\n".join(lines)


# ── playbook extraction ──
# details.md is ~71k chars of full project reference. Naively truncating it to
# the first N characters delivers tone-of-voice and the USPs but cuts everything
# from roughly section 6 onward — including the integrity rules and the
# per-project confidentiality boundaries, which sit near char 47,000. The writer
# is then never shown the rules it most needs to follow.
#
# So pull the sections a writer actually needs, by heading, in priority order,
# and spend the character budget on those instead of on whatever comes first.
#
# Order is priority order, NOT document order: the rules that must never be
# broken come first. A post in slightly-off tone is a quality problem; a post
# that leaks a client's internals is a trust problem.
_PLAYBOOK_SECTIONS = [
    "### 20.1 Work / portfolio (`src/data/projects.ts`)",   # integrity + confidentiality
    "## 21. Conventions & guardrails",
    "### 2.4 Tone of voice",
    "### 2.3 Brand positioning",
    "### 2.2 Vision, mission, values",
    "## 3. Unique Selling Propositions (USPs)",
    "### 2.6 What differentiates WizCodes from traditional software companies",
    "## 4. The Free Prototype offering",
    "### 2.5 Target audience",
]


def _playbook_sections(playbook: str, budget: int) -> str:
    """Return the writer-relevant sections of details.md, within `budget` chars.

    Falls back to plain truncation if the headings cannot be found, so a
    reformatted details.md degrades rather than silently emitting nothing.
    """
    out: list[str] = []
    used = 0
    for heading in _PLAYBOOK_SECTIONS:
        start = playbook.find(heading)
        if start == -1:
            continue
        after = playbook[start + len(heading):]
        ends = [after.find(m) for m in ("\n## ", "\n### ") if after.find(m) != -1]
        end = min(ends) if ends else len(after)
        block = (heading + after[:end]).strip()
        if used + len(block) > budget:
            break
        out.append(block)
        used += len(block)
    return "\n\n".join(out) if out else playbook[:budget].strip()


# ── extraction helpers ──
#
# CHANGED FROM THE INHERITED VERSION, and the reason matters.
#
# The blog agent split these files with `\{[^{}]*?id:\s*'[^']+'[^{}]*?\}` — a
# flat regex that cannot match an object containing a nested object. Measured
# against the live site repo on 6 Aug 2026, that found **1 of the 5** entries in
# `openSource.ts`: only Cartoon Diffusion, silently dropping ClarivueXAI,
# Bulkhead, Cyber Bug Bounty Agent and Chrome Extensions.
#
# (projects, testimonials and posts happened to extract completely — their
# entries carry no nested objects today. That is luck, not design: the first
# project entry to gain a nested field would have vanished the same way.)
#
# Nothing errors when this happens. The snapshot just quietly claims WizCodes
# has one open-source project, and the grounding gate then *rejects* a post
# truthfully mentioning ClarivueXAI while passing anything about the one entry
# it can see. A grounding check built on a silently incomplete corpus is worse
# than no check at all, because it is trusted.
#
# So the splitter below is brace-balanced and string/comment aware. The field
# extractors are unchanged — they were never the problem.


def _object_blocks(text: str) -> list[str]:
    """Every balanced top-level `{...}` literal, ignoring strings and comments.

    Depth is counted from zero at the file level, so in the usual
    `export const x = [ {...}, {...} ]` shape each array element comes back
    whole, with its nested objects intact inside it.
    """
    blocks: list[str] = []
    depth = 0
    start = -1
    i = 0
    n = len(text)
    quote = ""          # active string delimiter, or ""
    comment = ""        # "line" | "block" | ""
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if comment == "line":
            if ch == "\n":
                comment = ""
        elif comment == "block":
            if ch == "*" and nxt == "/":
                comment = ""
                i += 1
        elif quote:
            if ch == "\\":
                i += 1              # skip the escaped character
            elif ch == quote:
                quote = ""
        elif ch == "/" and nxt == "/":
            comment = "line"
            i += 1
        elif ch == "/" and nxt == "*":
            comment = "block"
            i += 1
        elif ch in "'\"`":
            quote = ch
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                blocks.append(text[start : i + 1])
                start = -1
            elif depth < 0:
                depth = 0           # unbalanced source: resync rather than crash
        i += 1
    return blocks


_NESTED_OBJ = re.compile(r"\{[^{}]*\}")


def _own_fields(block: str) -> str:
    """The block with nested *objects* blanked out, arrays preserved.

    A nested `{ ... name: 'x' ... }` would otherwise let a child's field be read
    as the parent's, which is how a project ends up reporting its sub-object's
    name. Arrays stay because `tech: [...]` and `tags: [...]` are real fields.
    """
    inner = block[1:-1] if block.startswith("{") and block.endswith("}") else block
    prev = None
    while prev != inner:                       # collapse innermost outward
        prev = inner
        inner = _NESTED_OBJ.sub(" ", inner)
    return inner


def _extract_services(site_ts: str) -> list[dict]:
    services = []
    for m in re.finditer(
        r"\{\s*name:\s*'([^']+)',\s*slug:\s*'([^']+)',\s*oneLineDescription:\s*\n?\s*'([^']+)'",
        site_ts,
    ):
        services.append({"name": m.group(1), "slug": m.group(2), "oneLineDescription": m.group(3)})
    return services


def _extract_scalar(site_ts: str, key: str) -> str:
    m = re.search(rf"{key}:\s*'([^']*)'", site_ts)
    return m.group(1) if m else ""


def _extract_projects(projects_ts: str) -> list[ProjectFact]:
    """Split the file into object literals and pull durable fields from each."""
    projects: list[ProjectFact] = []
    seen: set[str] = set()
    for block in _object_blocks(projects_ts):
        text = _own_fields(block)
        if not re.search(r"\bid:\s*'[^']+'", text):
            continue
        pf = ProjectFact()
        pf.id = _f(text, "id")
        pf.name = _f(text, "name")
        pf.category = _f(text, "category")
        pf.industry = _f(text, "industry")
        pf.client = _f(text, "client")
        pf.client_country = _f(text, "clientCountry")
        pf.description = _f(text, "description")
        # The site derives `slug: p.slug ?? p.id` at runtime, so most entries
        # carry no literal slug in the source text. Mirror that fallback —
        # otherwise this regex finds only the handful of hardcoded slugs and the
        # writer believes the other project pages do not exist, so it can never
        # link to them.
        pf.slug = _f(text, "slug") or pf.id
        pf.hide_status = "hideStatus: true" in text
        tech_m = re.search(r"tech:\s*\[([^\]]*)\]", text)
        if tech_m:
            pf.tech = [t.strip().strip("'\"") for t in tech_m.group(1).split(",") if t.strip()]
        if pf.name and pf.id not in seen:
            seen.add(pf.id)
            projects.append(pf)
    return projects


def _extract_open_source(oss_ts: str) -> list[dict]:
    out, seen = [], set()
    for block in _object_blocks(oss_ts):
        text = _own_fields(block)
        name = _f(text, "name")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(
            {
                "name": name,
                "description": _multiline_field(text, "description"),
                "downloads": _f(text, "downloads"),
                "slug": _f(text, "slug"),
                "status": _f(text, "status"),
            }
        )
    return out


def _extract_testimonials(ts: str) -> list[TestimonialFact]:
    """Client messages, for the `client_voice` pillar.

    The quotes are double-quoted in the source (they contain apostrophes), so
    this cannot reuse `_f`, which reads single-quoted values.
    """
    out: list[TestimonialFact] = []
    for block in _object_blocks(ts):
        text = _own_fields(block)
        if not re.search(r"\bid:\s*\d+", text):
            continue
        quote = re.search(r'text:\s*"((?:[^"\\]|\\.)*)"', text)
        name = _f(text, "name")
        if not (name and quote):
            continue
        out.append(
            TestimonialFact(
                name=name,
                country=_f(text, "country"),
                platform=_f(text, "platform"),
                text=quote.group(1).replace('\\"', '"'),
                role=_f(text, "role"),
                company=_f(text, "company"),
            )
        )
    return out


def _f(text: str, key: str) -> str:
    m = re.search(rf"{key}:\s*'([^']*)'", text)
    return m.group(1) if m else ""


def _extract_curated_stats(stats_ts: str) -> list[dict]:
    """Figures a chart may draw, from `src/data/socialStats.ts`.

    Two shapes of `value` have to survive here, and only one of them is a
    number in the source text:

        value: 26,                  a literal
        value: countriesServed,     an identifier computed by the site

    The identifier form is the *preferred* one — it is what stops a chart
    contradicting the same figure printed on the website — and this reader never
    executes TypeScript, so it cannot resolve it. Such a stat is returned with
    `value: None` and a `ref`, and the caller drops it: a chart is better absent
    than drawn from a number nobody can check.

    That is why `chartable_stats()` filters. A stat whose value cannot be read
    is exactly as unusable as one with no source.
    """
    out: list[dict] = []
    for block in _object_blocks(stats_ts):
        text = _own_fields(block)
        stat_id = _f(text, "id")
        if not stat_id:
            continue
        raw = re.search(r"\bvalue:\s*([^,\n]+)", text)
        literal = None
        ref = ""
        if raw:
            token = raw.group(1).strip()
            if re.fullmatch(r"-?\d+(?:\.\d+)?", token):
                literal = float(token) if "." in token else int(token)
            else:
                ref = token
        out.append(
            {
                "id": stat_id,
                "label": _f(text, "label"),
                "value": literal,
                "ref": ref,
                "unit": _f(text, "unit"),
                "scope": _f(text, "scope") or "ours",
                "source": _f(text, "source"),
                "source_url": _f(text, "sourceUrl"),
                "as_of": _f(text, "asOf"),
            }
        )
    return out


def _extract_posts(posts_ts: str) -> list[dict]:
    out, seen = [], set()
    for block in _object_blocks(posts_ts):
        text = _own_fields(block)
        slug = _f(text, "slug")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        tags_m = re.search(r"tags:\s*\[([^\]]*)\]", text)
        tags = (
            [t.strip().strip("'\"") for t in tags_m.group(1).split(",") if t.strip()]
            if tags_m
            else []
        )
        out.append(
            {
                "slug": slug,
                "title": _f(text, "title"),
                "description": _multiline_field(text, "description"),
                "tags": tags,
                "date": _f(text, "date"),
            }
        )
    return out


def _multiline_field(text: str, key: str) -> str:
    """description often spans lines: description:\\n  '...'."""
    m = re.search(rf"{key}:\s*\n?\s*'([^']*)'", text)
    return m.group(1) if m else _f(text, key)


# `socialStats.ts` derives its own figures from the site's data rather than
# hardcoding them, which is the property that stops a chart contradicting the
# number printed on the website. The cost is that the values are identifiers,
# and this reader never executes TypeScript.
#
# So the handful of identifiers it uses are resolved here, from the same two
# sources the site derives them from. That is not a workaround — `stats.ts`
# already computes `countriesServed` exactly this way, and `served_countries`
# above already mirrors it for the same reason. An identifier not in this map
# resolves to None and its stat is dropped, which is the safe direction:
# a chart is better absent than drawn from a number nobody can check.
def _resolve_stat_refs(stats: list[dict], snap: FactsSnapshot) -> list[dict]:
    known = {
        "countriesServed": snap.countries_served,
        "servedCountries.length": snap.countries_served,
        "allWork.length": len(snap.projects),
        "projectCount": len(snap.projects),
        "clientCount": len(snap.testimonials),
        "testimonials.length": len(snap.testimonials),
        "openSourceProjects.length": len(snap.open_source),
    }
    for stat in stats:
        if stat.get("value") is None and stat.get("ref"):
            stat["value"] = known.get(stat["ref"])
    return stats


def build_snapshot(reader: SiteReader | None = None) -> FactsSnapshot:
    """Build the snapshot. One `SiteReader` per run — it caches."""
    reader = reader or SiteReader()
    site_ts = reader.read(SITE_CONFIG)
    posts = _extract_posts(reader.read(POSTS_REGISTRY))
    # Newest first, so "the most recent post" is a question the caller can
    # answer without knowing how the registry happens to be ordered.
    posts.sort(key=lambda p: p.get("date", ""), reverse=True)

    snapshot = FactsSnapshot(
        brand_name=_extract_scalar(site_ts, "brandName") or "WizCodes",
        tagline=_extract_scalar(site_ts, "tagline"),
        url=_extract_scalar(site_ts, "url") or "https://wizcodes.site",
        contact_email=_extract_scalar(site_ts, "contactEmail"),
        locality=_extract_scalar(site_ts, "locality"),
        region=_extract_scalar(site_ts, "region"),
        country=_extract_scalar(site_ts, "country"),
        founding_year=_extract_scalar(site_ts, "foundingDate")[:4],
        services=_extract_services(site_ts),
        projects=_extract_projects(reader.read(PROJECTS)),
        open_source=_extract_open_source(reader.read(OPEN_SOURCE)),
        testimonials=_extract_testimonials(reader.read(TESTIMONIALS)),
        existing_posts=posts,
        playbook_excerpt=reader.read(PLAYBOOK),
    )
    # After construction, because resolving `countriesServed` needs the projects
    # and testimonials that were just read.
    snapshot.curated_stats = _resolve_stat_refs(
        _extract_curated_stats(reader.read(SOCIAL_STATS)), snapshot
    )
    return snapshot


if __name__ == "__main__":
    snap = build_snapshot()
    print(
        f"services={len(snap.services)} projects={len(snap.projects)} "
        f"oss={len(snap.open_source)} testimonials={len(snap.testimonials)} "
        f"posts={len(snap.existing_posts)} countries={snap.countries_served} "
        f"playbook_chars={len(snap.playbook_excerpt)}"
    )
    print("=" * 70)
    print(snap.to_prompt_block(include_testimonials=True)[:3000])
