"""Scoring. Pure module, no I/O, per engine spec section 7.

This is the file that gets retuned after the first batch, so everything it does
is arithmetic over values handed to it. It never reads Firestore, never looks a
check up, and never decides whether a check passed. It turns outcomes into a
score, a band, and a segment.

The section score is points earned over points *available*, where available
excludes anything skipped or errored, and the result is then normalized to the
section's nominal weight of 30 / 30 / 40. That normalization is what makes a
partial audit comparable to a complete one, and it is also why `partial` has to
travel with the score: a Booked score of 30/40 built on two surviving checks is
arithmetically fine and worthless as a routing signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from app.checks.definitions import BOOKED, CHOSEN, FOUND, MEASUREMENT, SECTION_WEIGHTS

__all__ = [n for n in dir() if not n.startswith('_')]

# ── Statuses ──────────────────────────────────────────────────────────────────

PASS = "pass"
FAIL = "fail"
SKIPPED = "skipped"
ERROR = "error"

VALID_STATUSES = frozenset({PASS, FAIL, SKIPPED, ERROR})

# Skipped and errored both mean "we did not measure this". They are recorded
# separately because one is a policy outcome and the other is a defect, but they
# leave the denominator identically.
NOT_MEASURED = frozenset({SKIPPED, ERROR})

SCORED_SECTIONS = (FOUND, CHOSEN, BOOKED)

# A section this thin is not evidence. Engine spec: partial when more than 20%
# of a section's points went unmeasured.
PARTIAL_THRESHOLD = 0.20

# ── Bands, criteria doc section 4 ─────────────────────────────────────────────

BAND_DIALED = "Dialed"
BAND_TUNED = "Tuned"
BAND_LEAKING = "Leaking"
BAND_BROKEN = "Broken"

BAND_FLOORS: tuple[tuple[int, str], ...] = (
    (85, BAND_DIALED),
    (65, BAND_TUNED),
    (40, BAND_LEAKING),
    (0, BAND_BROKEN),
)

# ── Segments, criteria doc section 4 ──────────────────────────────────────────

SEG_LEAKY_BUCKET = "Leaky Bucket"
SEG_INVISIBLE_PRO = "Invisible Pro"
SEG_BOTH_BROKEN = "Both Broken"
SEG_DIALED = "Dialed"

# Thresholds are on the normalized section scale, Found out of 30 and Booked out
# of 40, exactly as the criteria doc writes them.
FOUND_HIGH = 20.0   # "Found 20+/30"
FOUND_LOW = 15.0    # "Found 15-/30"
BOOKED_LOW = 20.0   # "Booked 20-/40"
BOOKED_HIGH = 28.0  # "Booked 28+/40"

SEGMENT_PRIORITY: dict[str, int] = {
    SEG_LEAKY_BUCKET: 1,
    SEG_INVISIBLE_PRO: 2,
    SEG_BOTH_BROKEN: 3,
    SEG_DIALED: 4,
}

# Anything unsegmented sorts after every named shape without being renamed into
# one. See `segment_for` for why the four signatures do not cover the plane.
UNSEGMENTED_PRIORITY = 99


@dataclass(frozen=True)
class CheckOutcome:
    """One check's result. `points` is the check's full-credit value."""

    code: str
    section: str
    status: str
    points: int

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"{self.code}: unknown status {self.status!r}")

    @property
    def measured(self) -> bool:
        return self.status not in NOT_MEASURED

    @property
    def earned(self) -> int:
        return self.points if self.status == PASS else 0


@dataclass(frozen=True)
class SectionScore:
    name: str
    earned: int
    available: int      # points that were actually measured
    basis: int          # points that were enabled and should have been measured
    nominal: int        # 30 / 30 / 40

    @property
    def unmeasured(self) -> int:
        return self.basis - self.available

    @property
    def normalized(self) -> float:
        """Earned, expressed against the section's nominal weight."""
        if self.available <= 0:
            return 0.0
        return self.earned / self.available * self.nominal

    @property
    def unmeasured_ratio(self) -> float:
        if self.basis <= 0:
            return 1.0
        return self.unmeasured / self.basis

    @property
    def partial(self) -> bool:
        return self.unmeasured_ratio > PARTIAL_THRESHOLD

    @property
    def coverage(self) -> float:
        """Enabled points over nominal points.

        Distinct from `partial`. This says how much of the section we are even
        attempting this week, which a disabled check reduces and a skipped check
        does not. Found sits well under 1.0 while the SERP checks are off.
        """
        if self.nominal <= 0:
            return 1.0
        return self.basis / self.nominal


@dataclass(frozen=True)
class Score:
    found: int
    chosen: int
    booked: int
    found_max: int
    chosen_max: int
    booked_max: int
    total: int
    band: str
    segment: str | None
    partial: bool
    partial_sections: tuple[str, ...] = ()
    sections: Mapping[str, SectionScore] = field(default_factory=dict)

    @property
    def segment_priority(self) -> int:
        return SEGMENT_PRIORITY.get(self.segment or "", UNSEGMENTED_PRIORITY)

    def normalized(self, section: str) -> float:
        row = self.sections.get(section)
        return row.normalized if row else 0.0


def band_for(total: int) -> str:
    for floor, name in BAND_FLOORS:
        if total >= floor:
            return name
    return BAND_BROKEN


def segment_for(found: float, booked: float) -> str | None:
    """The routing decision. Shape, not score.

    The criteria doc names four signatures and they are mutually exclusive, but
    they do not cover the whole plane: a prospect at Found 18 / Booked 24 is
    neither high nor low on either axis and matches none of them. That middle is
    returned as None rather than rounded into the nearest bucket, because the
    segment is what drives call order and a guessed segment is an invented
    number wearing a label. The ranker sorts unsegmented last.
    """
    if found >= FOUND_HIGH and booked <= BOOKED_LOW:
        return SEG_LEAKY_BUCKET
    if found <= FOUND_LOW and booked >= BOOKED_HIGH:
        return SEG_INVISIBLE_PRO
    if found <= FOUND_LOW and booked <= BOOKED_LOW:
        return SEG_BOTH_BROKEN
    if found >= FOUND_HIGH and booked >= BOOKED_HIGH:
        return SEG_DIALED
    return None


def _score_section(name: str, outcomes: Iterable[CheckOutcome]) -> SectionScore:
    rows = [o for o in outcomes if o.section == name]
    return SectionScore(
        name=name,
        earned=sum(o.earned for o in rows),
        available=sum(o.points for o in rows if o.measured),
        basis=sum(o.points for o in rows),
        nominal=SECTION_WEIGHTS[name],
    )


def compute(outcomes: list[CheckOutcome]) -> Score:
    """Outcomes in, Score out. Same input, same score, forever."""
    scored = [o for o in outcomes if o.section != MEASUREMENT]
    sections = {name: _score_section(name, scored) for name in SCORED_SECTIONS}

    total = round(sum(section.normalized for section in sections.values()))
    total = max(0, min(100, total))

    partial_sections = tuple(name for name in SCORED_SECTIONS if sections[name].partial)

    # Never segment on incomplete Booked data. Booked is the entire basis for
    # prioritization, so a thin Booked section means no segment at all rather
    # than a confident one built on two checks.
    if BOOKED in partial_sections:
        segment = None
    else:
        segment = segment_for(sections[FOUND].normalized, sections[BOOKED].normalized)

    return Score(
        found=sections[FOUND].earned,
        chosen=sections[CHOSEN].earned,
        booked=sections[BOOKED].earned,
        found_max=sections[FOUND].available,
        chosen_max=sections[CHOSEN].available,
        booked_max=sections[BOOKED].available,
        total=total,
        band=band_for(total),
        segment=segment,
        partial=bool(partial_sections),
        partial_sections=partial_sections,
        sections=sections,
    )


def outcomes_from(
    statuses: Mapping[str, str],
    definitions: Iterable[Mapping[str, object]],
) -> list[CheckOutcome]:
    """Join check statuses to their definitions.

    A definition with no status is `skipped`, not absent. A check that was
    enabled and produced nothing did not happen, and the score has to know that
    rather than quietly shrinking the denominator.
    """
    out: list[CheckOutcome] = []
    for row in definitions:
        if not row.get("enabled", True):
            continue
        code = str(row["code"])
        out.append(
            CheckOutcome(
                code=code,
                section=str(row["section"]),
                status=statuses.get(code, SKIPPED),
                points=int(row["points"]),  # type: ignore[arg-type]
            )
        )
    return out
