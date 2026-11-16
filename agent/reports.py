"""Compile bounded report requests and release only authorized results."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from math import isfinite

from . import policy

OPS = {'eq': '=', 'ne': '<>', 'lt': '<', 'lte': '<=', 'gt': '>', 'gte': '>='}
FROM = {
    'customers': 'customers c',
    'transactions': 'transactions t JOIN customers c ON c.customer_id = t.customer_id',
    'alerts': 'alerts a JOIN transactions t ON t.txn_id = a.txn_id JOIN customers c ON c.customer_id = t.customer_id',
    'case_notes': 'case_notes n JOIN alerts a ON a.alert_id = n.alert_id JOIN transactions t ON t.txn_id = a.txn_id JOIN customers c ON c.customer_id = t.customer_id',
    'users': 'users u',
}
ALIASES = dict(zip(FROM, 'ctanu'))
CASE_NOTE_FILTERS = {'txn_id': 't.txn_id', 'rule_id': 'a.rule_id', 'severity': 'a.severity',
                     'counterparty': 't.counterparty', 'channel': 't.channel'}
FORBIDDEN_FILTER_FIELDS = frozenset((*policy.T5, 'memo', 'body'))
METRICS = {
    'customers': {'count': None, **{f'avg_{f}': f for f in ('risk_score', 'birth_year', 'annual_income_usd')}},
    'transactions': {'count': None, 'sum_amount_minor': 'amount_minor', 'avg_amount_minor': 'amount_minor'},
    'alerts': {'count': None}, 'case_notes': {'count': None}, 'users': {'count': None},
}
ENUMS = {
    'region': {'WEST', 'EAST', 'CENTRAL'}, 'currency': {'USD', 'EUR', 'MXN'},
    'status': {'open', 'closed', 'resolved'}, 'channel': {'ach', 'wire', 'card', 'check'},
    'segment': {'retail', 'business'}, 'severity': {'low', 'medium', 'high', 'critical'},
    'income_band': {'lower-middle', 'middle', 'upper-middle', 'high'},
    'rule_id': {'R-SANCTIONS', 'R-STRUCTURING', 'R-VELOCITY', 'R-CARD-TESTING'},
}
RULE_ALIASES = {name.removeprefix('R-').lower(): name for name in ENUMS['rule_id']}
REQUEST_KEYS = {'source', 'mode', 'scope', 'fields', 'group_by', 'metrics', 'filters', 'limit'}


def canonical_value(field, value):
    if field not in ENUMS or not isinstance(value, str):
        return value
    value = value.upper() if field in ('region', 'currency', 'rule_id') else value.lower()
    return RULE_ALIASES.get(value.lower(), value) if field == 'rule_id' else value


class ReportError(ValueError):
    def __init__(self, status, message, reason_code=None):
        super().__init__(message)
        self.status = status
        self.reason_code = reason_code


@dataclass(frozen=True)
class ReportRequest:
    source: str
    mode: str
    scope: str
    fields: tuple
    group_by: tuple
    metrics: tuple
    filters: tuple
    limit: int


@dataclass(frozen=True)
class QueryPlan:
    sql: str
    params: tuple
    safe_params: tuple
    cap: int
    request: ReportRequest
    protected: bool


DATE_FIELDS = {'dob', 'onboarded_at', 'deleted_at'}
TIME_FIELDS = {'occurred_at', 'created_at'}
_NO_PREFIX = policy.NUMERIC | set(ENUMS) | DATE_FIELDS | TIME_FIELDS


def supports_prefix(field):
    return field not in _NO_PREFIX


def major_units(value):
    return Decimal(value).scaleb(-2) if type(value) is int else value / 100


def display_amount(value):
    amount = major_units(value)
    number = float(amount)
    if isinstance(amount, Decimal) and Decimal(str(number)) != amount:
        return format(amount, '.2f')
    return number


def normalize_transaction_dates(filters):
    """Normalize the fixture's UTC date filters to half-open ranges."""
    normalized = []
    for item in filters:
        field = item['field']
        if field not in DATE_FIELDS | TIME_FIELDS:
            normalized.append(item.copy())
            continue
        value, op = item['value'], item['op']
        if value is None and op in ('eq', 'ne'):
            normalized.append(item.copy())
            continue
        try:
            if not isinstance(value, str):
                raise ValueError
            day_only = len(value) == 10 and date.fromisoformat(value).isoformat() == value
            if field in DATE_FIELDS and not day_only:
                raise ValueError
            if day_only:
                stamp = datetime.fromisoformat(value)
            else:
                stamp = datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
                if stamp.isoformat(timespec='seconds') + 'Z' != value:
                    raise ValueError
            step = timedelta(days=1) if day_only else timedelta(seconds=1)
            if op == 'eq':
                terms = [('gte', stamp), ('lt', stamp + step)]
            elif op in ('gt', 'lte'):
                terms = [('gte' if op == 'gt' else 'lt', stamp + step)]
            elif op in ('gte', 'lt') or (op == 'ne' and (not day_only or field in DATE_FIELDS)):
                terms = [(op, stamp)]
            else:
                raise ValueError
        except (ValueError, OverflowError):
            raise ReportError('invalid_request', 'Use valid calendar dates or UTC timestamps for date fields.') from None
        normalized.extend({'field': field, 'op': operator,
                           'value': point.date().isoformat() if field in DATE_FIELDS else point.isoformat(timespec='seconds') + 'Z'}
                          for operator, point in terms)
    for field in DATE_FIELDS | TIME_FIELDS:
        starts = [f['value'] for f in normalized if f['field'] == field and f['op'] == 'gte']
        ends = [f['value'] for f in normalized if f['field'] == field and f['op'] == 'lt']
        if starts and ends and max(starts) >= min(ends):
            raise ReportError('invalid_request', 'The start must be before the end.')
    return tuple(normalized)


def validate_filters(filters):
    """Validate untrusted filter elements before any field lookup or merging."""
    def invalid():
        raise ReportError('invalid_request', 'Invalid report filters.')
    if not isinstance(filters, list) or len(filters) > 10:
        invalid()
    for item in filters:
        if not isinstance(item, dict) or item.keys() != {'field', 'op', 'value'}:
            invalid()
        field, op, value = item['field'], item['op'], item['value']
        if not isinstance(field, str) or not 0 < len(field) <= 64 or op not in (*OPS, 'prefix'):
            invalid()
        if value is not None and not (
            (isinstance(value, str) and len(value) <= 256) or
            (type(value) is int and -(2**63) <= value < 2**63) or
            (type(value) is float and isfinite(value))
        ):
            invalid()
        try:
            field.encode('utf-8')
            if isinstance(value, str):
                value.encode('utf-8')
        except UnicodeEncodeError:
            invalid()


def parse_request(raw):
    def invalid():
        raise ReportError('invalid_request', 'Invalid report arguments.')
    def names(key, maximum):
        values = raw.get(key, [])
        if not isinstance(values, list) or len(values) > maximum:
            invalid()
        if any(not isinstance(value, str) or not 0 < len(value) <= 64 for value in values):
            invalid()
        if len(set(values)) != len(values):
            invalid()
        return tuple(values)

    if not isinstance(raw, dict) or raw.keys() - REQUEST_KEYS:
        invalid()
    source, mode = raw.get('source'), raw.get('mode')
    scope, limit = raw.get('scope', 'default'), raw.get('limit', 100)
    if source not in tuple(FROM) or mode not in ('detail', 'summary') or scope not in ('default', 'own_region', 'company'):
        invalid()
    if type(limit) is not int or not 1 <= limit <= 100:
        invalid()
    fields, groups, metrics = names('fields', 10), names('group_by', 2), names('metrics', 3)
    if source == 'transactions' and 'amount_minor' in fields and 'currency' not in fields:
        fields = (*fields, 'currency')
    if (mode == 'detail' and (not fields or groups or metrics)) or (mode == 'summary' and (fields or not metrics)):
        invalid()
    filters = raw.get('filters', [])
    validate_filters(filters)
    filters = [{**item, 'value': canonical_value(item['field'], item['value'])} for item in filters]
    return ReportRequest(source, mode, scope, fields, groups, metrics, normalize_transaction_dates(filters), limit)


def operation_catalog(scope, source):
    """Describe executable operations using the same policy as authorization."""
    if source not in scope.sources:
        raise ReportError('access_denied', 'Access denied for the requested source.')
    fields = scope.fields(source)
    modes = ['summary'] if scope.role == 'fair_lending' else ['detail', 'summary']
    metrics = [name for name, field in METRICS[source].items()
               if field is None or field in fields]
    protected = [field for field in fields if field in policy.T5]
    return {
        'modes': modes,
        'detail_fields': [field for field in fields if field not in policy.T5]
                         if 'detail' in modes else [],
        'group_by_fields': [field for field in fields if field not in ('memo', 'body')],
        'metrics': metrics,
        'protected_summary': {'group_by_fields': protected, 'max_group_by': 1,
                              'metrics': metrics} if protected else None,
    }


def compile_report(scope, request, *, transaction_ids=None):
    def deny():
        raise ReportError('access_denied', 'Access denied for the requested fields, operation, or population.')

    source = request.source
    if source not in scope.sources or (scope.region and request.scope == 'company'):
        deny()
    if request.scope == 'own_region' and not scope.region:
        raise ReportError('invalid_request', 'This identity has no assigned region.')
    operations = operation_catalog(scope, source)
    if request.mode not in operations['modes']:
        deny()
    fields = {name: f'{ALIASES[source]}.{name}' for name in scope.fields(source)}
    filter_fields = {**fields}
    if source in ('transactions', 'alerts', 'case_notes'):
        filter_fields.update({field: f'c.{field}' for field in ('region', 'deleted_at')
                              if field in scope.customer_fields})
    if source == 'case_notes':
        filter_fields.update(CASE_NOTE_FILTERS)
    if (any(field not in operations['detail_fields'] for field in request.fields) or
        any(field not in operations['group_by_fields'] for field in request.group_by)):
        deny()
    if any(f['field'] not in filter_fields or f['field'] in FORBIDDEN_FILTER_FIELDS for f in request.filters):
        deny()
    metric_catalog = METRICS[source]
    if any(m not in operations['metrics'] for m in request.metrics):
        deny()
    protected = bool(set(request.group_by) & set(policy.T5))
    if protected and (scope.role != 'fair_lending' or
                      len(request.group_by) != operations['protected_summary']['max_group_by']):
        deny()
    # Apply actor scope before aggregating. Filtering an aggregate result cannot
    # remove the contribution of an unauthorized row.
    where, params, safe = [], [], []
    if scope.region:
        where.append('c.region = ?')
        params.append(scope.region)
        safe.append(scope.region)
    if scope.active_only:
        where.append('c.deleted_at IS NULL')
    # This relation is server-authored from already scoped transaction rows;
    # callers cannot submit an IN operator or populate this binding themselves.
    if transaction_ids is not None:
        if source != 'alerts' or len(transaction_ids) > 100:
            raise ReportError('invalid_request', 'Invalid report relation.')
        where.append('a.txn_id IN (' + ','.join('?' for _ in transaction_ids) + ')' if transaction_ids else '0')
        params.extend(transaction_ids)
        safe.extend('[redacted]' for _ in transaction_ids)
    for item in request.filters:
        field, op, value = item['field'], item['op'], item['value']
        expr = filter_fields[field]
        if scope.region and field == 'region':
            if op != 'eq' or value != scope.region:
                deny()
        if scope.active_only and field == 'deleted_at':
            if op != 'eq' or value is not None:
                deny()
            continue  # The mandatory actor predicate already enforces this filter.
        if value is None and op in ('eq', 'ne'):
            where.append(expr + (' IS NULL' if op == 'eq' else ' IS NOT NULL'))
            continue
        if field == 'status' and source == 'alerts':
            expr = f'LOWER({expr})'
        numeric = field in policy.NUMERIC
        if (numeric and type(value) not in (int, float)) or (not numeric and not isinstance(value, str)):
            raise ReportError('invalid_request', 'Filter value has the wrong type.')
        if field in ENUMS and value not in ENUMS[field]:
            raise ReportError('invalid_request', 'Unknown filter category.')
        if op == 'prefix':
            if not supports_prefix(field) or not value:
                raise ReportError('invalid_request', 'Prefix requires a text field and a literal value.')
            # BLOB lengths include embedded NULs, unlike SQLite text lengths.
            where.append(f'substr(CAST({expr} AS BLOB), 1, length(CAST(? AS BLOB))) = CAST(? AS BLOB)')
            params.extend((value, value))
            safe.extend(('[redacted]', '[redacted]'))
        else:
            where.append(f'{expr} {OPS[op]} ?')
            params.append(value)
            safe.append(value if field in ENUMS else '[redacted]')

    # Validate every filter first so an incomplete monetary draft cannot hide a
    # bad or unauthorized filter behind the missing currency.
    if any(metric_catalog[m] == 'amount_minor' for m in request.metrics):
        currencies = [f for f in request.filters if f['field'] == 'currency']
        if 'currency' not in request.group_by and not (
            len(currencies) == 1 and currencies[0]['op'] == 'eq' and currencies[0]['value'] is not None
        ):
            raise ReportError('invalid_request', 'Filter by one currency or group by currency.',
                              'currency_required')

    chosen = request.fields or request.group_by
    # A grouped status uses the same canonical value as a status filter.
    group_exprs = {field: fields[field] for field in request.group_by}
    if source == 'alerts' and 'status' in group_exprs:
        group_exprs['status'] = f"LOWER({fields['status']})"
    select = [f'{group_exprs.get(field, fields[field])} AS {field}' for field in chosen]
    for metric in request.metrics:
        field = metric_catalog[metric]
        if metric == 'count':
            expression = 'COUNT(DISTINCT c.customer_id)' if source == 'customers' else 'COUNT(*)'
        else:
            function = 'SUM' if metric.startswith('sum_') else 'AVG'
            expression = f'{function}({fields[field]})'
            if function == 'SUM':
                expression = f'COALESCE({expression}, 0)'
        select.append(f'{expression} AS {metric}')
    if source == 'customers' and request.mode == 'summary':
        select.append('COUNT(DISTINCT c.customer_id) AS _contributors')
        for metric in request.metrics:
            field = metric_catalog[metric]
            if field:
                select.append(f'COUNT(DISTINCT CASE WHEN {fields[field]} IS NOT NULL THEN c.customer_id END) AS _{metric}')
    if source == 'transactions' and request.mode == 'summary':
        select.append('COUNT(*) AS _records')
    sql = f"SELECT {', '.join(select)} FROM {FROM[source]}"
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    if request.group_by:
        keys = ', '.join(group_exprs.values())
        sql += f' GROUP BY {keys} ORDER BY {keys}'
    elif request.mode == 'detail':
        sql += f' ORDER BY {ALIASES[source]}.{policy.FIELDS[source][0]}'
    cap = 100 if protected else request.limit
    sql += ' LIMIT ?'
    return QueryPlan(sql, (*params, cap + 1), (*safe, cap + 1), cap, request, protected)


def release(scope, plan, execution):
    request = plan.request
    rows = execution.rows
    if execution.error:
        raise ReportError('unavailable', execution.error)
    if request.mode == 'summary' and len(rows) > plan.cap:
        raise ReportError('too_broad', 'Too many groups to report a complete breakdown.')
    # Decide whether the cohort can be released before constructing any tool
    # payload, chart data, or user-facing audit detail.
    notices = []
    if request.source == 'transactions' and request.mode == 'summary' and rows and rows[0].get('_records') == 0:
        notices.append('No matching transactions; count and sum are zero; average unavailable.')
    if plan.protected:
        visible = policy.protected_cells(rows, count_key='_contributors')
        if visible is None:
            raise ReportError('disclosure_denied', 'Privacy rules prevent releasing this breakdown.')
        # A missing metric input is not a contributor to that metric. Keep this
        # independent of displayed counts, so requesting an average alone is safe.
        for metric in request.metrics:
            if METRICS['customers'][metric] is not None:
                key = '_' + metric
                if sum(row[key] for row in rows) < 10 or any(row[key] < 3 for row in visible):
                    raise ReportError('disclosure_denied', 'Privacy rules prevent releasing this breakdown.')
        if len(visible) != len(rows):
            notices.append('Small groups were suppressed.')
        if len(visible) > request.limit:
            raise ReportError('too_broad', 'Too many groups to report a complete breakdown.')
        rows = visible
    elif request.source == 'customers' and request.mode == 'summary':
        if not rows or any(value < 2 for row in rows for key, value in row.items() if key.startswith('_')):
            raise ReportError('disclosure_denied', 'The report does not meet the customer privacy floor.')
    if len(rows) > request.limit:
        notices.append('Sample only. More records match.')
    released = [{key: value for key, value in row.items() if not key.startswith('_')}
                for row in rows[:request.limit]]
    columns = list(request.fields or request.group_by + request.metrics)
    display = [{key: display_amount(value) if key.endswith('amount_minor') and value is not None else value
                for key, value in row.items()} for row in released]
    # Keep each table column a single type while preserving exact cents when
    # a floating-point value cannot represent the amount.
    for key in columns:
        if key.endswith('amount_minor') and any(isinstance(row[key], str) for row in display):
            for row in display:
                if row[key] is not None:
                    row[key] = str(row[key])
    return {'status': 'ok' if released else 'empty', 'source': request.source, 'rows': released,
            'columns': columns, 'notices': notices, 'display_rows': display,
            'dimensions': list(request.group_by), 'metrics': list(request.metrics),
            'units': {key: 'major currency units' if key.endswith('amount_minor') else
                      'USD' if key.endswith('annual_income_usd') else 'count' if key == 'count' else None
                      for key in columns}}
