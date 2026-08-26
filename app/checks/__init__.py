"""Check package.

Importing the package registers every check implementation. The registry is
built by decorator, so a module nobody imports contributes nothing and every
check silently reports "not implemented" instead. Importing here means the
registry is a property of the package rather than of whoever remembered.
"""

from app.checks import onpage as _onpage  # noqa: F401  - registers the checks
from app.checks import rendered as _rendered  # noqa: F401  - registers the checks
from app.checks import speed as _speed  # noqa: F401  - registers the checks
from app.checks import vision as _vision  # noqa: F401  - registers the checks

__all__ = ["_onpage", "_rendered", "_speed", "_vision"]
