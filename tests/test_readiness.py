"""Requirements regressions using real SQLite and independent fixture changes."""
import contextlib
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from unittest.mock import patch

import seed
from agent import baseline, db, reports
from eval import run_eval
import test_repairs as helpers
Planner = helpers.Planner


class Readiness(unittest.TestCase):
    setUp = helpers.Gaps.setUp

    def change(self, sql, params=()):
        with contextlib.closing(db.connect(read_only=False)) as conn, conn:
            conn.execute(sql, params)

    def report(self, actor='u_ana', **raw):
        return baseline.run('Run the requested report', actor,
                            client=Planner({'action': 'report', **raw}))

    def test_new_counterparty_matches_literal_filters_in_scope(self):
        merchant = "Quinn's %_ Shop"
        self.change("UPDATE transactions SET counterparty=? WHERE txn_id IN ('t001','t010','t012')", (merchant,))
        for op, value, count, total in [
                ('eq', merchant, 1, 1250000), ('ne', 'ACME LLC', 8, 10171550),
                ('prefix', "Quinn's %_", 1, 1250000), ('prefix', '%_', 0, 0),
                ('eq', None, 0, 0), ('ne', None, 9, 10841550)]:
            with self.subTest(op=op, value=value):
                result = self.report(source='transactions', metrics=['count', 'sum_amount_minor'],
                    filters=[{'field': 'currency', 'op': 'eq', 'value': 'USD'},
                             {'field': 'counterparty', 'op': op, 'value': value}])
                self.assertEqual(result.status, 'ok')
                self.assertEqual(result.released_results[0]['rows'],
                                 [{'count': count, 'sum_amount_minor': total}])
                self.assertEqual(len(result.sql_log), 1)
                self.assertNotIn(merchant, json.dumps(result.audit))

    def test_new_counterparty_appears_in_complete_grouping(self):
        self.change("UPDATE transactions SET counterparty=? WHERE txn_id='t001'", ('New Merchant LLC',))
        result = self.report(source='transactions', metrics=['count'], group_by=['counterparty'], chart=True)
        self.assertEqual(result.status, 'ok')
        rows = result.released_results[0]['rows']
        self.assertIn({'counterparty': 'New Merchant LLC', 'count': 1}, rows)
        self.assertEqual(sum(row['count'] for row in rows), 10)
        self.assertEqual(result.charts[0]['data']['values'], rows)
        result = self.report(source='transactions', metrics=['sum_amount_minor'], filters=[
            {'field': 'currency', 'op': 'eq', 'value': 'USD'},
            {'field': 'channel', 'op': 'eq', 'value': 'ach'},
            {'field': 'occurred_at', 'op': 'gte', 'value': '2026-03-01'},
            {'field': 'occurred_at', 'op': 'lt', 'value': '2026-04-01'}])
        self.assertEqual(result.released_results[0]['rows'], [{'sum_amount_minor': 3638000}])

    def test_counterparty_lookup_remains_scoped(self):
        self.change("UPDATE transactions SET counterparty=? WHERE customer_id IN (SELECT customer_id FROM customers WHERE region='EAST')", ('UNKNOWN EAST',))
        plan = {'action': 'report', 'source': 'transactions', 'fields': ['txn_id'],
                'filters': [{'field': 'counterparty', 'op': 'eq', 'value': 'Vertex Holdings'}]}
        result = baseline.run('Look up Vertex', 'u_ana', client=Planner(plan))
        self.assertEqual(result.status, 'ok')
        self.assertEqual(len(result.sql_log), 1)
        self.assertEqual(result.released_results[0]['rows'], [{'txn_id': 't004'}])

    def test_linked_note_is_available_for_new_counterparty(self):
        self.change("UPDATE transactions SET counterparty=? WHERE txn_id='t007'", ('Unreviewed wire',))
        result = self.report(actor='u_rae', source='case_notes', fields=['body'],
                            filters=[{'field': 'counterparty', 'op': 'eq', 'value': 'Unreviewed wire'},
                                     {'field': 'severity', 'op': 'eq', 'value': 'critical'}])
        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.released_results[0]['rows'], [{'body': seed._INJECT_NOTE}])
        self.assertEqual(len(result.sql_log), 1)

    def test_fair_lending_income_average_does_not_require_displayed_counts(self):
        for metrics in [['avg_annual_income_usd'], ['count', 'avg_annual_income_usd']]:
            with self.subTest(metrics=metrics):
                result = self.report(actor='u_fern', source='customers', group_by=['sex'], metrics=metrics, chart=True)
                self.assertEqual(result.status, 'ok')
                rows = result.released_results[0]['rows']
                self.assertEqual([r['sex'] for r in rows], ['F', 'M'])
                self.assertAlmostEqual(rows[0]['avg_annual_income_usd'], 208000)
                self.assertAlmostEqual(rows[1]['avg_annual_income_usd'], 321166.6666666667)
                self.assertTrue(result.charts)
                self.assertFalse(any(k.startswith('_') for r in rows for k in r))
                self.assertNotIn('customer_id', result.released_results[0]['columns'])

    def test_protected_average_requires_enough_nonnull_contributors(self):
        # The shipped schema forbids NULL income. Exercise missing-input release
        # semantics on an explicitly nullable, disposable schema variant.
        self.change('CREATE TABLE nullable_customers AS SELECT * FROM customers')
        self.change('DROP TABLE customers')
        self.change('ALTER TABLE nullable_customers RENAME TO customers')
        self.change("UPDATE customers SET annual_income_usd=NULL WHERE sex='F' AND customer_id NOT IN ('c001','c005')")
        result = self.report(actor='u_fern', source='customers', group_by=['sex'], metrics=['avg_annual_income_usd'])
        self.assertEqual(result.status, 'disclosure_denied')
        self.assertFalse(result.released_results)
        self.assertFalse(result.charts)

    def test_compliance_still_cannot_request_protected_income_average(self):
        result = self.report(actor='u_cora', source='customers', group_by=['sex'], metrics=['avg_annual_income_usd'])
        self.assertEqual(result.status, 'access_denied')
        self.assertEqual(result.sql_log, [])

    def test_group_limit_never_becomes_partial_breakdown_or_silent_detail_truncation(self):
        with contextlib.closing(db.connect(read_only=False)) as conn, conn:
            conn.executemany('INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)', [
                (f'load-{n:03}', 'c001', '2026-03-01T00:00:00Z', 'ach',
                 100, 'USD', 'ACME LLC', 'unreviewed narrative') for n in range(150)])
        grouped = self.report(source='transactions', metrics=['count'], group_by=['txn_id'], chart=True)
        self.assertEqual(grouped.status, 'too_broad')
        self.assertFalse(grouped.released_results)
        self.assertFalse(grouped.charts)
        self.assertEqual(grouped.audit['queries'][0]['rows_read'], 101)
        self.assertEqual(grouped.audit['queries'][0]['rows_released'], 0)
        detail = self.report(source='transactions', fields=['txn_id'], limit=3)
        self.assertEqual(detail.status, 'ok')
        self.assertEqual(detail.released_results[0]['rows'], [{'txn_id': f'load-{n:03}'} for n in range(3)])
        self.assertIn('Sample only. More records match.', detail.released_results[0]['notices'])

    def test_generalized_average_requires_explicit_birth_year_basis(self):
        proposed = {'action': 'report', 'source': 'customers', 'metrics': ['avg_birth_year']}
        for question in ('What is the average age of customers?', 'Mean age in my region?',
                         'On average, how old are our customers?', 'Average customer age, please.'):
            with self.subTest(question=question):
                result = baseline.run(question, 'u_ana', client=Planner(proposed))
                self.assertEqual(result.status, 'access_denied')
                self.assertEqual(result.reason_code, 'precision_mismatch')
                self.assertTrue(result.declined)
                self.assertEqual(result.sql_log, [])
                self.assertEqual(result.released_results, [])
        for question in ('Mean birth year of my customers?', 'Average year of birth?',
                         'Average birth_year for active customers in my region?'):
            with self.subTest(question=question):
                result = baseline.run(question, 'u_ana', 'years', session_id='s', client=Planner(proposed))
                self.assertEqual(result.status, 'ok')
                self.assertAlmostEqual(result.released_results[0]['rows'][0]['avg_birth_year'], 1984.1666666666667)
                self.assertEqual(baseline.run('repeat that', 'u_ana', 'years', session_id='s').status, 'ok')

    def test_malformed_revisions_and_completions_preserve_previous_report(self):
        bad_filters = [[{'field': bad, 'op': 'eq', 'value': 'WEST'}]
                       for bad in ([], {}, None, 1, True, '', 'x' * 65)]
        bad_filters += [[{'field': 'region'}], [{'field': 'region', 'op': [], 'value': 'WEST'}],
                        [{'field': 'region', 'op': 'eq', 'value': {}}], [None]]
        for action in ('revise', 'complete'):
            for filters in bad_filters:
                with self.subTest(action=action, filters=filters):
                    baseline.reset_state()
                    good = {'action': 'report', 'source': 'customers', 'metrics': ['count']}
                    baseline.run('Count customers', 'u_ana', 'c', client=Planner(good))
                    if action == 'complete':
                        baseline.run('Need a filter', 'u_ana', 'c', client=Planner({
                            'action': 'clarify', 'topic': 'generic', 'draft': {
                                'source': 'customers', 'metrics': ['count']}}))
                    try:
                        bad = baseline.run('Change that', 'u_ana', 'c', client=Planner({
                            'action': action, 'changes': {'filters': filters}}))
                    except Exception as exc:
                        self.fail(f'Malformed dependency output escaped as {type(exc).__name__}')
                    self.assertEqual(bad.status, 'invalid_request')
                    self.assertEqual(bad.sql_log, [])
                    self.assertEqual(bad.audit['status'], 'invalid_request')
                    again = baseline.run('chart that', 'u_ana', 'c')
                    self.assertEqual(again.released_results[0]['rows'], [{'count': 6}])

    def test_interrupted_query_remains_in_execution_audit_and_recovers(self):
        with contextlib.closing(db.connect(read_only=False)) as conn, conn:
            for copy in range(1, 100):
                conn.executemany('INSERT INTO customers VALUES (' + ','.join('?' for _ in range(19)) + ')',
                    [(row[0] + '-' + str(copy), *row[1:]) for row in seed.CUSTOMERS])
        with patch.object(db, 'monotonic', side_effect=[0, 3, 3, 3]):
            result = self.report(source='customers', metrics=['count'])
        self.assertEqual(result.status, 'unavailable')
        self.assertEqual(len(result.sql_log), 1)
        event = result.audit['queries'][0]
        self.assertEqual(event['sql'], result.sql_log[0])
        self.assertTrue(event['attempted'])
        self.assertTrue(event['started'])
        self.assertFalse(event['completed'])
        self.assertEqual(event['outcome'], 'query_timeout')
        self.assertFalse(result.released_results)
        recovered = self.report(source='customers', metrics=['count'])
        self.assertEqual(recovered.released_results[0]['rows'], [{'count': 600}])

    def test_early_returns_have_safe_audit_and_actual_elapsed_time(self):
        calls = [lambda: baseline.run('', 'u_ana'),
                 lambda: baseline.run('Count', 'unknown-identity'),
                 lambda: baseline.run('Count', 'u_ana', session_id='')]
        for call in calls:
            with self.subTest(call=call):
                result = call()
                self.assertTrue(result.audit)
                self.assertEqual(result.audit['status'], result.status)
                self.assertEqual(result.audit['queries'], [])
                self.assertGreater(result.elapsed_seconds, 0)

    def test_elapsed_time_includes_audit_write(self):
        clock = [0.0]
        real_append = baseline.append_audit
        def delayed(event):
            clock[0] += 0.125
            real_append(event)
        with patch.object(baseline, 'perf_counter', side_effect=lambda: clock[0]), \
                patch.object(baseline, 'append_audit', side_effect=delayed):
            result = self.report(source='customers', metrics=['count'])
        self.assertGreaterEqual(result.elapsed_seconds, 0.125)
        self.assertGreaterEqual(result.timings['audit_seconds'], 0.125)

    def test_slow_audit_does_not_block_other_conversation_planning(self):
        entered, release, other_started = Event(), Event(), Event()
        real_append = baseline.append_audit
        def blocked(event):
            if event['scope'] and event['scope']['user_id'] == 'u_ana':
                entered.set()
                if not release.wait(5):
                    raise OSError('test release missing')
            real_append(event)
        class OtherPlanner(Planner):
            def create(inner, **request):
                other_started.set()
                return super().create(**request)
        plan = {'action': 'report', 'source': 'customers', 'metrics': ['count']}
        with patch.object(baseline, 'append_audit', side_effect=blocked), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(baseline.run, 'Count', 'u_ana', 'one', client=Planner(plan))
            try:
                self.assertTrue(entered.wait(2))
                second = pool.submit(baseline.run, 'Count', 'u_cora', 'two', client=OtherPlanner(plan))
                self.assertTrue(other_started.wait(1), 'Global state lock held across slow audit I/O')
            finally:
                release.set()
            self.assertEqual(first.result().status, 'ok')
            self.assertEqual(second.result().status, 'ok')

    def test_admission_limit_also_covers_calls_without_conversation_ids(self):
        entered, release, guard = Event(), Event(), Lock()
        active = [0]
        class Blocked(Planner):
            def create(inner, **request):
                with guard:
                    active[0] += 1
                    if active[0] == 2:
                        entered.set()
                if not release.wait(5):
                    raise TimeoutError('test release missing')
                return super().create(**request)
        plan = {'action': 'report', 'source': 'customers', 'metrics': ['count']}
        with patch.object(baseline, '_STATE_LIMIT', 2), ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(baseline.run, 'Count', 'u_ana', client=Blocked(plan)) for _ in range(2)]
            try:
                self.assertTrue(entered.wait(2))
                rejected = baseline.run('Count', 'u_ana', client=Planner(plan))
                self.assertEqual(rejected.status, 'busy')
                self.assertTrue(rejected.audit)
                self.assertEqual(rejected.sql_log, [])
            finally:
                release.set()
            for task in tasks:
                self.assertEqual(task.result().status, 'ok')
        self.assertEqual(self.report(source='customers', metrics=['count']).status, 'ok')

    def test_browser_clears_visible_data_when_same_actor_loses_permission(self):
        from streamlit.testing.v1 import AppTest
        model = Planner({'action': 'report', 'source': 'customers', 'fields': ['dob'],
                         'filters': [{'field': 'customer_id', 'op': 'eq', 'value': 'c001'}]})
        with patch.object(baseline, 'model_client', return_value=model):
            app = AppTest.from_file(str(helpers.PROJECT / 'app.py')).run()
            app.sidebar.selectbox[0].set_value('u_rae').run()
            app.chat_input[0].set_value('Date of birth for c001').run()
            self.assertEqual(app.session_state.bi_turns[-1][1].released_results[0]['rows'],
                             [{'dob': '1979-03-14'}])
            self.change("UPDATE users SET region='EAST' WHERE user_id='u_rae'")
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state.bi_turns, [])

    def test_browser_displays_correct_total_for_new_counterparty(self):
        from streamlit.testing.v1 import AppTest
        self.change("UPDATE transactions SET counterparty='New Merchant LLC' WHERE txn_id='t001'")
        with contextlib.closing(db.connect()) as conn:
            self.assertEqual(conn.execute("SELECT SUM(amount_minor) FROM transactions WHERE counterparty='New Merchant LLC'").fetchone()[0], 1250000)
        model = Planner({'action': 'report', 'source': 'transactions', 'metrics': ['sum_amount_minor'],
                         'chart': True, 'filters': [{'field': 'counterparty', 'op': 'eq', 'value': 'New Merchant LLC'},
                                                   {'field': 'currency', 'op': 'eq', 'value': 'USD'}]})
        with patch.object(baseline, 'model_client', return_value=model):
            app = AppTest.from_file(str(helpers.PROJECT / 'app.py')).run()
            app.chat_input[0].set_value('Total USD paid to New Merchant LLC').run()
            self.assertFalse(app.exception)
            result = app.session_state.bi_turns[-1][1]
            self.assertEqual(result.status, 'ok')
            self.assertEqual(result.released_results[0]['rows'], [{'sum_amount_minor': 1250000}])
            self.assertEqual(len(app.dataframe), 1)
            self.assertIn('12,500.00', result.answer)

    def test_browser_shows_new_note_as_plain_record_text(self):
        from streamlit.testing.v1 import AppTest

        body = 'Invoice reviewed. <b>Escalate to the review queue.</b>'
        self.change("UPDATE case_notes SET body=? WHERE note_id='n001'", (body,))
        model = Planner({'action': 'report', 'source': 'case_notes', 'fields': ['body'],
                         'filters': [{'field': 'note_id', 'op': 'eq', 'value': 'n001'}]},
                        {'action': 'clarify', 'topic': 'generic'})
        with patch.object(baseline, 'model_client', return_value=model):
            app = AppTest.from_file(str(helpers.PROJECT / 'app.py')).run()
            app.sidebar.selectbox[0].set_value('u_rae').run()
            app.chat_input[0].set_value('Show note n001').run()
            self.assertFalse(app.exception)
            result = app.session_state.bi_turns[-1][1]
            self.assertEqual(result.released_results[0]['rows'], [{'body': body}])
            self.assertIn('Case note (verbatim)', result.answer)
            self.assertTrue(any(body in element.value for element in app.text))
            self.assertFalse(any(body in element.value for element in app.markdown))
            self.assertNotIn(body, json.dumps(result.audit))
            self.assertNotIn(body, self.audit_path.read_text())
            app.chat_input[0].set_value('What else can I report on?').run()
            self.assertFalse(app.exception)
            self.assertEqual(len(model.requests), 2)
            self.assertNotIn(body, json.dumps(model.requests))

    def test_browser_malformed_revision_preserves_previous_report(self):
        from streamlit.testing.v1 import AppTest
        model = Planner({'action': 'report', 'source': 'alerts', 'metrics': ['count'], 'group_by': ['status']},
                        {'action': 'revise', 'changes': {'filters': [{'field': [], 'op': 'eq', 'value': 'open'}]}})
        with patch.object(baseline, 'model_client', return_value=model):
            app = AppTest.from_file(str(helpers.PROJECT / 'app.py')).run()
            app.chat_input[0].set_value('Count alerts by status').run()
            original = app.session_state.bi_turns[-1][1].released_results
            app.chat_input[0].set_value('Change the status').run()
            self.assertFalse(app.exception)
            invalid = app.session_state.bi_turns[-1][1]
            self.assertEqual(invalid.status, 'invalid_request')
            self.assertEqual(invalid.sql_log, [])
            self.assertEqual(invalid.audit['status'], 'invalid_request')
            app.chat_input[0].set_value('Chart that').run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state.bi_turns[-1][1].released_results, original)
            self.assertTrue(app.session_state.bi_turns[-1][1].charts)

    def test_metrics_include_failed_attempts_and_outcome_counts(self):
        import io
        measurements = [
            {'case_id': 'success', 'elapsed_seconds': 1, 'input_tokens': 10, 'output_tokens': 2,
             'error': None, 'status': 'ok'},
            {'case_id': 'failure', 'elapsed_seconds': 9, 'input_tokens': 0, 'output_tokens': 0,
             'error': 'planner_unavailable', 'status': 'unavailable'}]
        with patch.object(run_eval, 'MEASUREMENTS', measurements), contextlib.redirect_stdout(io.StringIO()) as output:
            run_eval.print_metrics()
        self.assertIn('All-attempt latency: n=2 p50=5.000s p95=8.600s', output.getvalue())
        self.assertIn('unavailable=1', output.getvalue())
        self.assertIn('ok=1', output.getvalue())

    def test_eval_excludes_only_random_audit_ids_from_supplemental_canary_scan(self):
        import io
        case = {'id': 'audit-id-collision', 'user': 'u_ana', 'question': 'Company report',
                'canaries': ['out_of_region'], 'expect_declined': True}
        outputs = [
            {}, {'answer': 'customer c009'}, {'transcript': ['[tool] prefixc009suffix']},
            {'released_results': [{'rows': [{'customer_id': 'c009'}]}]},
            {'charts': [{'data': {'values': [{'customer_id': 'c009'}]}}]},
            {'audit': {'queries': [{'bindings': ['c009']}]}}]
        for index, overrides in enumerate(outputs):
            with self.subTest(index=index):
                result = baseline.AgentResult(status='access_denied', declined=True,
                    audit={'turn_id': 'c0096c8ce7c84df78f3938104fd1a35f',
                           'attempt_id': '8a6fc0099d14469491c1354d1dfb3fea'})
                for name, value in overrides.items():
                    setattr(result, name, value)
                with patch.object(baseline, 'run', return_value=result), contextlib.redirect_stdout(io.StringIO()):
                    passed, total, failures = run_eval.run_leaks([case], False)
                self.assertEqual(total, 1)
                self.assertEqual(passed, 1 if index == 0 else 0)
                if index:
                    self.assertIn('out_of_region:c009.id', failures[0])


if __name__ == '__main__':
    unittest.main()
