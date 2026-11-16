"""Server-owned report capabilities."""
from dataclasses import dataclass
import re
from unicodedata import normalize

VERSION = 5

# A numeric generalization is a different measurement, not an implementation
# of the precise measurement. Require the user to name its basis explicitly.
_BIRTH_YEAR_BASIS = re.compile(r'\b(?:birth[\s_-]*years?|years?\s+of\s+birth)\b', re.IGNORECASE)


def generalized_metric_mismatch(scope, question, metrics):
    return (bool(question) and 'avg_birth_year' in metrics
            and 'dob' not in scope.customer_fields
            and not _BIRTH_YEAR_BASIS.search(question))
T0 = 'customer_id full_name region segment risk_score onboarded_at deleted_at'.split()
T1 = 'zip3 birth_year income_band'.split()
T2 = 'zip_code dob'.split()
T3 = ['annual_income_usd']
T4 = 'national_id email phone'.split()
T5 = 'race ethnicity sex'.split()
FIELDS = {
    'customers': T0 + T1 + T2 + T3 + T4 + T5,
    'transactions': 'txn_id customer_id amount_minor currency channel occurred_at counterparty memo'.split(),
    'alerts': 'alert_id txn_id rule_id severity status'.split(),
    'case_notes': 'note_id alert_id author_id body created_at'.split(),
    'users': 'user_id full_name role region'.split(),
}
NUMERIC = {'risk_score', 'birth_year', 'annual_income_usd', 'amount_minor'}
FIELD_TYPES = {source: {field: 'INTEGER' if field in NUMERIC else 'TEXT' for field in fields}
               for source, fields in FIELDS.items()}
BUSINESS = ('customers', 'transactions', 'alerts')
ROLES = {
    'analyst': (BUSINESS, T0 + T1, True, True),
    'reviewer': (BUSINESS + ('case_notes',), T0 + T1 + T2 + T3, True, True),
    'compliance': (BUSINESS + ('case_notes',), T0 + T1 + T2 + T3 + T4, False, False),
    'fair_lending': (('customers',), ['region', 'segment'] + T1 + T3 + T5, False, True),
    'admin': (('users',), [], False, False),
}

_POSTAL_LABEL = r'\b(?:zip(?:[\s-]*codes?)?(?:\s*\+\s*4)?|postal[\s-]+codes?|post[\s-]*codes?)\b'
_FULL_POSTAL_VALUE = r'\b[0-9]{5}(?:-[0-9]{4})?\b'
_POSTAL_LITERAL_START = r'\s*[:=#]?\s*[\x22\x27\x60]?'
_RESIDENCE_FILTER = (r'\b(?:(?:live|lives|living|reside|resides|residing)\s+in'
                     r'|customers?\s+(?:(?:is|are)\s+)?(?:in|from))\b')
_FULL_POSTAL_FILTER = re.compile(
    rf'{_POSTAL_LABEL}(?:\s+(?:is|of|equals?|equal\s+to|starts?\s+with))?'
    rf'{_POSTAL_LITERAL_START}{_FULL_POSTAL_VALUE}'
    rf'|{_RESIDENCE_FILTER}{_POSTAL_LITERAL_START}{_FULL_POSTAL_VALUE}'
    rf'|{_FULL_POSTAL_VALUE}\s+{_POSTAL_LABEL}',
    re.IGNORECASE,
)
_EXPLICIT_POSTAL_PREFIX = re.compile(
    rf'\b(?:zip3|zip(?:[\s-]+code)?[\s-]+prefix|postal(?:[\s-]+code)?[\s-]+prefix)\b'
    rf'{_POSTAL_LITERAL_START}([0-9]{{3}})\b',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Scope:
    user_id: str
    role: str
    region: str | None
    active_only: bool
    sources: tuple[str, ...]
    customer_fields: tuple[str, ...]

    def fields(self, source):
        return self.customer_fields if source == 'customers' else tuple(FIELDS[source])


def resolve_scope(user):
    role = user['role']
    if role not in ROLES:
        raise ValueError('invalid role')
    sources, columns, scoped, active = ROLES[role]
    region = user['region'] if scoped else None
    if scoped and region not in {'WEST', 'EAST', 'CENTRAL'}:
        raise ValueError('invalid region')
    return Scope(user['user_id'], role, region, active, sources, tuple(columns))


def denies_full_postal_filter(scope, question):
    """Preserve an explicitly requested full ZIP predicate before planning."""
    return ('customers' in scope.sources and 'zip_code' not in scope.customer_fields
            and _FULL_POSTAL_FILTER.search(normalize('NFKC', question)) is not None)


def denies_broadened_postal_filter(scope, question, filters):
    """Catch a plan that turns an ambiguous full postal literal into a ZIP3 filter."""
    if 'zip_code' in scope.customer_fields:
        return False
    proposed = {str(f['value']) for f in filters if f['field'] == 'zip3'}
    if not proposed:
        return False
    text = normalize('NFKC', question)
    full_prefixes = {value[:3] for value in re.findall(_FULL_POSTAL_VALUE, text)}
    explicit_prefixes = set(_EXPLICIT_POSTAL_PREFIX.findall(text))
    return bool(proposed & (full_prefixes - explicit_prefixes))


def protected_cells(rows, *, count_key='count'):
    """Return releasable cells, or None when suppression cannot protect counts."""
    if sum(row[count_key] for row in rows) < 10:
        return None
    visible = [row for row in rows if row[count_key] >= 3]
    hidden = [row[count_key] for row in rows if row[count_key] < 3]
    if not visible:
        return None
    if hidden:
        others = len(hidden) - 1
        total = sum(hidden)
        if max(1, total - 2 * others) == min(2, total - others):
            return None
    return visible
