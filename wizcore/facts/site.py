"""Read-only access to the WizCodes site repo.

The site repo is the single source of truth for what WizCodes has actually
built. None of the three agents in this system may ever write to it — they hold
`SITE_READ_TOKEN` (Contents: Read), not the publishing token the blog and case
study agents use.

Two backends, tried in order:

  1. **A local directory**, if one is given and looks like the site. This is the
     build-out path: point at the sibling `wizcodes_next` checkout and iterate
     without spending API calls or needing a token at all.
  2. **The GitHub Contents API.** This is the production path. A shallow clone
     would also work, but the snapshot needs five files and a clone pulls a
     whole repo into a container that is about to be thrown away.

Reads are cached for the lifetime of the reader — "cached per run", which is
what stops a graph with nine nodes making nine copies of the same five calls.
"""
from __future__ import annotations

import logging
from pathlib import Path

import requests

from wizcore.config import env_str

log = logging.getLogger("wizcore.facts.site")

_API = "https://api.github.com"

# Everything the snapshot is built from. Listed here so one glance answers
# "what does an agent actually read out of the website?".
SITE_FILES = (
    "src/config/site.ts",
    "src/data/projects.ts",
    "src/data/openSource.ts",
    "src/data/testimonials.ts",
    "src/content/blog/posts.ts",
    "details.md",
)


class SiteReadError(RuntimeError):
    """The site repo could not be read at all — not merely a missing file."""


class SiteReader:
    def __init__(
        self,
        repo: str | None = None,
        token: str | None = None,
        ref: str = "main",
        local_dir: str | Path | None = None,
        timeout: int = 20,
    ):
        self.repo = repo or env_str("SITE_REPO", "dkcodes121617/wizcodes_main_website")
        self.token = token or env_str("SITE_READ_TOKEN")
        self.ref = ref or env_str("SITE_REF", "main")
        self.timeout = timeout
        self._cache: dict[str, str] = {}

        # Only trust a local dir that actually looks like the site, so a stale
        # or half-checked-out folder falls through to the API instead of
        # producing a snapshot with no projects in it — which would read as
        # "WizCodes has built nothing" and is worse than an error.
        self.local_dir: Path | None = None
        candidate = local_dir or env_str("SITE_LOCAL_DIR")
        if candidate:
            p = Path(candidate)
            if (p / "src/config/site.ts").exists():
                self.local_dir = p
                log.info("site facts from local dir %s", p)

    def read(self, rel_path: str) -> str:
        """File contents, or '' if that file does not exist.

        A missing optional file (details.md on a fork, say) must degrade rather
        than abort — the snapshot renders whatever it did find.
        """
        if rel_path in self._cache:
            return self._cache[rel_path]
        text = self._read_local(rel_path) if self.local_dir else self._read_api(rel_path)
        self._cache[rel_path] = text
        return text

    def _read_local(self, rel_path: str) -> str:
        assert self.local_dir is not None
        p = self.local_dir / rel_path
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8", errors="replace")

    def _read_api(self, rel_path: str) -> str:
        if not self.token:
            raise SiteReadError(
                "SITE_READ_TOKEN is not set and no local site directory was found. "
                "Grounding facts cannot be built, and an ungrounded run must not publish."
            )
        url = f"{_API}/repos/{self.repo}/contents/{rel_path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            # Raw beats the default base64 JSON: no decode step, and no 1 MB
            # base64 ceiling to think about.
            "Accept": "application/vnd.github.raw",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "wizcodes-agent",
        }
        try:
            resp = requests.get(
                url, headers=headers, params={"ref": self.ref}, timeout=self.timeout
            )
        except requests.RequestException as e:
            raise SiteReadError(f"site repo unreachable: {e}") from e

        if resp.status_code == 404:
            log.warning("site file missing: %s", rel_path)
            return ""
        if resp.status_code in (401, 403):
            raise SiteReadError(
                f"SITE_READ_TOKEN rejected ({resp.status_code}) reading {rel_path}. "
                "Check the PAT has Contents: Read on this repo and has not expired."
            )
        if resp.status_code != 200:
            raise SiteReadError(f"HTTP {resp.status_code} reading {rel_path}: {resp.text[:200]}")
        return resp.text

    def prefetch(self) -> None:
        """Warm the cache for every snapshot input in one place.

        Worth doing at the top of a run: it turns a token problem into an
        immediate, obvious failure instead of one that surfaces at node seven
        after the run has already spent budget.
        """
        for rel in SITE_FILES:
            self.read(rel)
