"""Deterministic advisory matching and bounded read-only query construction."""
from contextlib import contextmanager
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.dialects import postgresql

from inventory_hub.services import product_candidates as service


EAN = '5901234123457'


def card(identifier=1, **changes):
    return {'product_id': identifier, 'sku': '00042-BLUE-M', 'name': 'Modrá turistická bunda',
            'brand': 'North', 'eans': [EAN], 'manual_eans': None,
            'attributes': [{'name': 'Size', 'value': 'M'}], 'is_active': True, **changes}


class CandidateRankingTests(unittest.TestCase):
    def test_exact_evidence_ranks_before_names_and_does_not_authorize_a_link(self):
        query = service.CandidateQuery('row', sku='00042-BLUE-M', name='Modrá turistická bunda')
        cards = {1: card(), 2: card(2, sku='OTHER', name='Completely different')}
        result = service._rank(query, cards, {2: {'exact_ean'}, 1: {'exact_sku'}}, 1, False)
        self.assertEqual(result['candidates'][0]['product_id'], 2)
        self.assertEqual(result['candidates'][0]['reasons'], ['exact_ean'])
        self.assertTrue(result['truncated'])
        self.assertNotIn('can_link', result['candidates'][0])

    def test_similar_codes_names_and_case_differences_are_advisory_only(self):
        query = service.CandidateQuery('row', sku='00042-blue-m', name='Modra turisticka bunda')
        result = service._rank(query, {1: card()}, {}, 5, False)['candidates'][0]
        self.assertEqual(result['reasons'], ['similar_sku', 'similar_name'])
        self.assertNotIn('exact_sku', result['reasons'])
        self.assertEqual(result['sku'], '00042-BLUE-M')

    def test_conflicts_are_visible_and_empty_manual_eans_stay_unknown(self):
        query = service.CandidateQuery('row', eans=('4006381333931',), brand='Other', attributes=(('Size', 'L'),))
        item = card(is_active=False, manual_eans=[])
        result = service._rank(query, {1: item}, {1: {'supplier_sku'}}, 5, False)['candidates'][0]
        self.assertEqual(result['warnings'], ['inactive_product', 'ean_conflict', 'brand_difference', 'variant_axes_conflict'])
        self.assertEqual(result['manual_eans'], [])
        self.assertEqual(result['eans'], [EAN])
        self.assertEqual(item['manual_eans'], [])

    def test_invalid_barcode_cannot_produce_conflict_or_remove_leading_zero(self):
        query = service.CandidateQuery('row', eans=('1234567890123', '0000000000000'))
        result = service._rank(query, {1: card()}, {1: {'exact_sku'}}, 5, False)['candidates'][0]
        self.assertEqual(result['warnings'], [])
        self.assertEqual(service.verified_barcodes(('012345678905', '12345678905')), ('012345678905',))

    def test_axis_aliases_share_the_definitive_link_normalizer(self):
        query = service.CandidateQuery('row', attributes=(('Farba', 'modrá'), ('Veľkosť', 'm')))
        matching = card(attributes=[{'name': 'Color', 'value': 'blue'}, {'name': 'Size', 'value': 'M'}])
        result = service._rank(query, {1: matching}, {1: {'exact_ean'}}, 5, False)['candidates'][0]
        self.assertEqual(result['warnings'], [])
        matching['attributes'].append({'name': 'Farba', 'value': 'červená'})
        result = service._rank(query, {1: matching}, {1: {'exact_ean'}}, 5, False)['candidates'][0]
        self.assertIn('variant_axes_ambiguous', result['warnings'])

    def test_exclusion_and_ties_are_deterministic(self):
        query = service.CandidateQuery('row', sku='00042-BLUE-M', exclude_product_id=2)
        result = service._rank(query, {3: card(3), 2: card(2), 1: card(1)}, {}, 5, False)
        self.assertEqual([item['product_id'] for item in result['candidates']], [1, 3])
        self.assertFalse(result['truncated'])

    def test_incomplete_pool_is_explicit_even_when_no_candidate_survives(self):
        result = service._rank(service.CandidateQuery('row', name='Unrelated'), {1: card()}, {}, 5, True)
        self.assertEqual(result, {'candidates': [], 'truncated': True})


class EmptyResult:
    def all(self):
        return []


class ReadSession:
    def __init__(self):
        self.statements, self.suppressed = [], False

    @property
    @contextmanager
    def no_autoflush(self):
        self.suppressed = True
        try:
            yield
        finally:
            self.suppressed = False

    async def execute(self, statement):
        assert self.suppressed
        assert statement.is_select
        self.statements.append(statement)
        return EmptyResult()

    async def scalars(self, statement):
        await self.execute(statement)
        return []


class CandidateSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_search_is_bounded_read_only_and_escapes_like_tokens(self):
        db = ReadSession()
        queries = [service.CandidateQuery(str(index), sku='AB_%' + str(index), name='Winter_100% coat',
            eans=(EAN,), supplier_id=7, supplier_sku='000123') for index in range(100)]
        with patch.object(service, '_load_cards', AsyncMock(return_value={})):
            result = await service.find_candidates(db, queries)
        self.assertEqual(len(result), 100)
        self.assertEqual(len(db.statements), 4, 'One set of batched reads, not queries per input row')
        for statement in db.statements:
            statement.compile(dialect=postgresql.dialect())
        fuzzy = db.statements[-1].compile(dialect=postgresql.dialect())
        self.assertIn(service.FUZZY_POOL_LIMIT + 1, fuzzy.params.values())
        self.assertIn('ESCAPE', str(fuzzy))
        self.assertIn('winter/_100', fuzzy.params.values())
        self.assertFalse(db.suppressed)

    async def test_invalid_unverified_ean_and_short_terms_skip_queries(self):
        db = ReadSession()
        result = await service.find_candidates(db, [service.CandidateQuery('row', eans=('1234', '00000000'), name='M')])
        self.assertEqual(result, {'row': {'candidates': [], 'truncated': False}})
        self.assertEqual(db.statements, [])

    async def test_invalid_or_oversized_input_fails_before_database_access(self):
        invalid = ([service.CandidateQuery('same'), service.CandidateQuery('same')],
                   [service.CandidateQuery(str(index)) for index in range(101)],
                   [service.CandidateQuery('one', supplier_id=True)],
                   [service.CandidateQuery('one', sku='x' * 101)],
                   [service.CandidateQuery('one', attributes=(('Size',),))])
        db = ReadSession()
        for queries in invalid:
            with self.subTest(queries=queries), self.assertRaises(ValueError):
                await service.find_candidates(db, queries)
        with self.assertRaises(ValueError):
            await service.find_candidates(db, [], limit=11)
        self.assertEqual(db.statements, [])
