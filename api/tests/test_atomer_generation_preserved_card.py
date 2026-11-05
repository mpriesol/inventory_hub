from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from inventory_hub.services import atomer_generations as gen, atomer_generation_preserved_card as keep
from inventory_hub.services.atomer_review_targets import ReviewError


class PreservedGenerationTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.pin=gen.PreservedCard(id=32,bike_link=63,bike_product='4',bike_variant='20',bike_parent='POS',on_hand=2)
        self.bare=gen.Addition(code='B',ean='4006381333931',revision=1,names={'PARAM_5':'Size'},generation='Model B')
        self.held=self.bare.model_copy(update={'preserved_card':self.pin})
        old=gen.Existing(code='A',ean='9780306406157',variant_id=49,axes={'Size':'S'},generation='Model A')
        self.family=gen.Family(parent='P',product_id=2,family_revision=0,evidence='https://manufacturer.example/p',existing=[old],rows=[self.held])
        self.sel=gen.Selection(run_id=4,source_sha='1'*64,phase='extend',families=[self.family])
        self.job=SimpleNamespace(state='verified',document={'observed_ids':{'B':500}})
        self.shop=SimpleNamespace(id=3)

    def test_previous_selections_still_address_the_original_journals(self):
        bare=self.family.model_copy(update={'rows':[self.bare]})
        data=bare.model_dump(mode='json'); data['rows'][0].pop('preserved_card')
        old=gen.repair.digest(dict(operation='variant_generation',phase='extend',run=4,source=self.sel.source_sha,family=data))
        self.assertEqual(old,gen.doc_id(self.sel,bare))
        self.assertNotEqual(old,gen.doc_id(self.sel,self.family))

    async def test_preview_checks_known_card_and_keeps_biketrek_link_without_new_card(self):
        db=AsyncMock(); db.scalar.return_value=None
        with patch.object(keep,'check',new=AsyncMock(return_value=None)) as check,patch.object(gen,'observe',return_value=({'B':500},[])):
            result=await keep.attach(db,self.sel,{'P':self.job},{2:{}},self.shop,'preview')
        check.assert_awaited_once_with(db,self.held,target=('2','500','P'),read_bike=True)
        db.flush.assert_not_awaited()
        self.assertEqual(result['links']['B'],{'card_id':32,'bike_link':63,'xtrek_link':None,'product_id':2,'variant_id':500})

    async def test_refuses_unverified_fields_or_another_link_owner(self):
        db=AsyncMock()
        for state,issues,owner in [('uncertain',[],None),('verified',['price'],None),('verified',[],919)]:
            self.job.state=state; db.scalar.return_value=owner
            with patch.object(keep,'check',new=AsyncMock(return_value=None)),patch.object(gen,'observe',return_value=({'B':500},issues)),self.assertRaises(ReviewError):
                await keep.attach(db,self.sel,{'P':self.job},{2:{}},self.shop,'apply')
        db.flush.assert_not_awaited()
