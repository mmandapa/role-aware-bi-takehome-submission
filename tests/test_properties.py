"""Generated requirements properties; failures shrink to reproducible examples."""
import contextlib
import itertools
import json
import statistics
import unittest
from unittest.mock import patch

from hypothesis import given, settings, strategies as st

import seed
from agent import baseline, db
import test_repairs as helpers


class Properties(unittest.TestCase):
    setUp = helpers.Gaps.setUp

    @settings(max_examples=60, deadline=None, derandomize=True)
    @given(st.lists(st.lists(st.one_of(st.none(), st.integers(0, 2_000_000)),
                            min_size=1, max_size=12), min_size=1, max_size=6))
    def test_protected_means_against_independent_population_oracle(self, populations):
        with contextlib.closing(db.connect(read_only=False)) as conn, conn:
            # Nullable schema variant, isolated from the assignment fixture.
            conn.execute('CREATE TABLE replacement AS SELECT * FROM customers WHERE 0')
            conn.execute('DROP TABLE customers')
            conn.execute('ALTER TABLE replacement RENAME TO customers')
            for group, incomes in enumerate(populations):
                for index, income in enumerate(incomes):
                    row = dict(zip([c[1] for c in conn.execute('PRAGMA table_info(customers)')], seed.CUSTOMERS[0]))
                    row.update(customer_id=f'g{group}c{index}', sex=f'group-{group}',
                               annual_income_usd=income, deleted_at=None)
                    conn.execute('INSERT INTO customers VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
        counts = [len(group) for group in populations]
        visible = [i for i, count in enumerate(counts) if count >= 3]
        hidden = [count for count in counts if count < 3]
        contributors = [[value for value in group if value is not None] for group in populations]
        alternatives = sum(sum(candidate) == sum(hidden)
                           for candidate in itertools.product((1, 2), repeat=len(hidden)))
        permitted = (sum(counts) >= 10 and bool(visible) and (not hidden or alternatives > 1)
                     and sum(map(len, contributors)) >= 10
                     and all(len(contributors[i]) >= 3 for i in visible))
        intent = {'action': 'report', 'source': 'customers', 'group_by': ['sex'],
                  'metrics': ['avg_annual_income_usd'], 'chart': True}
        model = helpers.Planner(intent)
        result = baseline.run('Average income by sex', 'u_fern', client=model)
        if not permitted:
            self.assertEqual(result.status, 'disclosure_denied')
            self.assertFalse(result.released_results)
            self.assertFalse(result.charts)
        else:
            self.assertEqual(result.status, 'ok')
            rows = result.released_results[0]['rows']
            self.assertEqual([r['sex'] for r in rows], [f'group-{i}' for i in visible])
            for row, i in zip(rows, visible):
                self.assertAlmostEqual(row['avg_annual_income_usd'], statistics.mean(contributors[i]))
                self.assertEqual(set(row), {'sex', 'avg_annual_income_usd'})
            payload = json.dumps([result.answer, result.released_results, result.charts])
            for i in set(range(len(counts))) - set(visible):
                self.assertNotIn(f'group-{i}', payload)
            self.assertEqual(result.charts[0]['data']['values'], rows)
        self.assertNotIn('group-', json.dumps(model.requests))

    @settings(max_examples=75, deadline=None, derandomize=True)
    @given(st.one_of(st.none(), st.booleans(), st.integers(), st.lists(st.integers(), max_size=5),
                     st.dictionaries(st.text(max_size=8), st.integers(), max_size=3),
                     st.just(''), st.text(min_size=65, max_size=70)),
           st.sampled_from(('revise', 'complete')))
    def test_invalid_field_shape_never_executes_or_replaces_a_confirmed_report(self, field, action):
        baseline.reset_state()
        plan = {'action': 'report', 'source': 'customers', 'metrics': ['count']}
        baseline.run('Count', 'u_ana', 'c', client=helpers.Planner(plan))
        if action == 'complete':
            baseline.run('Clarify', 'u_ana', 'c', client=helpers.Planner({
                'action': 'clarify', 'topic': 'generic',
                'draft': {'source': 'customers', 'metrics': ['count']}}))
        response = {'action': action, 'changes': {'filters': [{'field': field, 'op': 'eq', 'value': 'WEST'}]}}
        result = baseline.run('Change the report', 'u_ana', 'c', client=helpers.Planner(response))
        self.assertEqual(result.status, 'invalid_request')
        self.assertEqual(result.sql_log, [])
        self.assertTrue(result.audit)
        self.assertEqual(baseline.run('repeat that', 'u_ana', 'c').released_results[0]['rows'], [{'count': 6}])
