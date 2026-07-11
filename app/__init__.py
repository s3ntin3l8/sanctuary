"""Route the stdlib `sqlite3` name to pysqlite3-binary's bundled SQLite (>= 3.41, the
sqlite-vec KNN floor) BEFORE any other app module imports sqlite3. Must stay first:
app.config registers datetime/date adapters at import time, and stdlib sqlite3 and
pysqlite3 have separate C adapter registries — those adapters land in the wrong one if
this runs late.

Without this, KNN correctness depends on whatever libsqlite3 the host happens to link
(via actions/setup-python's dynamically-linked CPython, a contributor's older Linux box,
etc). sqlite-vec's `LIMIT`-form KNN query requires SQLite >= 3.41 to push LIMIT down to
the vtab query planner; on older SQLite it silently returns wrong/empty results instead
of failing loudly. Bundling SQLite ourselves makes KNN correctness independent of the
host, forever.

Hard import, no fallback: a silent fallback to stdlib sqlite3 would silently reintroduce
the exact host-version-drift bug this exists to close.
"""

import sys

import pysqlite3

sys.modules["sqlite3"] = pysqlite3
sys.modules["sqlite3.dbapi2"] = pysqlite3.dbapi2
