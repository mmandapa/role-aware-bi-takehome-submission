import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import seed
from agent import baseline, db


class Recorder:
    def __init__(self, intent):
        self.intent = intent
        self.requests = []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **request):
        self.requests.append(request)
        return NS(usage=None, choices=[NS(message=NS(content=json.dumps(self.intent)))])


class Boundary(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = folder / 'data.db'
        with patch.object(seed, 'DB_PATH', str(path)):
            seed.main()
        self.enterContext(patch.object(db, 'DB_PATH', path))
        self.enterContext(patch.dict('os.environ', BI_AUDIT_PATH=str(folder / 'audit.jsonl')))
        baseline.reset_state()

    def ask(self, actor, **intent):
        model = Recorder({'action': 'report', **intent})
        result = baseline.run('Run my report', actor, 'same', client=model, session_id='one')
        self.assertEqual(len(model.requests), 1)
        return result, model.requests

    def test_permitted_identifier_is_local_only_and_cannot_replay(self):
        query = dict(source='customers', fields=['national_id'], filters=[
            {'field': 'customer_id', 'op': 'eq', 'value': 'c001'}])
        result, requests = self.ask('u_cora', **query)
        value = result.released_results[0]['rows'][0]['national_id']
        self.assertIn(value, result.answer)
        self.assertNotIn(value, json.dumps(requests))
        denied, next_requests = self.ask('u_ana', **query)
        self.assertTrue(denied.declined)
        self.assertFalse(denied.sql_log)
        self.assertNotIn(value, json.dumps(next_requests))

    def test_scope_floors_and_chart(self):
        result, requests = self.ask('u_ana', source='customers', metrics=['count'])
        self.assertEqual(result.released_results[0]['rows'], [{'count': 6}])
        denied, _ = self.ask('u_ana', source='customers', metrics=['count'], scope='company')
        self.assertTrue(denied.declined)
        self.assertFalse(denied.sql_log)
        refused, _ = self.ask('u_ana', source='customers', metrics=['count'], filters=[
            {'field': 'risk_score', 'op': 'gt', 'value': 90}])
        self.assertTrue(refused.declined)
        self.assertFalse(refused.released_results)
        chart, trace = self.ask('u_ana', source='alerts', metrics=['count'], group_by=['status'], chart=True)
        self.assertEqual(chart.charts[0]['data']['values'], [
            {'status':'closed','count':1}, {'status':'open','count':5}, {'status':'resolved','count':2}])
        self.assertNotIn('rows', trace[0]['messages'][-1]['content'])

    def test_arbitrary_warehouse_text_stays_out_of_provider_payload(self):
        from contextlib import closing
        marker = 'PRIVATE_TRACE_MARKER'
        with closing(db.connect(read_only=False)) as conn, conn:
            conn.execute("UPDATE transactions SET counterparty=? WHERE txn_id='t001'", (marker,))
        result, requests = self.ask('u_ana', source='transactions', fields=['counterparty'], limit=1)
        self.assertNotIn(marker, json.dumps(requests))
        self.assertEqual(result.released_results[0]['rows'][0]['counterparty'], marker)
        self.assertNotIn(marker, json.dumps(result.audit))

    def test_followup_chart_reuses_filters_locally_without_sending_them(self):
        report = {'action': 'report', 'source': 'alerts', 'group_by': ['status'], 'metrics': ['count'],
                  'filters': [{'field': 'status', 'op': 'eq', 'value': 'open'}]}
        first = baseline.run('Count open alerts by status', 'u_ana', 'thread', client=Recorder(report))
        model = Recorder({'action': 'repeat', 'chart': True})
        second = baseline.run('Can you plot the report from before?', 'u_ana', 'thread', client=model)
        self.assertEqual(first.released_results, second.released_results)
        self.assertEqual(second.charts[0]['data']['values'], [{'status': 'open', 'count': 5}])
        hint = json.loads(model.requests[0]['messages'][0]['content'].splitlines()[-1])
        self.assertNotIn('filters', hint['previous_report_shape'])

    def test_protected_cells_against_exhaustive_small_histograms(self):
        from itertools import product
        from agent import policy
        for width in range(1, 6):
            for counts in product(range(1, 6), repeat=width):
                rows = [{'label': i, 'count': n} for i, n in enumerate(counts)]
                visible = [row for row in rows if row['count'] >= 3]
                hidden = width - len(visible)
                remainder = sum(counts) - sum(row['count'] for row in visible)
                alternatives = sum(sum(candidate) == remainder
                                   for candidate in product((1, 2), repeat=hidden))
                expected = visible if sum(counts) >= 10 and visible and (
                    not hidden or alternatives > 1) else None
                self.assertEqual(policy.protected_cells(rows), expected, counts)

    def test_parallel_turns_emit_exactly_one_audit_each(self):
        import os
        from concurrent.futures import ThreadPoolExecutor
        def turn(i):
            model = Recorder({'action': 'report', 'source': 'customers', 'metrics': ['count']})
            actor = 'u_ana' if i % 2 == 0 else 'u_cora'
            return baseline.run('Count customers', actor, str(i), client=model)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(turn, range(240)))
        for i, result in enumerate(results):
            self.assertEqual(result.released_results[0]['rows'], [{'count': 6 if i % 2 == 0 else 15}])
        records = [json.loads(line) for line in Path(os.environ['BI_AUDIT_PATH']).read_text().splitlines()]
        self.assertCountEqual([record['turn_id'] for record in records],
                              [result.audit['turn_id'] for result in results])
        self.assertEqual(len({record['turn_id'] for record in records}), 240)

    def test_yesterday_without_anchor_clarifies_before_planning(self):
        model = Recorder({'action': 'report', 'source': 'transactions', 'metrics': ['count']})
        answer = baseline.run("Show me yesterday's transaction activity for my region.",
                              'u_ana', client=model)
        self.assertTrue(answer.clarified)
        self.assertIn('UTC', answer.answer)
        self.assertEqual(answer.sql_log, [])
        self.assertEqual(model.requests, [])

    def test_yesterday_with_explicit_date_can_be_planned(self):
        model = Recorder({'action': 'report', 'source': 'transactions', 'metrics': ['count']})
        answer = baseline.run('Show yesterday (2026-03-15) in my region.', 'u_ana', client=model)
        self.assertFalse(answer.clarified)
        self.assertEqual(len(model.requests), 1)

    def test_overlap_cannot_replace_report_and_other_conversation_progresses(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        entered, release = Event(), Event()

        class SlowRecorder(Recorder):
            def create(self, **request):
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('test did not release planner')
                return super().create(**request)

        report = {'action': 'report', 'source': 'alerts', 'metrics': ['count'], 'group_by': ['status']}
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(baseline.run, 'Count alerts', 'u_ana', 'thread',
                                client=SlowRecorder(report))
            try:
                self.assertTrue(entered.wait(5))
                other = pool.submit(baseline.run, 'Count alerts', 'u_ana', 'other',
                                    client=Recorder(report)).result(3)
                self.assertEqual(other.status, 'ok')
                overlapping = Recorder({'action': 'repeat', 'chart': True})
                busy = pool.submit(baseline.run, 'Chart that', 'u_ana', 'thread',
                                   client=overlapping).result(3)
                self.assertEqual(busy.status, 'busy')
                self.assertEqual(overlapping.requests, [])
                self.assertFalse(first.done())
            finally:
                release.set()
            self.assertEqual(first.result(5).status, 'ok')
        retry = baseline.run('Chart that', 'u_ana', 'thread',
                             client=Recorder({'action': 'repeat', 'chart': True}))
        self.assertEqual(len(retry.charts[0]['data']['values']), 3)

    def test_repeat_without_report_clarifies_and_runs_no_sql(self):
        model = Recorder({'action': 'repeat', 'chart': True})
        result = baseline.run('Chart that', 'u_ana', 'new',
                              client=model)
        self.assertTrue(result.clarified)
        self.assertEqual(result.sql_log, [])
        self.assertEqual(result.charts, [])
        self.assertEqual(model.requests, [])

    def test_explicit_chart_reruns_previous_sql_without_the_provider(self):
        from contextlib import closing

        model = Recorder({'action': 'report', 'source': 'alerts', 'group_by': ['status'],
                          'metrics': ['count'], 'filters': [{'field': 'status', 'op': 'eq', 'value': 'open'}]})
        original = baseline.run('Count open alerts by status.', 'u_ana', 'thread', client=model,
                                session_id='browser')
        self.assertEqual(original.released_results[0]['rows'], [{'status': 'open', 'count': 5}])
        with closing(db.connect(read_only=False)) as conn, conn:
            conn.execute("UPDATE alerts SET status='closed' WHERE alert_id='a003'")

        class OfflinePlanner(Recorder):
            def create(self, **request):
                raise TimeoutError('provider offline')

        result = baseline.run('Please chart that!', 'u_ana', 'thread', client=OfflinePlanner({}),
                              session_id='browser')
        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.charts[0]['data']['values'], [{'status': 'open', 'count': 4}])
        self.assertEqual(result.turns, 0)
        self.assertEqual(len(result.sql_log), 1)
        self.assertEqual(result.audit['planner_seconds'], 0)

    def test_no_api_key_widget_or_credential_in_browser_failure(self):
        from streamlit.testing.v1 import AppTest

        credential = 'synthetic-credential-for-browser-test'
        with patch.dict('os.environ', OPENAI_API_KEY=credential), patch.object(
            baseline, 'model_client', side_effect=RuntimeError(credential)
        ), self.assertLogs('agent.baseline', level='WARNING') as log:
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py')).run()
            app.chat_input[0].set_value('Count active customers.').run()
        self.assertFalse(app.exception)
        self.assertEqual(list(app.text_input), [])
        self.assertEqual(app.session_state.bi_turns[-1][1].error, 'planner_unavailable')
        self.assertNotIn(credential, repr(app.text))
        self.assertNotIn(credential, '\n'.join(log.output))

    def test_provider_duration_is_audited_even_when_the_provider_fails(self):
        now = 100.0

        class SlowFailure(Recorder):
            def create(self, **request):
                nonlocal now
                now += 8.0
                raise TimeoutError('provider stalled')

        with patch.object(baseline, 'perf_counter', lambda: now):
            result = baseline.run('Count active customers.', 'u_ana', client=SlowFailure({}))
        self.assertEqual(result.error, 'planner_unavailable')
        self.assertEqual(result.audit['planner_seconds'], 8.0)
        self.assertEqual(result.audit['queries'], [])

    def test_rejected_credential_is_reported_safely_and_preserves_last_report(self):
        import os
        from dataclasses import asdict

        secret = 'synthetic-credential-in-provider-error'
        report = {'action': 'report', 'source': 'customers', 'metrics': ['count']}

        class Rejected(Recorder):
            def create(self, **request):
                self.requests.append(request)
                error = RuntimeError(secret)
                error.status_code = 401
                raise error

        baseline.run('Count customers', 'u_ana', 'auth', client=Recorder(report))
        provider = Rejected({})
        with self.assertLogs('agent.baseline', level='WARNING') as log:
            result = baseline.run('Count alerts', 'u_ana', 'auth', client=provider)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(result.status, 'unavailable')
        self.assertEqual(result.error, 'planner_unavailable')
        self.assertIn('rejected', result.answer)
        self.assertEqual(result.audit['reason_code'], 'planner_authentication_failed')
        self.assertEqual(result.sql_log, [])
        self.assertEqual(result.released_results, [])
        self.assertNotIn(secret, json.dumps(asdict(result)))
        self.assertNotIn(secret, Path(os.environ['BI_AUDIT_PATH']).read_text())
        self.assertNotIn(secret, '\n'.join(log.output))
        repeated = baseline.run('Repeat that', 'u_ana', 'auth', client=provider)
        self.assertEqual(repeated.released_results[0]['rows'], [{'count': 6}])
        self.assertEqual(len(provider.requests), 1)

    def test_generated_answer_cannot_override_database_result(self):
        result = baseline.run('Count my customers', 'u_ana', client=Recorder({
            'action': 'report', 'source': 'customers', 'metrics': ['count'],
            'answer': '999 customers',
        }))
        self.assertEqual(result.status, 'invalid_request')
        self.assertNotIn('999', result.answer)
        self.assertEqual(result.sql_log, [])

    def test_rerun_does_not_repeat_query_and_switch_clears_history(self):
        import os
        from streamlit.testing.v1 import AppTest
        model = Recorder({'action': 'report', 'source': 'customers', 'metrics': ['count']})
        with patch.object(baseline, 'model_client', return_value=model):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'),
                                    default_timeout=30).run()
            app.chat_input[0].set_value('Count active customers.').run()
            self.assertFalse(app.exception)
            self.assertEqual(len(model.requests), 1)
            self.assertEqual(len(app.chat_message), 2)
            records = [json.loads(line) for line in Path(os.environ['BI_AUDIT_PATH']).read_text().splitlines()]
            self.assertEqual(len(app.session_state.bi_turns), 1)
            self.assertEqual(app.session_state.bi_turns[-1][1].released_results[0]['rows'],
                             [{'count': 6}])
            self.assertEqual(len(records), 1)
            self.assertEqual(len(records[0]['queries']), 1)
            self.assertFalse(any(button.label in ('Run report', 'Cancel') for button in app.button))
            self.assertTrue(any(panel.label == 'Query activity' for panel in app.expander))
            self.assertTrue(any('Scope: WEST' in element.value for element in app.text))
            app.run()
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.chat_message), 2)
            self.assertEqual(len(model.requests), 1)
            records = [json.loads(line) for line in Path(os.environ['BI_AUDIT_PATH']).read_text().splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(sum(len(r['queries']) for r in records), 1)
            app.selectbox[0].set_value('u_ben').run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.chat_message), 0)
            # Streamlit Clear cache clears session state. A fresh session must
            # never use the former session's report, even with the same actor.
            fresh = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'),
                                      default_timeout=30).run()
            with patch.object(baseline, 'model_client',
                              return_value=Recorder({'action': 'repeat', 'chart': True})):
                fresh.chat_input[0].set_value('Chart that.').run()
            self.assertFalse(fresh.exception)
            self.assertEqual(fresh.session_state.bi_turns[-1][1].status, 'clarified')
            self.assertEqual(fresh.session_state.bi_turns[-1][1].sql_log, [])

    def test_provider_failure_returns_no_report_and_retry_can_progress(self):
        class Unavailable(Recorder):
            def create(self, **request):
                raise TimeoutError('private diagnostic detail')

        result = baseline.run('Count customers', 'u_ana', 'retry', client=Unavailable({}))
        self.assertEqual(result.error, 'planner_unavailable')
        self.assertNotIn('private diagnostic detail', result.answer)
        self.assertEqual(result.sql_log, [])
        self.assertEqual(result.released_results, [])
        result = baseline.run('Count customers', 'u_ana', 'retry', client=Recorder({
            'action': 'report', 'source': 'customers', 'metrics': ['count'],
        }))
        self.assertEqual(result.released_results[0]['rows'], [{'count': 6}])

    def test_planner_transport_has_a_bounded_retry(self):
        with patch('openai.OpenAI') as constructor:
            baseline.model_client('test credential')
        self.assertEqual(constructor.call_args.kwargs['max_retries'], 1)

    def test_utc_interval_includes_start_and_excludes_end(self):
        from contextlib import closing
        with closing(db.connect(read_only=False)) as conn, conn:
            conn.executemany(
                'INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                [('t-start', 'c001', 123, 'USD', 'ach', '2026-03-01T00:00:00Z', 'ACME LLC', None),
                 ('t-end', 'c001', 456, 'USD', 'ach', '2026-04-01T00:00:00Z', 'ACME LLC', None)],
            )
        result, _ = self.ask('u_ana', source='transactions', metrics=['sum_amount_minor'], filters=[
            {'field': 'currency', 'op': 'eq', 'value': 'USD'},
            {'field': 'channel', 'op': 'eq', 'value': 'ach'},
            {'field': 'occurred_at', 'op': 'gte', 'value': '2026-03-01T00:00:00Z'},
            {'field': 'occurred_at', 'op': 'lt', 'value': '2026-04-01T00:00:00Z'},
        ])
        self.assertEqual(result.released_results[0]['rows'], [{'sum_amount_minor': 3638123}])

    def test_currency_split_preserves_units_and_unqualified_sum_is_rejected(self):
        query = {'source': 'transactions', 'metrics': ['sum_amount_minor'], 'filters': [
            {'field': 'channel', 'op': 'eq', 'value': 'wire'},
        ]}
        rejected, _ = self.ask('u_ana', **query)
        self.assertEqual(rejected.status, 'invalid_request')
        self.assertEqual(rejected.sql_log, [])
        split, _ = self.ask('u_ana', **query, group_by=['currency'])
        self.assertCountEqual(split.released_results[0]['rows'], [
            {'currency': 'USD', 'sum_amount_minor': 6380050},
            {'currency': 'EUR', 'sum_amount_minor': 150000},
        ])

    def test_ambiguous_followup_does_not_replace_previous_report(self):
        count = {'action': 'report', 'source': 'customers', 'metrics': ['count']}
        initial = baseline.run('Count customers', 'u_ana', 'follow', client=Recorder(count))
        unclear = baseline.run('What about the other ones?', 'u_ana', 'follow',
                               client=Recorder({'action': 'clarify', 'topic': 'followup'}))
        self.assertTrue(unclear.clarified)
        self.assertEqual(unclear.sql_log, [])
        repeat = baseline.run('Repeat the report', 'u_ana', 'follow', client=Recorder({'action': 'repeat'}))
        self.assertEqual(initial.released_results, repeat.released_results)

    def test_unspecified_other_followup_uses_general_planner_clarification(self):
        for question in ('What about the other ones?', 'And the rest?', 'How about the others?'):
            with self.subTest(question=question):
                model = Recorder({'action': 'clarify', 'topic': 'followup'})
                result = baseline.run(question, 'u_ana', 'follow', client=model)
                self.assertTrue(result.clarified)
                self.assertEqual(result.sql_log, [])
                self.assertEqual(len(model.requests), 1)

    def test_report_interpretation_shows_dates_currency_and_scope(self):
        result, _ = self.ask('u_ana', source='transactions', metrics=['sum_amount_minor'], filters=[
            {'field': 'currency', 'op': 'eq', 'value': 'usd'},
            {'field': 'occurred_at', 'op': 'gte', 'value': '2026-03-01'},
            {'field': 'occurred_at', 'op': 'lt', 'value': '2026-04-01'},
        ])
        self.assertIn('WEST', result.report_summary)
        self.assertIn('Currency: USD', result.report_summary)
        self.assertIn('Transaction time: on or after 2026-03-01T00:00:00Z UTC', result.report_summary)
        self.assertIn('Transaction time: before 2026-04-01T00:00:00Z UTC', result.report_summary)

    def test_invalid_or_reversed_dates_cannot_execute(self):
        for values in [('2026-02-29', '2026-04-01'), ('2026-04-01', '2026-03-01'),
                       ('2026-03-01T00:00:00-06:00', '2026-04-01')]:
            with self.subTest(values=values), patch.object(db, 'execute_plan') as execute:
                model = Recorder({'action': 'report', 'source': 'transactions', 'metrics': ['count'],
                                  'filters': [{'field': 'occurred_at', 'op': op, 'value': value}
                                              for op, value in zip(('gte', 'lt'), values)]})
                result = baseline.run('Count transactions', 'u_ana', client=model)
                self.assertEqual(result.status, 'invalid_request')
                execute.assert_not_called()

    def test_generated_calendar_days_use_half_open_utc_intervals(self):
        from datetime import date, timedelta
        from agent import reports
        for year in range(2024, 2029):
            day = date(year, 2, 27)
            while day.month == 2:
                with self.subTest(day=day):
                    terms = reports.normalize_transaction_dates([
                        {'field': 'occurred_at', 'op': 'eq', 'value': day.isoformat()},
                    ])
                    self.assertEqual([(f['op'], f['value']) for f in terms], [
                        ('gte', f'{day}T00:00:00Z'),
                        ('lt', f'{day + timedelta(days=1)}T00:00:00Z'),
                    ])
                day += timedelta(days=1)
