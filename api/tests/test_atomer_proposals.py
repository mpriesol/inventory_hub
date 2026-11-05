import unittest
from inventory_hub.services.atomer_proposals import suggestions


class AcceptedSuggestionsTest(unittest.TestCase):
    def test_shop_scope_limits_directions(self):
        row=dict(state='needs_target',card_state='exists',bike_state='unlinked',reasons=[])
        self.assertEqual(suggestions(row,'xtrek'),['targetX'])
        self.assertEqual(suggestions(row,'biketrek'),['linkB'])
        self.assertEqual(suggestions(row,'both'),['targetX','linkB'])

    def test_card_and_identity_are_visible_together(self):
        row=dict(state='review',card_state='missing',bike_state='no_card',reasons=['kind_differs'])
        self.assertEqual(suggestions(row,'default'),['identity','card'])
