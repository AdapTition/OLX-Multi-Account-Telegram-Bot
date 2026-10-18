import time
from types import SimpleNamespace

import pytest

from app.config import Config
from app.db import Database
from app.olx import OlxAccounts, OlxAuthError, parse_auth_reply
from app.poller import Poller
from app.services import Hub



class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent), chat=SimpleNamespace(id=chat_id))


class FakeOlx:
    def __init__(self):
        self.threads_data = []
        self.msgs = {}
        self.outbox = []
        self.read = []

    async def threads(self, account_id):
        return self.threads_data

    async def messages(self, account_id, tid):
        return self.msgs.get(tid, [])

    async def advert(self, account_id, advert_id):
        return {"title": "iPhone 13", "url": "https://olx.ua/d/iphone"}

    async def user(self, account_id, uid):
        return {"name": "Иван"}

    async def send(self, account_id, tid, text):
        self.outbox.append((account_id, tid, text))

    async def mark_read(self, account_id, tid):
        self.read.append(tid)


def msg(i, kind="received", text=None):
    return {"id": i, "uuid": f"m{i}", "type": kind, "text": text or f"msg {i}",
            "created_at": f"2026-01-01T00:00:{i:02d}+00:00"}


@pytest.fixture
async def env(tmp_path):
    cfg = Config(bot_token="x", owner_id=None, db_path=tmp_path / "t.db", poll_interval=10,
                 olx_domain="www.olx.ua", olx_client_id="cid", olx_client_secret="sec",
                 olx_redirect_uri="https://example.com/")
    db = Database(cfg.db_path)
    await db.connect()
    await db.add_member(100, "owner", "Owner")
    await db.add_member(200, "manager", "Manager")
    bot = FakeBot()
    hub = Hub(bot, db, cfg)
    hub.olx = FakeOlx()
    acc_id, created = await db.upsert_account("555", "Магазин", "a@b.c",
                                              {"access_token": "t", "refresh_token": "r"})
    poller = Poller(hub, 10)
    yield SimpleNamespace(db=db, bot=bot, hub=hub, olx=hub.olx, poller=poller, acc=acc_id)
    await db.close()


def thread(tid, total, unread=0):
    return {"id": 1, "uuid": tid, "advert_id": 7, "interlocutor_id": 9,
            "total_count": total, "unread_count": unread}


async def test_first_sync_does_not_flood_old_messages(env):
    env.olx.threads_data = [thread("T1", 3, 1)]
    env.olx.msgs["T1"] = [msg(1), msg(2, "sent"), msg(3)]
    await env.poller.poll_account(env.acc)
    assert env.bot.sent == []
    assert (await env.db.count_threads(env.acc)) == (1, 1)


async def test_new_messages_are_forwarded_to_every_member_once(env):
    env.olx.threads_data = [thread("T1", 1)]
    env.olx.msgs["T1"] = [msg(1)]
    await env.poller.poll_account(env.acc)  # базовая синхронизация

    env.olx.threads_data = [thread("T1", 3, 2)]
    env.olx.msgs["T1"] = [msg(1), msg(2, text="Ещё актуально?"), msg(3, text="Торг есть?")]
    await env.poller.poll_account(env.acc)
    assert [c for c, _ in env.bot.sent] == [100, 200, 100, 200]
    text = env.bot.sent[0][1]
    assert "Магазин" in text and "iPhone 13" in text and "Иван" in text and "Ещё актуально?" in text

    await env.poller.poll_account(env.acc)  # без изменений — ничего не шлём повторно
    assert len(env.bot.sent) == 4


async def test_own_sent_messages_are_not_forwarded(env):
    env.olx.threads_data = [thread("T1", 1)]
    env.olx.msgs["T1"] = [msg(1)]
    await env.poller.poll_account(env.acc)
    env.olx.threads_data = [thread("T1", 2)]
    env.olx.msgs["T1"] = [msg(1), msg(2, "sent")]
    await env.poller.poll_account(env.acc)
    assert env.bot.sent == []


async def test_brand_new_thread_after_sync_is_forwarded(env):
    await env.poller.poll_account(env.acc)
    env.olx.threads_data = [thread("T2", 1, 1)]
    env.olx.msgs["T2"] = [msg(5, text="Здравствуйте")]
    await env.poller.poll_account(env.acc)
    assert len(env.bot.sent) == 2 and "Здравствуйте" in env.bot.sent[0][1]


async def test_reply_is_linked_and_sent_to_olx(env):
    env.olx.threads_data = [thread("T1", 1)]
    await env.poller.poll_account(env.acc)
    env.olx.threads_data = [thread("T1", 2, 1)]
    env.olx.msgs["T1"] = [msg(1), msg(2)]
    await env.poller.poll_account(env.acc)

    link = await env.db.get_link(100, 1)  # первое пересланное сообщение владельцу
    assert link["thread_id"] == "T1" and link["account_id"] == env.acc

    replies = []

    async def reply(text, **kw):
        replies.append(text)
        return SimpleNamespace(message_id=999, chat=SimpleNamespace(id=100))

    message = SimpleNamespace(reply=reply, answer=reply, chat=SimpleNamespace(id=100),
                              from_user=SimpleNamespace(full_name="Owner"))
    await env.hub.reply(link["account_id"], link["thread_id"], "Да, в наличии", message)
    assert env.olx.outbox == [(env.acc, "T1", "Да, в наличии")]
    assert env.olx.read == ["T1"]
    assert "Отправлено" in replies[0]
    assert "Да, в наличии" in env.bot.sent[-1][1] and env.bot.sent[-1][0] == 200  # коллега в курсе
    assert await env.db.get_link(100, 999)  # на подтверждение тоже можно ответить


async def test_duplicate_login_updates_existing_account(env):
    acc_id, created = await env.db.upsert_account("555", "Другое имя", None,
                                                  {"access_token": "new", "refresh_token": "r2"})
    assert acc_id == env.acc and not created
    assert len(await env.db.list_accounts()) == 1
    other, created = await env.db.upsert_account("777", "Второй", None, {"access_token": "z"})
    accs = await env.db.list_accounts()
    assert created and accs[0]["emoji"] != accs[1]["emoji"]


def test_parse_auth_reply():
    assert parse_auth_reply("https://example.com/?code=abc123def456&state=xyz") == ("abc123def456", "xyz", None)
    assert parse_auth_reply("example.com/?state=s&code=QWERTY12345") == ("QWERTY12345", "s", None)
    assert parse_auth_reply("https://example.com/?error=access_denied&state=s")[2] == "access_denied"
    assert parse_auth_reply("abcdef0123456789") == ("abcdef0123456789", None, None)
    assert parse_auth_reply("привет как дела") == (None, None, None)


class FakeClient:
    def __init__(self):
        self.refreshes = 0
        self.valid = "fresh"

    async def token(self, payload):
        self.refreshes += 1
        assert payload["grant_type"] == "refresh_token"
        return {"access_token": "fresh", "refresh_token": "r2", "expires_in": 86400}

    async def api(self, method, path, token, **kw):
        if token != self.valid:
            raise OlxAuthError(401, "expired")
        return [{"ok": True}]


async def test_expired_token_is_refreshed_transparently(env):
    client = FakeClient()

    async def creds():
        return "cid", "sec", "https://example.com/"

    api = OlxAccounts(env.db, client, creds)
    await env.db.save_tokens(env.acc, {"access_token": "old", "expires_in": 86400})
    assert await api.call(env.acc, "GET", "/threads") == [{"ok": True}]  # 401 -> refresh -> повтор
    assert client.refreshes == 1
    acc = await env.db.get_account(env.acc)
    assert acc["access_token"] == "fresh" and acc["refresh_token"] == "r2"

    await env.db.save_tokens(env.acc, {"access_token": "fresh", "expires_in": 10})  # скоро истечёт
    await api.call(env.acc, "GET", "/threads")
    assert client.refreshes == 2
    assert (await env.db.get_account(env.acc))["expires_at"] > time.time() + 3600


async def test_revoked_refresh_token_marks_account(env):
    class Revoked(FakeClient):
        async def token(self, payload):
            raise OlxAuthError(400, "invalid_grant")

    async def creds():
        return "cid", "sec", "u"

    api = OlxAccounts(env.db, Revoked(), creds)
    await env.db.save_tokens(env.acc, {"access_token": "old", "expires_in": 86400})
    with pytest.raises(OlxAuthError):
        await api.call(env.acc, "GET", "/threads")
    assert (await env.db.get_account(env.acc))["status"] == "auth_error"
