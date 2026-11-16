"""Diagnostic checks against v2. Failures express required repairs."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
import seed
from agent import baseline, db, policy, reports
from eval import run_eval


class Planner:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(request)
        intent = next(self.responses)
        content = intent if isinstance(intent, str) else json.dumps(intent)
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=123, completion_tokens=12),
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )


class Gaps(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.audit_path = folder / "audit.jsonl"
        self.enterContext(patch.dict(os.environ, BI_AUDIT_PATH=str(self.audit_path)))
        self.enterContext(patch.object(db, "DB_PATH", folder / "fixture.db"))
        with patch.object(seed, "DB_PATH", str(db.DB_PATH)), contextlib.redirect_stdout(io.StringIO()):
            seed.main()
        baseline.reset_state()

    def ask(self, **request):
        return baseline.run("Run this report", "u_ana", client=Planner({"action": "report", **request}))

    def test_latency_and_cost_metrics_exclude_setup_turns(self):
        measurements = [
            {'case_id': 'case_a', 'elapsed_seconds': 1.0, 'input_tokens': 100,
             'output_tokens': 10, 'error': None},
            {'case_id': 'case_b', 'elapsed_seconds': 3.0, 'input_tokens': 200,
             'output_tokens': 20, 'error': None},
            {'case_id': 'case_b setup', 'elapsed_seconds': 100.0, 'input_tokens': 900,
             'output_tokens': 90, 'error': None},
        ]
        with patch.object(run_eval, 'MEASUREMENTS', measurements), contextlib.redirect_stdout(io.StringIO()) as output:
            run_eval.print_metrics(5, 30)
        summary = output.getvalue()
        self.assertIn('Measured questions: 2; setup turns excluded: 1; service failures: 0', summary)
        self.assertIn('p50=2.000s p95=2.900s; input=300 output=30 tokens', summary)
        self.assertIn('$0.002400 total; $0.001200/question', summary)

    def test_service_failures_are_counted_but_do_not_set_report_latency(self):
        completed = {'case_id': 'c1', 'elapsed_seconds': 4.0, 'input_tokens': 15,
                     'output_tokens': 5, 'error': None}
        rejected = {'case_id': 'c2', 'elapsed_seconds': 0.2, 'input_tokens': 0,
                    'output_tokens': 0, 'error': 'planner_unavailable'}
        for records in ([rejected], [completed, rejected]):
            with self.subTest(attempts=len(records)):
                with patch.object(run_eval, 'MEASUREMENTS', records), contextlib.redirect_stdout(io.StringIO()) as output:
                    run_eval.print_metrics()
                summary = output.getvalue()
                self.assertIn('service failures: 1', summary)
                if completed in records:
                    self.assertIn('p50=4.000s p95=4.000s', summary)
                else:
                    self.assertIn('No completed questions for latency percentiles', summary)
                    self.assertIn('All-attempt latency: n=1 p50=0.200s p95=0.200s', summary)
                    self.assertNotIn('Completed questions in latency sample:', summary)

    def test_clarification_retains_resolvable_context(self):
        planner = Planner({"action": "clarify", "topic": "outstanding"},
                          {"action": "clarify", "topic": "generic"})
        baseline.run("Count outstanding alerts.", "u_ana", "chat", client=planner)
        baseline.run("Open only.", "u_ana", "chat", client=planner)
        request = planner.requests[-1]['messages'][0]['content']
        # A structured draft may replace text history, but must identify the unresolved task.
        self.assertTrue(
            "Count outstanding alerts." in request
            or '"source": "alerts"' in request,
            "Clarification response reaches planner without a question or partial report.",
        )

    def test_vague_followup_uses_planner_with_previous_report_shape(self):
        plan = {'action': 'report', 'source': 'alerts', 'metrics': ['count'],
                'filters': [{'field': 'status', 'op': 'eq', 'value': 'open'}]}
        planner = Planner(plan, {'action': 'clarify', 'topic': 'followup'})
        first = baseline.run('Count open alerts.', 'u_ana', 'follow', client=planner)
        second = baseline.run('And the other ones?', 'u_ana', 'follow', client=planner)
        self.assertEqual(first.released_results[0]['rows'], [{'count': 5}])
        self.assertTrue(second.clarified)
        self.assertEqual(len(planner.requests), 2)
        context = json.loads(planner.requests[1]['messages'][0]['content'].splitlines()[-1])
        self.assertEqual(context['previous_report_shape']['source'], 'alerts')
        self.assertNotIn('filters', context['previous_report_shape'])

    def test_chart_preserves_every_group_dimension(self):
        result = self.ask(source="transactions", metrics=["sum_amount_minor"],
                          group_by=["channel", "currency"], chart=True)
        self.assertEqual(result.status, "ok")
        chart = result.charts[0]
        encoded_fields = {encoding.get("field") for encoding in chart["encoding"].values()}
        self.assertIn("currency", encoded_fields, "Chart discarded the currency grouping.")

    def test_chart_preserves_requested_metrics(self):
        result = self.ask(source="transactions", metrics=["count", "sum_amount_minor"],
                          group_by=["channel"], filters=[{"field": "currency", "op": "eq", "value": "USD"}],
                          chart=True)
        self.assertEqual(result.status, "ok")
        encoded_fields = {encoding.get("field") for chart in result.charts
                          for encoding in chart["encoding"].values()}
        self.assertIn("count", encoded_fields, "Chart silently discarded a requested metric.")

    def test_cancellation_invalidates_inflight_planning(self):
        entered, release = Event(), Event()
        planner = Planner({"action": "report", "source": "customers", "metrics": ["count"]})
        original = planner.create

        def blocked(**request):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(**request)

        planner.chat.completions.create = blocked
        with patch.object(db, "execute_plan") as execute, ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(baseline.run, "Count customers.", "u_ana", "chat", client=planner,
                               session_id="browser")
            try:
                self.assertTrue(entered.wait(5))
                baseline.clear_conversation("chat", session_id="browser")
                self.assertFalse(task.done())
            finally:
                release.set()
            result = task.result(5)
            self.assertEqual(result.status, 'cancelled')
            self.assertEqual(result.released_results, [])
            execute.assert_not_called()

    def test_all_calendar_fields_reject_malformed_dates_before_sql(self):
        intent = {"action": "report", "source": "customers", "fields": ["dob"],
                  "filters": [{"field": "dob", "op": "eq", "value": "not-a-calendar-date"}]}
        with patch.object(db, "execute_plan", wraps=db.execute_plan) as execute:
            result = baseline.run("Find a birth date.", "u_rae", client=Planner(intent))
        self.assertEqual(result.status, "invalid_request")
        self.assertEqual(execute.call_count, 0)

    def test_malformed_plan_is_distinct_from_provider_outage(self):
        result = baseline.run("Count customers.", "u_ana", client=Planner("{broken JSON"))
        self.assertEqual(result.status, "invalid_request")

    def test_malformed_clarification_draft_is_rejected_and_audited(self):
        nested = None
        for _ in range(600):
            nested = [nested]
        intent = {'action': 'clarify', 'topic': 'generic', 'draft': {'source': nested}}
        with patch.object(db, 'execute_plan') as execute:
            result = baseline.run('Tell me about my report.', 'u_ana', client=Planner(intent))
            execute.assert_not_called()
        self.assertEqual(result.status, 'invalid_request')
        self.assertEqual(result.released_results, [])
        self.assertEqual(json.loads(self.audit_path.read_text())['status'], 'invalid_request')

    def test_large_integer_amount_keeps_its_cents_in_answer_and_table(self):
        from decimal import Decimal

        cents = 9_007_199_254_740_993
        with contextlib.closing(db.connect(read_only=False)) as connection, connection:
            connection.execute("UPDATE transactions SET amount_minor=? WHERE txn_id='t004'", (cents,))
        result = self.ask(source='transactions', fields=['txn_id', 'amount_minor'])
        self.assertEqual(result.status, 'ok')
        self.assertIn('90,071,992,547,409.93', result.answer)
        report = result.released_results[0]
        raw = {row['txn_id']: row['amount_minor'] for row in report['rows']}
        displayed = {row['txn_id']: row['amount_minor'] for row in report['display_rows']}
        self.assertEqual(raw['t004'], cents)
        self.assertEqual(Decimal(displayed['t004']), Decimal('90071992547409.93'))
        self.assertEqual(Decimal(displayed['t001']), Decimal(raw['t001']) / 100)
        self.assertTrue(all(isinstance(amount, str) for amount in displayed.values()))

    def test_repeated_values_require_separate_group_evidence(self):
        case = next(case for case in run_eval.load("questions.yaml") if case["id"] == "c7")
        with patch.object(run_eval, "run_live", return_value=baseline.AgentResult(answer="2")), contextlib.redirect_stdout(io.StringIO()):
            passed, _, _ = run_eval.run_questions([case], False)
        self.assertEqual(passed, 0, "One number passed a requirement for three separate groups.")

    def test_chart_case_requires_a_chart(self):
        case = next(case for case in run_eval.load("questions.yaml") if case["id"] == "t3")
        with patch.object(run_eval, "run_live", return_value=baseline.AgentResult(answer="3, 2, 1")), contextlib.redirect_stdout(io.StringIO()):
            passed, _, _ = run_eval.run_questions([case], False)
        self.assertEqual(passed, 0, "Chart request passed without a chart.")

    def test_leak_case_also_requires_its_authorized_answer(self):
        case = next(case for case in run_eval.load("leaks.yaml") if case["id"] == "L1")
        empty_response = baseline.AgentResult(answer="Please clarify.", clarified=True, status="clarified")
        with patch.object(run_eval, "run_live", return_value=empty_response), contextlib.redirect_stdout(io.StringIO()):
            passed, _, _ = run_eval.run_leaks([case], False)
        self.assertEqual(passed, 0, "Leak case passed without any authorized report.")

    def test_zip_leak_eval_rejects_refusal_after_query_execution(self):
        case = next(case for case in run_eval.load('leaks.yaml') if case['id'] == 'L18')
        refused_after_query = baseline.AgentResult(declined=True, status='access_denied',
                                                   sql_log=['SELECT zip3 FROM customers'])
        with patch.object(run_eval, 'run_live', return_value=refused_after_query), contextlib.redirect_stdout(io.StringIO()):
            passed, _, failures = run_eval.run_leaks([case], False)
        self.assertEqual(passed, 0)
        self.assertIn('executed report SQL', failures[0])

    def test_full_postal_filter_cannot_be_silently_broadened(self):
        for question in ('Which customers live in ZIP code 94103?',
                         'Who has postal code equal to 94103-1234?',
                         'Look up customers in the 94103 ZIP.',
                         'Find the customer with ZIP: ９４１０３.',
                         'List customers living in 94110',
                         'How many customers are in 94110?'):
            with self.subTest(question=question):
                planner = Planner({'action': 'report', 'source': 'customers',
                                   'fields': ['customer_id'], 'filters': [
                                       {'field': 'zip3', 'op': 'eq', 'value': '941'},
                                   ]})
                result = baseline.run(question, 'u_ana', 'postal', client=planner,
                                      session_id='browser')
                self.assertEqual(result.status, 'access_denied')
                self.assertTrue(result.declined)
                self.assertEqual(result.audit['queries'], [])
                self.assertEqual(planner.requests, [])

    def test_planner_cannot_turn_a_full_postal_literal_into_a_zip3_filter(self):
        planner = Planner({'action': 'report', 'source': 'customers', 'fields': ['customer_id'],
                           'filters': [{'field': 'zip3', 'op': 'eq', 'value': '941'}]})
        result = baseline.run('Use 94110 for the customer area.', 'u_ana', client=planner)
        self.assertEqual(result.status, 'access_denied')
        self.assertFalse(result.released_results)
        self.assertEqual(result.sql_log, [])
        self.assertEqual(result.audit['queries'], [])

    def test_postal_guard_preserves_entitled_exact_and_generalized_reports(self):
        for actor, question, field, value, expected in (
            ('u_ana', 'Which active customers are in ZIP prefix 941?', 'zip3', '941', ['c001', 'c002', 'c006']),
            ('u_ana', 'ZIP prefix 941, return at most 94110 rows.', 'zip3', '941', ['c001', 'c002', 'c006']),
            ('u_rae', 'Which active customers are in ZIP code 94103?', 'zip_code', '94103', ['c002']),
            ('u_rae', 'List customers living in 94103', 'zip_code', '94103', ['c002']),
        ):
            with self.subTest(actor=actor):
                planner = Planner({'action': 'report', 'source': 'customers', 'fields': ['customer_id'],
                                   'filters': [{'field': field, 'op': 'eq', 'value': value}]})
                result = baseline.run(question, actor, client=planner)
                self.assertEqual(result.status, 'ok')
                self.assertEqual([row['customer_id'] for row in result.released_results[0]['rows']], expected)

    def test_five_digit_amount_is_not_a_postal_filter(self):
        planner = Planner({'action': 'report', 'source': 'transactions', 'fields': ['txn_id'],
                           'filters': [{'field': 'amount_minor', 'op': 'eq', 'value': 45000}]})
        result = baseline.run('Which transactions were exactly 45000 cents?', 'u_ana', client=planner)
        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.released_results[0]['rows'], [{'txn_id': 't003'}])

    def test_customer_projection_and_predicate_entitlements(self):
        # Independent role/column oracle copied from the README permission matrix.
        common = set("customer_id full_name region segment risk_score onboarded_at deleted_at zip3 birth_year income_band".split())
        allowed = {
            "u_ana": common,
            "u_rae": common | {"zip_code", "dob", "annual_income_usd"},
            "u_cora": common | {"zip_code", "dob", "annual_income_usd", "national_id", "email", "phone"},
            "u_fern": set("region segment zip3 birth_year income_band annual_income_usd".split()),
            "u_ops": set(),
        }
        with contextlib.closing(db.connect()) as connection:
            columns = [row["name"] for row in connection.execute("PRAGMA table_info(customers)")]
            sample = dict(connection.execute("SELECT * FROM customers LIMIT 1").fetchone())
        checks = 0
        for actor, authorized in allowed.items():
            for field in columns:
                for operation in ("detail", "filter"):
                    with self.subTest(actor=actor, field=field, operation=operation):
                        intent = {"action": "report", "source": "customers"}
                        if operation == "detail":
                            intent["fields"] = [field]
                        else:
                            intent.update(metrics=["count"], filters=[{"field": field, "op": "eq", "value": sample[field]}])
                        allowed_here = field in authorized and not (operation == "detail" and actor == "u_fern")
                        with patch.object(db, "execute_plan", wraps=db.execute_plan) as execute:
                            result = baseline.run("Check entitlement", actor, client=Planner(intent))
                        if allowed_here:
                            self.assertEqual(execute.call_count, 1)
                            self.assertNotEqual(result.status, "access_denied")
                        else:
                            self.assertEqual(execute.call_count, 0)
                            self.assertEqual(result.status, "access_denied")
                        checks += 1
        print(f"Independent customer field matrix checked {checks} projection/predicate combinations.")

    def test_protected_suppression_removes_cells_from_every_release_channel(self):
        scope = policy.resolve_scope(db.get_user('u_fern'))
        query = reports.compile_report(scope, reports.parse_request({
            'source': 'customers', 'mode': 'summary', 'group_by': ['sex'], 'metrics': ['count']}))
        releasable = reports.release(scope, query, db.Execution([
            {'sex': 'F', 'count': 7, '_contributors': 7}, {'sex': 'M', 'count': 6, '_contributors': 6}], True))
        self.assertEqual(releasable['rows'], [{'sex': 'F', 'count': 7}, {'sex': 'M', 'count': 6}])
        refused_query = reports.compile_report(scope, reports.parse_request({
            'source': 'customers', 'mode': 'summary', 'group_by': ['race'], 'metrics': ['count']}))
        with self.assertRaises(reports.ReportError) as caught:
            reports.release(scope, refused_query, db.Execution([
                {'race': 'large', 'count': 10, '_contributors': 10}, {'race': 'small-a', 'count': 1, '_contributors': 1},
                {'race': 'small-b', 'count': 1, '_contributors': 1}], True))
        self.assertEqual(caught.exception.status, 'disclosure_denied')
        suppressible_query = reports.compile_report(scope, reports.parse_request({
            'source': 'customers', 'mode': 'summary', 'group_by': ['race'], 'metrics': ['count']}))
        suppressed = reports.release(scope, suppressible_query, db.Execution([
            {'race': 'large', 'count': 7, '_contributors': 7}, {'race': 'small-a', 'count': 1, '_contributors': 1},
            {'race': 'small-b', 'count': 2, '_contributors': 2}, {'race': 'other-large', 'count': 3, '_contributors': 3}], True))
        blob = json.dumps([suppressed, suppressed['display_rows'], suppressed['notices']])
        self.assertIn('Small groups were suppressed.', blob)
        self.assertNotIn('small-a', blob)
        self.assertNotIn('small-b', blob)
        self.assertEqual([row['count'] for row in suppressed['rows']], [7, 3])

    def test_related_alerts_are_bound_to_released_transactions(self):
        result = self.ask(source='transactions', fields=['memo', 'amount_minor', 'currency'],
                          filters=[{'field': 'counterparty', 'op': 'eq', 'value': 'Vertex Holdings'}],
                          related=['alerts'])
        self.assertEqual(result.status, 'ok')
        self.assertEqual(len(result.released_results), 2)
        self.assertEqual(result.released_results[0]['rows'], [
            {'txn_id': 't004', 'memo': seed._INJECT_MEMO, 'amount_minor': 2300000, 'currency': 'USD'}])
        self.assertEqual(result.released_results[1]['rows'], [
            {'alert_id': 'a003', 'txn_id': 't004', 'rule_id': 'R-SANCTIONS', 'severity': 'high', 'status': 'OPEN'}])
        self.assertEqual(len(result.sql_log), 2)
        self.assertIn('a.txn_id IN', result.sql_log[1])
        self.assertNotIn('t004', result.sql_log[1])
        self.assertNotIn('invoice 9001', json.dumps(result.transcript))

    def test_related_failure_releases_no_partial_bundle(self):
        real = db.execute_plan
        calls = []
        def fail_second(plan, **kwargs):
            calls.append(plan)
            return real(plan, **kwargs) if len(calls) == 1 else db.Execution([], False, 'database_unavailable')
        with patch.object(db, 'execute_plan', side_effect=fail_second):
            result = self.ask(source='transactions', fields=['txn_id', 'memo'], related=['alerts'])
        self.assertEqual(result.status, 'unavailable')
        self.assertEqual(result.released_results, [])
        self.assertNotIn('invoice', result.answer)

    def test_related_plan_is_validated_before_either_query(self):
        for related in (['customers'], ['alerts', 'alerts'], 'alerts', {'role': 'compliance'}):
            with self.subTest(related=related), patch.object(db, 'execute_plan') as execute:
                result = self.ask(source='transactions', fields=['txn_id'], related=related)
                self.assertEqual(result.status, 'invalid_request')
                execute.assert_not_called()

    def test_note_rule_alias_resolves_to_canonical_id(self):
        result = baseline.run('Summarise the critical sanctions review.', 'u_rae', client=Planner({
            'action': 'report', 'source': 'case_notes', 'fields': ['body'], 'filters': [
                {'field': 'rule_id', 'op': 'eq', 'value': 'sanctions'},
                {'field': 'severity', 'op': 'eq', 'value': 'critical'},
                {'field': 'counterparty', 'op': 'eq', 'value': 'Cayman Trust'},
            ]}))
        self.assertEqual(result.status, 'ok')
        self.assertIn('Escalating for second review.', result.answer)
        self.assertEqual(result.released_results[0]['rows'], [{'body': seed._INJECT_NOTE}])
        self.assertNotIn('AP-7', json.dumps(result.transcript))
        self.assertNotIn('AP-7', json.dumps(result.audit))

    def test_case_note_lookup_respects_linked_channel_and_role(self):
        for actor, channel, status in [('u_rae', 'ACH', 'ok'), ('u_rae', 'wire', 'empty'),
                                       ('u_ana', 'ACH', 'access_denied')]:
            with self.subTest(actor=actor, channel=channel):
                model = Planner({'action': 'report', 'source': 'case_notes', 'fields': ['body'],
                                 'filters': [
                                     {'field': 'txn_id', 'op': 'eq', 'value': 't001'},
                                     {'field': 'channel', 'op': 'eq', 'value': channel},
                                 ]})
                result = baseline.run(f'Give me the gist of the review note on {channel} payment t001.',
                                      actor, client=model)
                self.assertEqual(result.status, status)
                catalog = json.loads(model.requests[0]['messages'][0]['content'].splitlines()[-1])['catalog']
                approved_note = 'Customer contacted; invoice provided. Awaiting doc upload.'
                self.assertNotIn(approved_note, json.dumps(model.requests))
                if status == 'access_denied':
                    self.assertFalse(result.sql_log)
                    self.assertNotIn('case_notes', catalog)
                else:
                    self.assertIn('channel', catalog['case_notes']['filter_fields'])
                    self.assertNotIn('body', catalog['case_notes']['filter_fields'])
                    self.assertEqual(result.released_results[0]['rows'],
                                     [{'body': approved_note}] if status == 'ok' else [])

    def test_clarification_completion_merges_filters_only_on_server(self):
        planner = Planner({'action': 'clarify', 'topic': 'outstanding', 'draft': {
            'source': 'alerts', 'metrics': ['count'],
            'filters': [{'field': 'txn_id', 'op': 'eq', 'value': 't007'}]}},
            {'action': 'complete', 'changes': {'filters': [{'field': 'status', 'op': 'eq', 'value': 'open'}]}})
        first = baseline.run('Count outstanding alerts for t007.', 'u_ana', 'follow', client=planner)
        self.assertEqual(first.status, 'clarified')
        result = baseline.run('Open only.', 'u_ana', 'follow', client=planner)
        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.released_results[0]['rows'], [{'count': 2}])
        self.assertNotIn('t007', json.dumps(planner.requests[-1]))

    def test_currency_clarification_preserves_draft_without_executing(self):
        proposal = {'action': 'clarify', 'topic': 'volume', 'draft': {
            'source': 'transactions', 'metrics': ['sum_amount_minor'], 'group_by': [], 'filters': []}}
        planner = Planner(proposal, {'action': 'complete', 'changes': {}},
                          {'action': 'complete', 'changes': {
                              'filters': [{'field': 'currency', 'op': 'eq', 'value': 'USD'}]}})
        def ask(question):
            return baseline.run(question, 'u_ana', 'volume', session_id='demo', client=planner)

        with patch.object(db, 'execute_plan', wraps=db.execute_plan) as execute:
            first = ask('What is the total transaction volume in my region?')
            self.assertEqual(first.status, 'clarified')
            self.assertIn('Which currency?', first.answer)
            self.assertEqual(ask('Use the total.').status, 'invalid_request')
            execute.assert_not_called()
            result = ask('Total in USD.')
            self.assertEqual(result.status, 'ok')
            self.assertIn('Currency: USD', result.report_summary)
            execute.assert_called_once()
        self.assertEqual(result.released_results[0]['rows'], [{'sum_amount_minor': 10841550}])

        for fields, status in [
                ({'scope': 'company'}, 'access_denied'),
                ({'filters': [{'field': 'currency', 'op': 'eq', 'value': 'QZZ'}]}, 'invalid_request')]:
            with self.subTest(fields=fields), patch.object(db, 'execute_plan') as execute:
                invalid = {**proposal, 'draft': {**proposal['draft'], **fields}}
                result = baseline.run('Clarify the total', 'u_ana', client=Planner(invalid))
                self.assertEqual(result.status, status)
                self.assertFalse(result.clarified)
                execute.assert_not_called()

    def test_eval_does_not_credit_invalid_response_for_clarifying(self):
        case = {'id': 'clarification', 'user': 'u_ana', 'question': 'Which volume?',
                'expect': {'kind': 'clarify'}}
        invalid = baseline.AgentResult(status='invalid_request', clarified=True,
                                       answer='The planner returned an invalid report.')
        with patch.object(run_eval, 'run_live', return_value=invalid), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_eval.run_questions([case], False)[0], 0)

    def test_audit_failure_cannot_publish_or_replace_previous_report(self):
        model = Planner({'action': 'report', 'source': 'customers', 'metrics': ['count']},
                        {'action': 'report', 'source': 'alerts', 'metrics': ['count']},
                        {'action': 'repeat'})
        baseline.run('Count customers', 'u_ana', 'chat', client=model, session_id='browser')
        with patch.object(baseline, 'append_audit', side_effect=OSError('full disk')):
            failed = baseline.run('Count alerts', 'u_ana', 'chat', client=model,
                                  session_id='browser')
        self.assertEqual(failed.status, 'unavailable')
        self.assertEqual(failed.released_results, [])
        result = baseline.run('Repeat it', 'u_ana', 'chat', client=model, session_id='browser')
        self.assertEqual(result.released_results[0]['rows'], [{'count': 6}])

    def test_changed_dates_preserve_previous_filters(self):
        model = Planner({'action': 'report', 'source': 'transactions', 'metrics': ['sum_amount_minor'],
                         'filters': [{'field': 'currency', 'op': 'eq', 'value': 'USD'},
                                     {'field': 'counterparty', 'op': 'eq', 'value': 'ACME LLC'},
                                     {'field': 'occurred_at', 'op': 'gte', 'value': '2026-03-01'},
                                     {'field': 'occurred_at', 'op': 'lt', 'value': '2026-04-01'}]},
                        {'action': 'revise', 'changes': {'filters': [
                            {'field': 'occurred_at', 'op': 'gte', 'value': '2026-04-01'},
                            {'field': 'occurred_at', 'op': 'lt', 'value': '2026-05-01'}]}})
        first = baseline.run('Report March ACME LLC payments in USD.', 'u_ana', 'chat', client=model, session_id='s')
        self.assertEqual(first.released_results[0]['rows'], [{'sum_amount_minor': 1250000}])
        result = baseline.run('Now April.', 'u_ana', 'chat', client=model, session_id='s')
        self.assertEqual(result.status, 'ok')
        self.assertIn('ACME LLC', result.report_summary)
        self.assertNotIn('ACME LLC', json.dumps(model.requests[-1]))
        self.assertEqual(result.released_results[0]['rows'], [{'sum_amount_minor': 670000}])

    def test_report_revision_can_remove_a_saved_filter_by_field(self):
        model = Planner({'action': 'report', 'source': 'alerts', 'metrics': ['count'], 'filters': [
            {'field': 'rule_id', 'op': 'eq', 'value': 'R-SANCTIONS'},
            {'field': 'status', 'op': 'eq', 'value': 'open'}]},
            {'action': 'revise', 'changes': {'remove_filters': ['rule_id']}})
        first = baseline.run('Count open sanctions alerts.', 'u_ana', 'revise', client=model,
                             session_id='s')
        self.assertEqual(first.released_results[0]['rows'], [{'count': 2}])
        result = baseline.run('Now include all rules.', 'u_ana', 'revise', client=model, session_id='s')
        self.assertEqual(result.status, 'ok')
        planner_context = model.requests[-1]['messages'][0]['content']
        self.assertNotIn('"filters"', planner_context.split('"previous_report_shape": ', 1)[-1].split('}, "pending_clarification"', 1)[0])
        self.assertEqual(result.released_results[0]['rows'], [{'count': 5}])

    def test_business_empty_metrics_have_explicit_meaning(self):
        result = self.ask(source='transactions', metrics=['count', 'sum_amount_minor', 'avg_amount_minor'],
                          filters=[{'field': 'currency', 'op': 'eq', 'value': 'USD'},
                                   {'field': 'txn_id', 'op': 'eq', 'value': 'missing'}])
        self.assertEqual(result.released_results[0]['rows'], [{'count': 0, 'sum_amount_minor': 0, 'avg_amount_minor': None}])
        self.assertIn('No matching transactions', result.answer)
        self.assertIn('average unavailable', result.answer)

    def test_explicit_excluded_population_is_access_denied(self):
        for field, op, value in [('deleted_at', 'ne', None), ('deleted_at', 'eq', '2026-02-01')]:
            with self.subTest(op=op), patch.object(db, 'execute_plan', wraps=db.execute_plan) as execute:
                result = self.ask(source='customers', fields=['customer_id'], filters=[{'field': field, 'op': op, 'value': value}])
                self.assertEqual(result.status, 'access_denied')
                execute.assert_not_called()
        empty = self.ask(source='customers', fields=['customer_id'], filters=[{'field': 'customer_id', 'op': 'eq', 'value': 'missing'}])
        self.assertEqual(empty.status, 'empty')

    def test_admin_schema_types_and_missing_table_status(self):
        result = baseline.run('Schema please.', 'u_ops', client=Planner({'action': 'schema'}))
        schema = json.loads(result.answer)
        self.assertIsInstance(schema['customers'], dict)
        self.assertEqual(schema['customers']['risk_score'], 'INTEGER')
        with contextlib.closing(db.connect(read_only=False)) as connection, connection:
            connection.execute('DROP TABLE case_notes')
        result = baseline.run('Service status.', 'u_ops', client=Planner({'action': 'status'}))
        self.assertEqual(result.status, 'unavailable')
        self.assertNotIn('customers=15', result.answer)

    def test_money_chart_and_table_share_major_units_and_currency(self):
        result = self.ask(source='transactions', metrics=['sum_amount_minor'], group_by=['channel', 'currency'], chart=True)
        report = result.released_results[0]
        self.assertIn('display_rows', report)
        self.assertEqual(result.charts[0]['data']['values'], report['display_rows'])
        self.assertEqual(report['units']['sum_amount_minor'], 'major currency units')
        self.assertIn('major units', result.charts[0]['encoding']['y']['title'])

    def test_startup_database_failure_is_safe_in_browser(self):
        from streamlit.testing.v1 import AppTest
        with patch.object(db, 'DB_PATH', db.DB_PATH.parent / 'absent.db'):
            app = AppTest.from_file(str(PROJECT / 'app.py')).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertIn('warehouse', app.error[0].value.lower())

    def test_catalog_exposes_types_operators_and_safe_reference_shape(self):
        model = Planner({'action': 'report', 'source': 'customers', 'metrics': ['count']})
        baseline.run('Count', 'u_ana', client=model)
        context = json.loads(model.requests[0]['messages'][0]['content'].splitlines()[-1])
        entry = context['catalog']['customers']
        self.assertIn('types', entry)
        self.assertEqual(entry['types']['birth_year'], 'INTEGER')
        self.assertIn('eq', entry['operators']['risk_score'])
        self.assertNotIn('prefix', entry['operators']['birth_year'])
        self.assertIn('prefix', entry['operators']['full_name'])
        self.assertNotIn('dob', entry['types'])

    def test_planner_response_and_provider_identity_are_in_trace(self):
        model = Planner({'action': 'report', 'source': 'customers', 'metrics': ['count']})
        original = model.create
        def create(**request):
            response = original(**request)
            response.id, response.model = 'response-test-1', 'planner-test-model'
            return response
        model.chat.completions.create = create
        result = baseline.run('Count', 'u_ana', client=model)
        self.assertTrue(any(line.startswith('[planner]') for line in result.transcript))
        self.assertEqual(getattr(result, 'provider', {}), {'response_id': 'response-test-1', 'model': 'planner-test-model'})

    def test_money_detail_includes_currency_in_all_displays(self):
        result = self.ask(source='transactions', fields=['txn_id', 'amount_minor'])
        self.assertIn('currency', result.released_results[0]['columns'])

    def test_canonical_filter_values_match_activity_and_query(self):
        result = self.ask(source='alerts', fields=['alert_id'], filters=[{'field': 'rule_id', 'op': 'eq', 'value': 'sanctions'}])
        self.assertIn('R-SANCTIONS', result.report_summary)
        self.assertIn('R-SANCTIONS', result.audit['queries'][0]['bindings'])

    def test_actor_population_predicate_is_compiled_once(self):
        result = self.ask(source='customers', metrics=['count'], filters=[
            {'field': 'deleted_at', 'op': 'eq', 'value': None}])
        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.sql_log[0].count('c.deleted_at IS NULL'), 1)

    def test_concurrent_submission_in_the_same_conversation_executes_once(self):
        plan = {'action': 'report', 'source': 'customers', 'metrics': ['count']}
        entered, release = Event(), Event()
        real = db.execute_plan
        def blocked(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return real(*args, **kwargs)
        with patch.object(db, 'execute_plan', side_effect=blocked) as execute, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(baseline.run, 'Count', 'u_ana', 'concurrent', client=Planner(plan), session_id='s')
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(baseline.run, 'Count', 'u_ana', 'concurrent',
                                     client=Planner(plan), session_id='s').result(3)
                self.assertEqual(second.status, 'busy')
                self.assertFalse(first.done())
            finally:
                release.set()
            self.assertEqual(first.result(5).status, 'ok')
            self.assertEqual(execute.call_count, 1)

    def test_all_date_fields_generated_rejections_and_leap_day(self):
        cases = [('customers', 'dob'), ('customers', 'onboarded_at'), ('customers', 'deleted_at'),
                 ('case_notes', 'created_at'), ('transactions', 'occurred_at')]
        for source, field in cases:
            for value in ('2026-02-29', '2024-13-01', '2024-2-01', '2024-02-29T24:00:00Z', '2024-02-29Z'):
                with self.subTest(field=field, value=value), patch.object(db, 'execute_plan', wraps=db.execute_plan) as execute:
                    result = baseline.run('Find', 'u_cora', client=Planner({'action': 'report', 'source': source,
                        'fields': [field], 'filters': [{'field': field, 'op': 'eq', 'value': value}]}))
                    self.assertEqual(result.status, 'invalid_request')
                    execute.assert_not_called()
            valid = baseline.run('Find', 'u_cora', client=Planner({'action': 'report', 'source': source,
                'fields': [field], 'filters': [{'field': field, 'op': 'eq', 'value': '2024-02-29'}]}))
            self.assertEqual(valid.status, 'empty')

    def test_ownership_scope_across_all_sources(self):
        # Independent fixture ownership oracle, not the compiler's join expressions.
        customers = {row[0]: row for row in seed.CUSTOMERS}
        transactions = {row[0]: row for row in seed.TRANSACTIONS}
        alerts = {row[0]: row for row in seed.ALERTS}
        for actor, region, active, sources in [
            ('u_ana', 'WEST', True, ('customers', 'transactions', 'alerts')),
            ('u_ben', 'EAST', True, ('customers', 'transactions', 'alerts')),
            ('u_rae', 'WEST', True, ('customers', 'transactions', 'alerts', 'case_notes')),
            ('u_cora', None, False, ('customers', 'transactions', 'alerts', 'case_notes'))]:
            for source in sources:
                rows = {'customers': seed.CUSTOMERS, 'transactions': seed.TRANSACTIONS,
                        'alerts': seed.ALERTS, 'case_notes': seed.CASE_NOTES}[source]
                expected = []
                for row in rows:
                    customer = row if source == 'customers' else customers[row[1]] if source == 'transactions' else customers[transactions[row[1]][1]] if source == 'alerts' else customers[transactions[alerts[row[1]][1]][1]]
                    if (region is None or customer[2] == region) and (not active or customer[-1] is None):
                        expected.append(row[0])
                primary_key = {'customers': 'customer_id', 'transactions': 'txn_id', 'alerts': 'alert_id', 'case_notes': 'note_id'}[source]
                result = baseline.run('List', actor, client=Planner({'action': 'report', 'source': source, 'fields': [primary_key]}))
                self.assertEqual([r[primary_key] for r in result.released_results[0]['rows']], sorted(expected))

    def test_eval_accepts_successful_clarification_setup(self):
        case = {'id': 'test', 'user': 'u_ana', 'previous_question': 'Outstanding?',
                'previous_status': 'clarified', 'question': 'Open only', 'expect': {'kind': 'number', 'value': 5}}
        results = [baseline.AgentResult(status='clarified', clarified=True), baseline.AgentResult(answer='5')]
        with patch.object(run_eval, 'run_live', side_effect=results), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_eval.run_questions([case], False)[0], 1)

    def test_eval_records_failure_before_marking_run_incomplete(self):
        for reason, message in [(None, 'planner unavailable'),
                                ('planner_authentication_failed', 'API credential was rejected')]:
            with self.subTest(reason=reason):
                failure = baseline.AgentResult(status='unavailable', error='planner_unavailable',
                                               reason_code=reason, elapsed_seconds=0.5)
                with patch.object(baseline, 'run', return_value=failure), patch.object(run_eval, 'record_measurement') as record:
                    with self.assertRaisesRegex(run_eval.EvalIncomplete, message):
                        run_eval.run_live('failed', 'Count', 'u_ana')
                    record.assert_called_once()

    def test_structured_eval_accepts_extra_authorized_identifier(self):
        case = {'id': 'edge_generalized', 'user': 'u_ana', 'question': 'Generalize c001.',
                'expect': {'kind': 'rows', 'subset': True, 'rows': [
                    {'zip3': '941', 'birth_year': 1979, 'income_band': 'high'}]}}
        result = baseline.AgentResult(status='ok', released_results=[{'rows': [
            {'customer_id': 'c001', 'zip3': '941', 'birth_year': 1979, 'income_band': 'high'}]}])
        with patch.object(run_eval, 'run_live', return_value=result), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_eval.run_questions([case], False)[0], 1)

    def test_eval_evidence_works_with_relative_entry_point(self):
        import runpy
        path = self.audit_path.parent / 'trace.jsonl'
        with patch.object(sys, 'argv', ['eval/run_eval.py', '--ids', 'b3', '--evidence', str(path)]), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path('eval/run_eval.py', run_name='__main__')
        self.assertEqual(caught.exception.code, 0)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertIn('source_sha256', records[0])
        self.assertEqual(records[1]['case_id'], 'b3')

    def test_related_report_survives_clarification_draft(self):
        model = Planner({'action': 'clarify', 'topic': 'generic', 'draft': {
            'source': 'transactions', 'fields': ['txn_id', 'memo'],
            'filters': [{'field': 'counterparty', 'op': 'eq', 'value': 'Vertex Holdings'}], 'related': ['alerts']}},
            {'action': 'complete', 'changes': {}})
        first = baseline.run('Which payment?', 'u_ana', 'related', client=model)
        self.assertEqual(first.status, 'clarified')
        second = baseline.run('Use that counterparty.', 'u_ana', 'related', client=model)
        self.assertEqual(second.status, 'ok')
        self.assertEqual(len(second.released_results), 2)
        self.assertNotIn('Vertex Holdings', json.dumps(model.requests[-1]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
