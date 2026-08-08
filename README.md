# wizcore

Shared invariants for the WizCodes multi-agent system — the Content Poster, the
Lead Finder and the Outreach agent.

## Where it lives, and why the ground rule still holds

It sits inside `main_company_folder` at the owner's direction — kept outside, it
would be one folder among a couple of hundred on `D:\` and would get lost.

That is a change to the letter of workspace ground rule 1 ("the root holds agent
folders and nothing else"), so the rule's *purpose* has to be preserved
deliberately rather than by accident. The purpose was: **an agent folder must be
independently cloneable and deployable, with nothing beside it.** Two things keep
that true, and breaking either one breaks the rule for real:

1. `wizcore` is **its own git repo**, not part of any agent's.
2. Agents depend on it as an **installed package**, never by reaching sideways
   at runtime. No `sys.path` insert, no `../wizcore` import, no relative file
   read. The editable install below is a build-out convenience; the pinned git
   dependency is the production form, and both resolve through pip.

If an agent ever imports `wizcore` by path instead of by install, that folder has
quietly stopped being cloneable and the rule is gone.

## The admission test

> **Would two copies of this drifting be a _bug_, or just duplication?**

Bug → it belongs here. Duplication → it belongs in the agent.

That test is decidable without asking anyone, and it is the only thing keeping
this package small enough to stay understandable.

| Here | Why | Stays in the agent |
|---|---|---|
| `db/identity.py` — `entity_key()` | Drift means the same business is pitched twice, silently. No exception fires. | `platforms/facebook.py` — one agent publishes |
| `llm/` — the proxy client | Drift means a 403 nobody can explain six months later | `assess/psi.py` — one agent assesses |
| `facts/` — the site snapshot | Drift means one agent inventing a client in public | `prompts/library.py` — each agent's own voice |
| `db/` — conn, runs, actions, spend | One DB contract, one run log, one idempotency ledger, one budget | Telegram *message formatting* — per agent |
| `telegram/send.py` — transport only | One bot | everything else |

If something in here stops passing that test, take it out.

## Install

During build-out, editable, from each agent's own venv:

```powershell
cd <agent> ; .\.venv\Scripts\python.exe -m pip install -e ..\wizcore
```

Modal picks it up with `.add_local_python_source("wizcore")`.

Once it stabilises, tag it and switch each agent's `requirements.txt` to the
pinned form — pinned by tag, never floating, because a scheduled agent nobody is
watching must not be surprised by an upstream change:

```
wizcore @ git+https://github.com/dkcodes121617/wizcore@v0.1.0
```

## Configuration

`wizcore` reads no config of its own — it reads the **calling agent's**
environment, loaded by `wizcore.config.load_env(AGENT_ROOT)`. These are the
variables it touches:

| Variable | Used by | Notes |
|---|---|---|
| `NEON_DATABASE_URL` | `db/*` | the message bus; required by everything except `llm` and `facts` |
| `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` | `llm` | the proxy |
| `ANTHROPIC_MODEL` | `llm` | default model when a caller does not pass one |
| `SITE_REPO` / `SITE_READ_TOKEN` | `facts` | read-only PAT (Contents: Read) |
| `SITE_LOCAL_DIR` | `facts` | optional; point at a local `wizcodes_next` checkout to skip the API entirely |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | `telegram` | send-only |
| `TELEGRAM_TOPIC_*` | `telegram` | optional forum topics; blank falls back to the main chat |

Nothing here is read at import time, so importing `wizcore` in a context with
no environment is safe.

## The rule that came over from `core_sync.py`

**Never edit anything here to suit one agent.** A specific need is a new module
in that agent's own package. This rule is the valuable part of the mechanism
this package replaces.

## Layout

```
wizcore/
├─ config.py      load_env + the _clean() dotenv-comment fix + typed getters
├─ llm/           client.py (the proxy's quirks) · sanitize.py
├─ facts/         snapshot.py · site.py (GitHub Contents API reader)
├─ db/            conn.py · identity.py · runs.py · actions.py · spend.py
├─ telegram/      send.py  (send-only: no webhook, no callbacks)
└─ obs/           log.py   (JSON lines, every record carrying run_id)
```
