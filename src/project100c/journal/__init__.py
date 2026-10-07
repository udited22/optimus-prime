"""Append-only, hash-chained event journal (K-01)."""

from project100c.journal.codec import decode, encode
from project100c.journal.store import GENESIS, Journal, JournalRecord

__all__ = ["GENESIS", "Journal", "JournalRecord", "decode", "encode"]
