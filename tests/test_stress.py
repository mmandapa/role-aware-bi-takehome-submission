"""Deterministic stress/oracles; no network calls or timing-based interleavings."""
import contextlib
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from unittest.mock import patch

import seed
from agent import baseline, db, policy, reports
import test_repairs as helpers
Planner = helpers.Planner


class Stress(unittest.TestCase):
    setUp = helpers.Gaps.setUp
    ask = helpers.Gaps.ask

    def test_100x_rows_do_not_multiply_business_totals(self):
        # 100 copies with new record IDs but the same authorized field values.
        with contextlib.closing(db.connect(read_only=False)) as conn, conn:
            for copy in range(1, 100):
                suffix = f'-{copy}'
                conn.executemany('INSERT INTO customers VALUES (' + ','.join('?' for _ in range(19)) + ')',
                                 [(row[0] + suffix, *row[1:]) for row in seed.CUSTOMERS])
                conn.executemany('INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)',
                                 [(row[0] + suffix, row[1] + suffix, *row[2:]) for row in seed.TRANSACTIONS])
                conn.executemany('INSERT INTO alerts VALUES (?,?,?,?,?)',
                                 [(row[0] + suffix, row[1] + suffix, *row[2:]) for row in seed.ALERTS])
            conn.execute("INSERT INTO alerts VALUES ('extra', 't007', 'R-SANCTIONS', 'high', 'open')")
        result = self.ask(source='transactions', metrics=['sum_amount_minor'], filters=[
            {'field': 'currency', 'op': 'eq', 'value': 'USD'}, {'field': 'channel', 'op': 'eq', 'value': 'wire'}])
        self.assertEqual(result.released_results[0]['rows'], [{'sum_amount_minor': 638005000}])
        self.assertLess(result.elapsed_seconds, 2)
        query = reports.compile_report(policy.resolve_scope(db.get_user('u_ana')), reports.parse_request({
            'source': 'customers', 'mode': 'summary', 'metrics': ['count']}))
        # Force the deadline to expire when SQLite first checks its VM budget.
        with patch.object(db, 'monotonic', side_effect=[0, 3, 3, 3]):
            execution = db.execute_plan(query)
        self.assertEqual(execution.error, 'query_timeout')
        self.assertEqual(db.execute_plan(query).rows[0]['count'], 600)

    def test_global_active_capacity_recovers_after_overload(self):
        all_entered, release = Event(), Event()
        count_lock = Lock()
        count = 0
        class Blocked(Planner):
            def create(inner, **request):
                nonlocal count
                with count_lock:
                    count += 1
                    if count == 64:
                        all_entered.set()
                if not release.wait(10):
                    raise TimeoutError('test release missing')
                return super().create(**request)
        plan = {'action': 'report', 'source': 'customers', 'metrics': ['count']}
        with ThreadPoolExecutor(max_workers=64) as pool:
            tasks = [pool.submit(baseline.run, 'Count', 'u_ana', f'capacity-{i}', client=Blocked(plan)) for i in range(64)]
            try:
                self.assertTrue(all_entered.wait(5))
                extra = Planner(plan)
                result = baseline.run('Count', 'u_ana', 'overflow', client=extra)
                self.assertEqual(result.status, 'busy')
                self.assertEqual(extra.requests, [])
            finally:
                release.set()
            self.assertTrue(all(task.result(5).status == 'ok' for task in tasks))
        self.assertLessEqual(len(baseline._HISTORY), 64)
        result = baseline.run('Count', 'u_ana', 'overflow', client=Planner(plan))
        self.assertEqual(result.status, 'ok')

    def test_customer_floor_zero_one_two_and_null_contributors(self):
        for threshold, expected in [(100, 'disclosure_denied'), (90, 'disclosure_denied'), (70, 'ok')]:
            result = self.ask(source='customers', metrics=['count'], filters=[{'field': 'risk_score', 'op': 'gt', 'value': threshold}])
            self.assertEqual(result.status, expected)
        # A synthetic execution isolates a nullable metric contributor from row count.
        scope = policy.resolve_scope(db.get_user('u_rae'))
        query = reports.compile_report(scope, reports.parse_request({
            'source': 'customers', 'mode': 'summary', 'metrics': ['avg_annual_income_usd']}))
        execution = db.Execution([{'avg_annual_income_usd': 123, '_contributors': 2, '_avg_annual_income_usd': 1}], True)
        with self.assertRaises(reports.ReportError) as caught:
            reports.release(scope, query, execution)
        self.assertEqual(caught.exception.status, 'disclosure_denied')

    def test_rejected_shapes_never_reach_sql(self):
        mutations = [{'source': []}, {'fields': [False]}, {'limit': True}, {'limit': 101},
                     {'role': 'compliance'}, {'filters': [{'field': 'risk_score', 'op': 'in', 'value': 1}]},
                     {'filters': [{'field': 'risk_score', 'op': 'eq', 'value': float('nan')}]},
                     {'filters': [{'field': 'full_name', 'op': 'eq', 'value': 'x' * 257}]},
                     {'fields': ['customer_id', 'customer_id']}, {'metrics': {'count': True}}]
        for mutation in mutations:
            with self.subTest(mutation=mutation), patch.object(db, 'execute_plan', wraps=db.execute_plan) as execute:
                result = self.ask(**{'source': 'customers', 'fields': ['customer_id'], **mutation})
                self.assertEqual(result.status, 'invalid_request')
                execute.assert_not_called()
        for literal in ("' OR 1=1 --", '%', '_', '\\', '\x00'):
            result = self.ask(source='customers', fields=['customer_id'], filters=[{'field': 'customer_id', 'op': 'prefix', 'value': literal}])
            self.assertEqual(result.status, 'empty')
            self.assertEqual(result.released_results[0]['rows'], [])


if __name__ == '__main__':
    unittest.main()
