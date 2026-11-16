"""Read-only SQLite access for the agent."""
import sqlite3
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from .policy import FIELDS

DB_PATH = Path(__file__).resolve().parents[1] / 'fraud.db'


def connect(*, read_only=True):
    uri = Path(DB_PATH).resolve().as_uri() + ('?mode=ro' if read_only else '?mode=rw')
    conn = sqlite3.connect(uri, uri=True, timeout=0.25)
    conn.row_factory = sqlite3.Row
    if read_only:
        conn.execute('PRAGMA query_only=ON')
    return conn


def get_user(user_id):
    with closing(connect()) as conn:
        row = conn.execute(
            'SELECT user_id, full_name, role, region FROM users WHERE user_id = ?', (user_id,)
        ).fetchone()
    if row is None:
        raise LookupError('unknown identity')
    return dict(row)


def schema_info():
    with closing(connect()) as conn:
        return {table: {row['name']: row['type'] for row in conn.execute(f'PRAGMA table_info({table})')}
                for table in FIELDS}


@dataclass
class Execution:
    rows: list
    started: bool
    error: str | None = None
    attempted: bool = True
    completed: bool = False


@contextmanager
def snapshot():
    """Related reports see one read-only SQLite snapshot."""
    with closing(connect()) as conn:
        conn.execute('BEGIN')
        yield conn


def execute_plan(plan, *, connection=None):
    deadline = monotonic() + 2
    started = False
    attempted = False
    def trace_started(_expanded_sql):
        # SQLite may start stepping before execute() returns a cursor. Never
        # retain the expanded trace string: it contains unredacted bindings.
        nonlocal started
        started = True
    try:
        with (closing(connect()) if connection is None else nullcontext(connection)) as conn:
            try:
                conn.set_progress_handler(lambda: monotonic() >= deadline, 1000)
                conn.set_trace_callback(trace_started)
                attempted = True
                cursor = conn.execute(plan.sql, plan.params)
                return Execution([dict(row) for row in cursor.fetchmany(plan.cap + 1)], started,
                                 attempted=attempted, completed=True)
            finally:
                conn.set_trace_callback(None)
                conn.set_progress_handler(None, 0)
    except sqlite3.Error as exc:
        code = 'query_timeout' if getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT else 'database_unavailable'
        return Execution([], started, code, attempted=attempted)
