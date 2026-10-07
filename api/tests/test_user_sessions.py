import unittest
from types import SimpleNamespace
from fastapi import HTTPException, Request, Response
from inventory_hub.services import auth
from inventory_hub.access import operator_access, ai_access


def request(method='POST', headers=None):
    return Request({'type':'http','method':method,'path':'/test','headers':[(k.encode(),v.encode()) for k,v in (headers or {}).items()]})


class UserSessionTests(unittest.TestCase):
    def test_password_hash_is_salted_and_invalid_password_rejected(self):
        password='isolated-password-fixture'
        first,second=auth.password_hash(password),auth.password_hash(password)
        self.assertNotEqual(first,second)
        self.assertNotIn(password,first)
        self.assertTrue(auth.password_matches(password,first))
        self.assertFalse(auth.password_matches('wrong',first))
        self.assertFalse(auth.password_matches(password,'malformed'))

    def test_session_cookie_is_not_readable_by_javascript(self):
        response=Response()
        auth.set_cookie(response,'synthetic-session')
        cookie=response.headers['set-cookie']
        for attribute in ('HttpOnly','Secure','SameSite=strict','Path=/','Max-Age='):
            self.assertIn(attribute,cookie)
        self.assertNotIn('Domain=',cookie)
        self.assertEqual(response.headers['cache-control'],'no-store')

    def test_cookie_guards_reject_cross_origin_and_simple_form_writes(self):
        for headers in ({},{'x-hub-request':'1','origin':'https://evil.test','host':'hub.test'},
                        {'x-hub-request':'1','sec-fetch-site':'cross-site'}):
            req=request(headers=headers);req.state.hub_user={'role':'operator'}
            for guard in (operator_access,ai_access):
                with self.assertRaises(HTTPException) as error:guard(None,req)
                self.assertEqual(error.exception.status_code,403)
        good=request(headers={'host':'hub.test','origin':'https://hub.test','x-hub-request':'1'})
        good.state.hub_user={'role':'operator'}
        operator_access(None,good);ai_access(None,good)

    def test_public_account_does_not_return_hashes(self):
        user=SimpleNamespace(id=1,username='fixture',display_name='Fixture',role='operator',active=True,password_hash='private')
        self.assertNotIn('password_hash',auth.public_user(user))
