import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import delete, select, text

from inventory_hub.access import _require_token, operator_access
from inventory_hub.auth_models import HubUser, HubSession
from inventory_hub.database import get_session_context
from inventory_hub.services import auth
from inventory_hub.settings import settings


# Everything else, including legacy routes, downloads and API documentation,
# is private. Match methods and complete paths, never a path prefix.
PUBLIC_ENDPOINTS = frozenset({
    ('GET', '/health'), ('HEAD', '/health'),
    ('GET', '/auth/session'), ('POST', '/auth/login'),
    ('POST', '/auth/logout'), ('POST', '/auth/token-session'),
    # Each exact delivery route verifies its own shop-scoped Basic credential.
    ('POST', '/webhooks/upgates/orders/create'),
    ('POST', '/webhooks/upgates/orders/update'),
    ('POST', '/webhooks/upgates/orders/delete'),
})


def route_path(request):
    path = request.scope.get('path', '')
    root = request.scope.get('root_path', '').rstrip('/')
    if root and path.startswith(root + '/'):
        path = path[len(root):]
    return path


def private_headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return response


def install(app):
    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Pydantic's default response echoes rejected inputs and exception
        # context, which can include passwords, tokens or credential URLs.
        return private_headers(JSONResponse(status_code=422, content={
            'detail': {'code': 'hub_request_invalid', 'message': 'Invalid request fields'},
        }))

    @app.middleware('http')
    async def session_boundary(request: Request, call_next):
        request.state.hub_user = None
        request.state.hub_bearer = False
        token = request.cookies.get(auth.COOKIE)
        if token and settings.USE_POSTGRES:
            async with get_session_context() as db:
                request.state.hub_user = await auth.session_user(db, token)
        valid_cookie = request.state.hub_user is not None
        # Upgrade already-authorized operator tabs during deployment, without
        # exposing or persisting the old shared credential in browser storage.
        authorization = request.headers.get('authorization')
        if authorization:
            try:
                _require_token(authorization)
                request.state.hub_bearer = True
                request.state.hub_user = auth.public_user(None)
            except HTTPException:
                pass
        path = route_path(request)
        if (request.method, path) not in PUBLIC_ENDPOINTS:
            try:
                operator_access(authorization, request)
            except HTTPException as error:
                return private_headers(JSONResponse(status_code=error.status_code,
                    content={'detail': error.detail}, headers=error.headers))
        response = await call_next(request)
        private_headers(response)
        if (settings.USE_POSTGRES and request.state.hub_bearer and not valid_cookie and response.status_code < 400
                and request.headers.get('sec-fetch-site') == 'same-origin'
                and path not in ('/auth/logout', '/auth/token-session')):
            async with get_session_context() as db:
                await auth.new_session(db, response)
        return response
    app.include_router(router)


class AuthRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                # FastAPI validation output normally includes input values.
                raise HTTPException(422, detail={'code': 'hub_account_invalid', 'message': 'Check the account fields'}) from None
        return handler


router = APIRouter(prefix='/auth', tags=['Accounts'], route_class=AuthRoute)


class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1, max_length=80, pattern=r'^[a-zA-Z0-9_.@-]+$')
    password: SecretStr = Field(min_length=1, max_length=200)


class Account(Login):
    display_name: str = Field(min_length=1, max_length=120)
    password: SecretStr = Field(min_length=12, max_length=200)
    role: Literal['admin', 'operator'] = 'operator'


def admin(request):
    principal = getattr(request.state, 'hub_user', None)
    if principal is not None and principal['role'] != 'admin':
        raise HTTPException(403, detail={'code': 'hub_admin_required', 'message': 'Administrator account required'})


@router.get('/session')
async def session(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    principal = getattr(request.state, 'hub_user', None)
    cookie = request.cookies.get(auth.COOKIE)
    if settings.USE_POSTGRES and principal and cookie and not getattr(request.state, 'hub_bearer', False):
        async with get_session_context() as db:
            row = await db.get(HubSession, auth.fingerprint(cookie), with_for_update=True)
            if row:
                row.expires_at = auth.now() + auth.timedelta(seconds=auth.SESSION_SECONDS)
                auth.set_cookie(response, cookie)
            else:
                principal = None
    return {'user': principal}


@router.post('/login')
async def login(body: Login, request: Request, response: Response):
    auth.csrf(request)
    async with get_session_context() as db:
        user, error = await auth.login(db, body.username.lower(), body.password.get_secret_value(), request.client.host if request.client else 'unknown')
        if not error:
            result = await auth.new_session(db, response, user)
    # Failed-login counters must commit before the generic error is raised.
    if error:
        raise HTTPException(429 if error == 'hub_login_limited' else 401, detail={'code': error, 'message': 'Unable to sign in'})
    return {'user': result}


@router.post('/token-session')
async def token_session(request: Request, response: Response, authorization: Annotated[str | None, Header()] = None):
    auth.csrf(request)
    _require_token(authorization)
    async with get_session_context() as db:
        result = await auth.new_session(db, response)
    return {'user': result}


@router.post('/logout')
async def logout(request: Request, response: Response):
    auth.csrf(request)
    token = request.cookies.get(auth.COOKIE)
    if token:
        async with get_session_context() as db:
            await db.execute(delete(HubSession).where(HubSession.token_hash == auth.fingerprint(token)))
    response.delete_cookie(auth.COOKIE, path='/', secure=True, httponly=True, samesite='strict')
    response.headers['Cache-Control'] = 'no-store'
    return {'ok': True}


@router.get('/users', dependencies=[Depends(operator_access)])
async def users(request: Request):
    admin(request)
    async with get_session_context() as db:
        return [auth.public_user(u) for u in (await db.scalars(select(HubUser).order_by(HubUser.username))).all()]


@router.post('/users', dependencies=[Depends(operator_access)])
async def create_user(body: Account, request: Request):
    admin(request)
    if not body.display_name.strip():
        raise HTTPException(422, detail={'code': 'hub_account_invalid', 'message': 'Check the account fields'})
    async with get_session_context() as db:
        await db.execute(text('SELECT pg_advisory_xact_lock(691432120)'))
        username = body.username.lower()
        if await db.scalar(select(HubUser.id).where(HubUser.username == username)):
            raise HTTPException(409, detail={'code': 'hub_username_exists', 'message': 'Username already exists'})
        user = HubUser(username=username, display_name=body.display_name.strip(), role=body.role,
            password_hash=await asyncio.to_thread(auth.password_hash, body.password.get_secret_value()))
        db.add(user)
        await db.flush()
        return auth.public_user(user)


class AccountStatus(BaseModel):
    active: bool


@router.put('/users/{user_id}', dependencies=[Depends(operator_access)])
async def update_user(user_id: int, body: AccountStatus, request: Request):
    admin(request)
    if (getattr(request.state, 'hub_user', None) or {}).get('id') == user_id and not body.active:
        raise HTTPException(409, detail={'code': 'hub_self_disable', 'message': 'Cannot disable your own active account'})
    async with get_session_context() as db:
        user = await db.get(HubUser, user_id, with_for_update=True)
        if not user:
            raise HTTPException(404)
        user.active = body.active
        if not body.active:
            await db.execute(delete(HubSession).where(HubSession.user_id == user_id))
        return auth.public_user(user)
