"""Клиент официального OLX Partner API v2 (https://developer.olx.ua/api/doc)."""
import asyncio
import logging
import time
from collections import defaultdict
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from .db import Database

log = logging.getLogger(__name__)

SCOPE = "v2 read write"


class OlxError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"OLX {status}: {message}")
        self.status = status
        self.message = message


class OlxAuthError(OlxError):
    """Токен или ключи приложения недействительны — аккаунт нужно переподключить."""


def parse_auth_reply(text: str) -> tuple[str | None, str | None, str | None]:
    """Достаёт (code, state, error) из ссылки, на которую OLX перенаправил после входа.

    Принимает как полную ссылку, так и просто скопированный код.
    """
    text = (text or "").strip()
    if not text:
        return None, None, None
    if "code=" in text or "error=" in text:
        query = urlparse(text).query or text.split("?", 1)[-1]
        qs = parse_qs(query)
        code = (qs.get("code") or [None])[0]
        return code, (qs.get("state") or [None])[0], (qs.get("error") or [None])[0]
    if " " not in text and "/" not in text and len(text) >= 10:
        return text, None, None
    return None, None, None


class OlxClient:
    def __init__(self, domain: str):
        self.domain = domain
        self._session: aiohttp.ClientSession | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def authorize_url(self, client_id: str, redirect_uri: str, state: str) -> str:
        params = {"client_id": client_id, "response_type": "code", "state": state,
                  "scope": SCOPE, "redirect_uri": redirect_uri}
        return f"https://{self.domain}/oauth/authorize/?{urlencode(params)}"

    async def token(self, payload: dict) -> dict:
        s = await self.session()
        async with s.post(f"https://{self.domain}/api/open/oauth/token", json=payload) as r:
            body = await r.text()
            if r.status in (400, 401, 403):
                raise OlxAuthError(r.status, body[:300])
            if r.status >= 400:
                raise OlxError(r.status, body[:300])
            data = await r.json(content_type=None)
        if "access_token" not in data:
            raise OlxAuthError(r.status, str(data)[:300])
        return data

    async def api(self, method: str, path: str, access_token: str, *,
                  params: dict | None = None, json: dict | None = None):
        s = await self.session()
        headers = {"Authorization": f"Bearer {access_token}", "Version": "2.0",
                   "Accept": "application/json"}
        url = f"https://{self.domain}/api/partner{path}"
        async with s.request(method, url, headers=headers, params=params, json=json) as r:
            if r.status == 401:
                raise OlxAuthError(401, (await r.text())[:300])
            if r.status >= 400:
                raise OlxError(r.status, (await r.text())[:300])
            if r.status == 204:
                return None
            data = await r.json(content_type=None)
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data


Creds = Callable[[], Awaitable[tuple[str, str, str]]]


class OlxAccounts:
    """Запросы от имени конкретного подключённого аккаунта с автообновлением токена."""

    def __init__(self, db: Database, client: OlxClient, creds: Creds):
        self.db = db
        self.client = client
        self.creds = creds  # -> (client_id, client_secret, redirect_uri)
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ---------- подключение ----------
    async def exchange_code(self, code: str) -> dict:
        client_id, secret, redirect_uri = await self.creds()
        return await self.client.token({
            "grant_type": "authorization_code", "client_id": client_id,
            "client_secret": secret, "code": code, "scope": SCOPE, "redirect_uri": redirect_uri,
        })

    async def check_app_credentials(self, client_id: str, secret: str) -> None:
        await self.client.token({"grant_type": "client_credentials", "client_id": client_id,
                                 "client_secret": secret, "scope": SCOPE})

    async def me(self, access_token: str) -> dict:
        return await self.client.api("GET", "/users/me", access_token)

    # ---------- токены ----------
    async def _refresh(self, account_id: int, failed_token: str | None = None) -> str:
        async with self._locks[account_id]:
            acc = await self.db.get_account(account_id)
            # Пока ждали блокировку, токен мог обновить другой запрос.
            if failed_token is None and acc["expires_at"] - 300 > time.time():
                return acc["access_token"]
            if failed_token is not None and acc["access_token"] != failed_token:
                return acc["access_token"]
            if not acc["refresh_token"]:
                raise OlxAuthError(401, "нет refresh_token")
            client_id, secret, _ = await self.creds()
            try:
                token = await self.client.token({
                    "grant_type": "refresh_token", "client_id": client_id,
                    "client_secret": secret, "refresh_token": acc["refresh_token"],
                })
            except OlxAuthError:
                await self.db.set_account_field(account_id, "status", "auth_error")
                raise
            await self.db.save_tokens(account_id, token)
            log.info("Токен аккаунта %s обновлён", account_id)
            return token["access_token"]

    async def call(self, account_id: int, method: str, path: str, **kw):
        acc = await self.db.get_account(account_id)
        if acc is None:
            raise OlxError(404, "аккаунт удалён")
        token = acc["access_token"]
        if acc["expires_at"] - 300 < time.time():
            token = await self._refresh(account_id)
        try:
            return await self.client.api(method, path, token, **kw)
        except OlxAuthError:
            token = await self._refresh(account_id, failed_token=token)
            try:
                return await self.client.api(method, path, token, **kw)
            except OlxAuthError:
                await self.db.set_account_field(account_id, "status", "auth_error")
                raise

    # ---------- чаты ----------
    async def threads(self, account_id: int, page: int = 50, max_pages: int = 40) -> list[dict]:
        result: list[dict] = []
        for n in range(max_pages):
            chunk = await self.call(account_id, "GET", "/threads",
                                    params={"offset": n * page, "limit": page}) or []
            result.extend(chunk)
            if len(chunk) < page:
                break
        return result

    async def messages(self, account_id: int, thread_id: str) -> list[dict]:
        msgs = await self.call(account_id, "GET", f"/threads/{thread_id}/messages") or []
        return sorted(msgs, key=lambda m: (m.get("created_at") or "", m.get("id") or 0))

    async def send(self, account_id: int, thread_id: str, text: str) -> None:
        await self.call(account_id, "POST", f"/threads/{thread_id}/messages", json={"text": text})

    async def mark_read(self, account_id: int, thread_id: str) -> None:
        await self.call(account_id, "POST", f"/threads/{thread_id}/commands",
                        json={"command": "mark-as-read"})

    async def advert(self, account_id: int, advert_id: str) -> dict:
        return await self.call(account_id, "GET", f"/adverts/{advert_id}")

    async def user(self, account_id: int, user_id: str) -> dict:
        return await self.call(account_id, "GET", f"/users/{user_id}")


def thread_key(thread: dict) -> str:
    return str(thread.get("uuid") or thread["id"])


def message_key(msg: dict) -> str:
    return str(msg.get("uuid") or msg["id"])
