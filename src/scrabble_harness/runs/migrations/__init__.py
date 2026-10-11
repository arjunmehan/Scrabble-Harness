"""Versioned, checksum-verified schema migrations bundled in the wheel."""

import hashlib
import sqlite3
from dataclasses import dataclass
from importlib.resources import files


@dataclass(frozen=True)
class Migration:
    version: int
    sql: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    def statements(self):
        # executescript commits implicitly; split complete statements so DDL stays
        # inside the caller's explicit transaction, including whole trigger bodies.
        pending = ""
        for line in self.sql.splitlines(keepends=True):
            pending += line
            if sqlite3.complete_statement(pending):
                yield pending
                pending = ""
        if pending.strip():
            raise ValueError("migration contains an incomplete SQL statement")


MIGRATIONS = (
    Migration(1, files(__package__).joinpath("001_initial.sql").read_text(encoding="utf-8")),
    Migration(2, files(__package__).joinpath("002_accounting.sql").read_text(encoding="utf-8")),
)
APPLICATION_ID = 0x53435248  # SCRH: reject another application's SQLite database.
