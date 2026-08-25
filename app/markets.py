"""Market definitions. A market is the metro we sweep and the shape of "local".

Only the metros we actually work are enumerated. An unknown market resolves to a
permissive spec whose locality test is advisory rather than blocking, because a
gate should not fail a prospect over a boundary we never drew.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MarketSpec:
    name: str
    state: str | None = None
    cities: frozenset[str] = field(default_factory=frozenset)
    area_codes: frozenset[str] = field(default_factory=frozenset)
    # True when we enumerated the metro and can fail on a locality miss.
    boundaries_known: bool = True

    def query_market(self) -> str:
        return f"{self.name}, {self.state}" if self.state else self.name

    def in_metro(self, city: str | None, state: str | None) -> bool | None:
        """True, False, or None when we cannot tell."""
        if not self.boundaries_known:
            return None
        if state and self.state and state.strip().upper() != self.state.upper():
            return False
        if not city:
            return None
        return city.strip().lower() in self.cities

    def local_area_code(self, area_code: str | None) -> bool | None:
        if not self.area_codes or not area_code:
            return None
        return area_code in self.area_codes


def _cities(*names: str) -> frozenset[str]:
    return frozenset(n.lower() for n in names)


COLORADO_SPRINGS = MarketSpec(
    name="Colorado Springs",
    state="CO",
    cities=_cities(
        "Colorado Springs", "Fountain", "Monument", "Falcon", "Peyton",
        "Manitou Springs", "Woodland Park", "Security-Widefield", "Widefield",
        "Security", "Cimarron Hills", "Black Forest", "Palmer Lake",
        "Green Mountain Falls", "Calhan", "Ellicott", "Fort Carson",
        "Divide", "Cascade", "Gleneagle", "Stratmoor", "Fountain Valley",
        "Rock Creek Park", "Air Force Academy", "Usaf Academy",
    ),
    area_codes=frozenset({"719"}),
)

DENVER = MarketSpec(
    name="Denver",
    state="CO",
    cities=_cities(
        "Denver", "Aurora", "Lakewood", "Arvada", "Westminster", "Thornton",
        "Centennial", "Littleton", "Englewood", "Wheat Ridge", "Golden",
        "Broomfield", "Commerce City", "Northglenn", "Parker", "Castle Rock",
        "Highlands Ranch", "Brighton", "Lone Tree", "Greenwood Village",
        "Edgewater", "Louisville", "Lafayette", "Superior", "Federal Heights",
        "Sheridan", "Glendale", "Morrison", "Bow Mar", "Columbine Valley",
        "Castle Pines", "Erie", "Dacono", "Firestone", "Frederick",
    ),
    area_codes=frozenset({"303", "720", "983"}),
)

FORT_COLLINS = MarketSpec(
    name="Fort Collins",
    state="CO",
    cities=_cities(
        "Fort Collins", "Loveland", "Greeley", "Windsor", "Timnath",
        "Wellington", "Berthoud", "Johnstown", "Severance", "Evans",
        "Laporte", "Estes Park", "Milliken",
    ),
    area_codes=frozenset({"970"}),
)

PUEBLO = MarketSpec(
    name="Pueblo",
    state="CO",
    cities=_cities("Pueblo", "Pueblo West", "Colorado City", "Rye", "Boone", "Avondale"),
    area_codes=frozenset({"719"}),
)

_REGISTRY: dict[str, MarketSpec] = {
    m.name.lower(): m for m in (COLORADO_SPRINGS, DENVER, FORT_COLLINS, PUEBLO)
}


def resolve_market(name: str) -> MarketSpec:
    """Known metro, or a permissive spec that will not fail on locality."""
    key = name.strip().lower()
    if key in _REGISTRY:
        return _REGISTRY[key]
    # Accept "Colorado Springs, CO" and similar.
    head = key.split(",")[0].strip()
    if head in _REGISTRY:
        return _REGISTRY[head]
    state = None
    if "," in name:
        tail = name.split(",")[-1].strip().upper()
        if len(tail) == 2 and tail.isalpha():
            state = tail
    return MarketSpec(name=name.split(",")[0].strip(), state=state, boundaries_known=False)


def known_markets() -> list[str]:
    return sorted(m.name for m in _REGISTRY.values())
