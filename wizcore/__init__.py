"""Shared invariants for the WizCodes multi-agent system.

Import surface is deliberately explicit — `from wizcore.db.identity import
entity_key` rather than `from wizcore import entity_key`. The prefix is doing
real work: at every call site it answers "is this shared, or is this mine?"
without anyone having to remember.
"""

__version__ = "0.1.0"
