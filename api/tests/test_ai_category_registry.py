"""Registry scopes, partial content and request inspection without paid calls."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from inventory_hub.ai_content_types import CategoryProfile, ParameterDefinition, Rule, RuleBook, Scope
from inventory_hub.services import ai_content_rules as rules, ai_content_provider as provider
from inventory_hub.services.ai_content_validation import validate_content
from test_ai_content import content, context


class RegistryTests(unittest.TestCase):
    def test_shared_blocks_are_selected_once_and_never_leak_to_other_shop(self):
        book = RuleBook(categories=[CategoryProfile(id=n, name=n) for n in ['bikes','pumps','general']],
            rules=[Rule(id='shared', name='shared', scope=Scope(shop='biketrek'), category_profiles=['bikes','pumps'], instructions='shared text')])
        self.assertEqual([r['id'] for r in rules.resolve(book, Scope(shop='biketrek',category='bikes'))['instructions']], ['shared'])
        self.assertEqual(rules.resolve(book, Scope(shop='biketrek',category='general'))['instructions'], [])
        self.assertEqual(rules.resolve(book, Scope(shop='xtrek',category='bikes'))['instructions'], [])
        with self.assertRaises(ValueError):
            RuleBook(rules=[Rule(id='broken',name='broken',category_profiles=['absent'])])

    def test_exact_mapping_wins_and_ambiguous_mapping_does_not_guess(self):
        bike=CategoryProfile(id='bikes',name='Bikes',shop_categories={'biketrek':'ROOT'},shop_category_matches={'biketrek':['LEAF']})
        book=RuleBook(categories=[bike])
        self.assertEqual(rules.select_category_profile(book,'biketrek','LEAF'), 'bikes')
        self.assertEqual(rules.select_category_profile(book,'xtrek','LEAF'), 'general')
        self.assertEqual(rules.select_category_profile(book,'biketrek','LEAF','manual'), 'manual')
        book.categories.append(CategoryProfile(id='other',name='Other',shop_category_matches={'biketrek':['LEAF']}))
        self.assertEqual(rules.select_category_profile(book,'biketrek','LEAF'), 'general')
        book.categories[-1].shop_categories={'biketrek':'LEAF'}
        self.assertEqual(rules.select_category_profile(book,'biketrek','LEAF'), 'other')

    def test_draft_fields_never_enter_prompt_or_validation(self):
        book=RuleBook(categories=[CategoryProfile(id='bikes',name='Bikes',parameters=[
            ParameterDefinition(name='Weight'),ParameterDefinition(name='Proposed',approved=False,required=True)])])
        ctx=context(resolved=rules.resolve(book,Scope(category='bikes')))
        self.assertEqual([p['name'] for p in ctx['resolved']['category']['parameters']], ['Weight'])
        self.assertEqual(validate_content(content(warnings=['Weight could not be found']),ctx)['errors'], [])
        invalid=content(parameters=[dict(name='Proposed',values=['1'],product_id=None)])
        self.assertIn('ai_unregistered_parameter:Proposed',validate_content(invalid,ctx)['errors'])

    def test_choice_is_parent_without_axis_and_protects_real_variant_identity(self):
        ctx=context()
        ctx['resolved']['category']['parameters']=[ParameterDefinition(name='Farba',scope='choice',values=['Red','Blue']).model_dump()]
        parent=content(parameters=[dict(name='Farba',values=['Red'],product_id=None)])
        self.assertEqual(validate_content(parent,ctx)['errors'], [])
        ctx['facts'][0]['variant_attributes']=[dict(name='Farba',value='Red')]
        self.assertIn('ai_parameter_scope:Farba',validate_content(parent,ctx)['errors'])
        variant=content(parameters=[dict(name='Farba',values=['Red'],product_id=1)])
        self.assertEqual(validate_content(variant,ctx)['errors'], [])
        changed=content(parameters=[dict(name='Farba',values=['Blue'],product_id=1)])
        self.assertIn('ai_variant_identity_change:Farba',validate_content(changed,ctx)['errors'])
        self.assertEqual(validate_content(content(),ctx)['errors'], [],'Absent optional values never block')
        self.assertIn('ai_missing_facts',validate_content(content(missing_facts=['Contradictory product identity']),ctx)['errors'])

    def test_optional_parameters_still_checked_for_invalid_values(self):
        ctx=context()
        ctx['resolved']['category']['parameters']=[ParameterDefinition(name='Brake',values=['Disc']).model_dump()]
        bad=content(parameters=[dict(name='Brake',values=['Invented'],product_id=None)])
        self.assertIn('ai_parameter_value:Brake',validate_content(bad,ctx)['errors'])
        prompt=provider.request_body(ctx)['instructions']
        self.assertIn('všetky parametre registra',prompt)
        self.assertIn('required=false',prompt)
        self.assertIn('vo warnings',prompt)


class RequestInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_endpoint_uses_pinned_context_without_mutating_or_generating(self):
        from inventory_hub.routers.ai_content import job_request
        ctx=context(rules_version=42)
        job=SimpleNamespace(context=ctx,kind='product',status='estimate',revision=1)
        with patch('inventory_hub.routers.ai_content.service.get_job',AsyncMock(return_value=job)), patch.object(provider,'generate',AsyncMock()) as generate:
            result=await job_request('fixture',object())
        self.assertEqual(result['rules_version'],42)
        self.assertEqual(result['request'],provider.request_body(ctx))
        self.assertFalse(result['generated'])
        self.assertEqual((job.status,job.revision),('estimate',1))
        generate.assert_not_called()
