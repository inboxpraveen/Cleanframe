"""Allow ``python -m cleanframe`` as an alias for the ``cleanframe`` console script."""

from .cli import main

raise SystemExit(main())
