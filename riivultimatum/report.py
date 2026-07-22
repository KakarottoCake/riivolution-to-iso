"""Per-patch outcome reporting.

Every patch the tool considers ends up here with an explicit verdict. Silent
skips are how you end up shipping an ISO that boots to a black screen.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Outcome(enum.Enum):
    APPLIED = "APPLIED"
    #: Patch had an `original` guard that did not match; Riivolution treats
    #: this as a no-op, not an error, and so do we.
    SKIPPED = "SKIPPED"
    #: We understand the patch but cannot represent it in a static ISO.
    UNSUPPORTED = "UNSUPPORTED"
    #: Not fatal, but the user should know: two stacked mods touch the same
    #: file or memory, so the later one silently wins.
    WARNING = "WARNING"
    FAILED = "FAILED"


@dataclass
class Entry:
    outcome: Outcome
    kind: str
    detail: str
    reason: str = ""

    def __str__(self) -> str:
        line = f"  {self.outcome.value:<12} {self.kind:<10} {self.detail}"
        if self.reason:
            line += f"\n  {'':<12} {'':<10} -> {self.reason}"
        return line


@dataclass
class Report:
    entries: list[Entry] = field(default_factory=list)

    def add(self, outcome: Outcome, kind: str, detail: str, reason: str = "") -> None:
        self.entries.append(Entry(outcome, kind, detail, reason))

    def applied(self, kind: str, detail: str, reason: str = "") -> None:
        self.add(Outcome.APPLIED, kind, detail, reason)

    def skipped(self, kind: str, detail: str, reason: str) -> None:
        self.add(Outcome.SKIPPED, kind, detail, reason)

    def unsupported(self, kind: str, detail: str, reason: str) -> None:
        self.add(Outcome.UNSUPPORTED, kind, detail, reason)

    def warned(self, kind: str, detail: str, reason: str) -> None:
        self.add(Outcome.WARNING, kind, detail, reason)

    def failed(self, kind: str, detail: str, reason: str) -> None:
        self.add(Outcome.FAILED, kind, detail, reason)

    def count(self, outcome: Outcome) -> int:
        return sum(1 for e in self.entries if e.outcome is outcome)

    @property
    def has_failures(self) -> bool:
        return self.count(Outcome.FAILED) > 0

    def render(self) -> str:
        lines = ["Patch report:"]
        lines.extend(str(e) for e in self.entries)
        lines.append("")
        lines.append(
            "  {} applied, {} skipped, {} unsupported, {} warning, {} failed".format(
                self.count(Outcome.APPLIED),
                self.count(Outcome.SKIPPED),
                self.count(Outcome.UNSUPPORTED),
                self.count(Outcome.WARNING),
                self.count(Outcome.FAILED),
            )
        )
        return "\n".join(lines)
