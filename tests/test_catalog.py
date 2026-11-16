"""The planner's advertised operations must match the compiler's permissions."""
import itertools
import json
import unittest

from agent import baseline, db, policy, reports
import test_repairs as helpers


class Catalog(unittest.TestCase):
    setUp = helpers.Gaps.setUp

    def test_prefix_is_advertised_only_for_fields_that_accept_it(self):
        for actor in ('u_ana', 'u_rae', 'u_cora', 'u_fern', 'u_ops'):
            scope = policy.resolve_scope(db.get_user(actor))
            for source, entry in baseline.catalog(scope).items():
                for field in entry['date_fields'] + entry['utc_timestamp_fields']:
                    with self.subTest(actor=actor, source=source, field=field):
                        self.assertNotIn('prefix', entry['operators'][field])
        scope = policy.resolve_scope(db.get_user('u_ana'))
        entry = baseline.catalog(scope)['transactions']
        self.assertIn('prefix', entry['operators']['counterparty'])
        report = reports.parse_request({
            'source': 'transactions', 'mode': 'summary', 'metrics': ['count'],
            'filters': [{'field': 'counterparty', 'op': 'prefix', 'value': 'Vertex'}],
        })
        self.assertTrue(reports.compile_report(scope, report).params)

    def test_catalog_explicitly_advertises_permitted_operations(self):
        for actor in ('u_ana', 'u_rae', 'u_cora', 'u_fern', 'u_ops'):
            scope = policy.resolve_scope(db.get_user(actor))
            for source, entry in baseline.catalog(scope).items():
                with self.subTest(actor=actor, source=source):
                    self.assertIn('modes', entry)
                    self.assertEqual(entry['modes'], ['summary'] if actor == 'u_fern'
                                     else ['detail', 'summary'])
                    for field in entry['detail_fields']:
                        reports.compile_report(scope, reports.parse_request({
                            'source': source, 'mode': 'detail', 'fields': [field]}))
                    for field, metric in itertools.product(entry['group_by_fields'], entry['metrics']):
                        filters = ([{'field': 'currency', 'op': 'eq', 'value': 'USD'}]
                                   if source == 'transactions' and metric != 'count' else [])
                        reports.compile_report(scope, reports.parse_request({
                            'source': source, 'mode': 'summary', 'group_by': [field],
                            'metrics': [metric], 'filters': filters}))
                    self.assertFalse({'memo', 'body'} & set(entry['group_by_fields']))
                    self.assertFalse(set(policy.T5) & set(entry['detail_fields']))

    def test_protected_summary_contract_includes_averages_without_identifiers(self):
        scope = policy.resolve_scope(db.get_user('u_fern'))
        entry = baseline.catalog(scope)['customers']
        self.assertIn('detail_fields', entry)
        self.assertEqual(entry['detail_fields'], [])
        self.assertEqual(set(entry['protected_summary']['group_by_fields']), {'race', 'ethnicity', 'sex'})
        self.assertEqual(entry['protected_summary']['max_group_by'], 1)
        self.assertIn('avg_annual_income_usd', entry['protected_summary']['metrics'])
        self.assertNotIn('customer_id', json.dumps(entry))
        self.assertNotIn('full_name', json.dumps(entry))
        self.assertFalse(set(policy.T5) & set(entry['filter_fields']))
        for groups in itertools.permutations(policy.T5, 2):
            with self.assertRaises(reports.ReportError) as error:
                reports.compile_report(scope, reports.parse_request({
                    'source': 'customers', 'mode': 'summary', 'group_by': list(groups),
                    'metrics': ['avg_annual_income_usd']}))
            self.assertEqual(error.exception.status, 'access_denied')

    def test_provider_receives_mandatory_population_separately_from_filters(self):
        for actor, population, region in [('u_ana', 'active', 'WEST'),
                                          ('u_cora', 'including_offboarded', None),
                                          ('u_fern', 'active', None),
                                          ('u_ops', 'user_records', None)]:
            client = helpers.Planner({'action': 'decline', 'reason': 'unsupported'})
            baseline.run('Describe the permitted operation', actor, client=client)
            content = client.requests[0]['messages'][0]['content']
            context = json.loads(content.split('\n')[-1])
            self.assertIn('mandatory_population', context)
            self.assertEqual(context['mandatory_population'], population)
            self.assertEqual(context['region'], region)


if __name__ == '__main__':
    unittest.main()
