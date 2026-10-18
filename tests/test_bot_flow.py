"""Сквозная проверка диалогов бота: фейковый Telegram + фейковый OLX."""
import datetime as dt
import re
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.config import Config
from app.db import Database
from app.handlers import AccessMiddleware, router
from app.keyboards import BTN_ACCOUNTS, BTN_ADD, AccCb, ThreadCb
from app.poller import Poller
from app.services import Hub
from tests.test_core import FakeOlx, msg, thread


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.next_id = 1000

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = type(method).__name__
        if name == "GetMe":
            return User(id=1, is_bot=True, first_name="Bot", username="test_olx_bot")
        if name in ("SendMessage", "EditMessageText"):
            self.next_id += 1
            chat_id = getattr(method, "chat_id", None) or 100
            return Message(message_id=self.next_id, date=dt.datetime.now(),
                           chat=Chat(id=chat_id, type="private"), text=method.text).as_(bot)
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):
        yield b""

    def texts(self, chat_id=None):
        return [c.text for c in self.calls if type(c).__name__ in ("SendMessage", "EditMessageText")
                and (chat_id is None or getattr(c, "chat_id", None) == chat_id)]


class FlowOlx(FakeOlx):
    async def exchange_code(self, code):
        assert code == "GOODCODE123"
        return {"access_token": "acc2", "refresh_token": "ref2", "expires_in": 86400}

    async def me(self, token):
        return {"id": 888, "name": "Второй магазин", "email": "two@shop.ua"}


@pytest.fixture
async def world(tmp_path):
    cfg = Config(bot_token="1:x", owner_id=None, db_path=tmp_path / "b.db", poll_interval=10,
                 olx_domain="www.olx.ua", olx_client_id="cid", olx_client_secret="sec",
                 olx_redirect_uri="https://example.com/")
    db = Database(cfg.db_path)
    await db.connect()
    session = FakeSession()
    bot = Bot("123:abc", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    hub = Hub(bot, db, cfg)
    hub.olx = FlowOlx()
    poller = Poller(hub, 10)
    dp = Dispatcher()
    if not router.message.outer_middleware._middlewares:
        router.message.outer_middleware(AccessMiddleware())
        router.callback_query.outer_middleware(AccessMiddleware())
    if router.parent_router is None:
        dp.include_router(router)
    else:
        router._parent_router = None
        dp.include_router(router)
    counter = iter(range(1, 10_000))

    async def send(user_id, text, reply_to=None):
        chat = Chat(id=user_id, type="private")
        m = Message(message_id=next(counter), date=dt.datetime.now(), chat=chat,
                    from_user=User(id=user_id, is_bot=False, first_name=f"U{user_id}"), text=text,
                    reply_to_message=Message(message_id=reply_to, date=dt.datetime.now(), chat=chat,
                                             text="x") if reply_to else None)
        await dp.feed_update(bot, Update(update_id=next(counter), message=m), hub=hub, poller=poller)

    async def press(user_id, data):
        chat = Chat(id=user_id, type="private")
        cb = CallbackQuery(id=str(next(counter)), chat_instance="1", data=data,
                           from_user=User(id=user_id, is_bot=False, first_name=f"U{user_id}"),
                           message=Message(message_id=5, date=dt.datetime.now(), chat=chat, text="x"))
        await dp.feed_update(bot, Update(update_id=next(counter), callback_query=cb), hub=hub, poller=poller)

    yield SimpleNamespace(db=db, s=session, hub=hub, olx=hub.olx, poller=poller, send=send, press=press)
    await db.close()


async def test_full_customer_journey(world):
    w = world
    # 1. Первый /start — владелец.
    await w.send(100, "/start")
    assert (await w.db.get_member(100))["role"] == "owner"
    assert any("Подключите первый аккаунт" in t for t in w.s.texts(100))

    # Посторонний не получает доступ.
    await w.send(300, "/start")
    await w.send(300, BTN_ACCOUNTS)
    assert await w.db.get_member(300) is None
    assert all("закрытый бот" in t for t in w.s.texts(300))

    # 2. Подключение аккаунта через вставку ссылки.
    await w.send(100, BTN_ADD)
    login = w.s.texts(100)[-1]
    state = re.search(r"state=([\w-]+)", login).group(1)
    await w.send(100, "просто текст")
    assert "Не нашёл код" in w.s.texts(100)[-1]
    await w.send(100, f"https://example.com/?code=GOODCODE123&state={state}")
    assert "Аккаунт подключён" in w.s.texts(100)[-1] and "Второй магазин" in w.s.texts(100)[-1]
    acc = (await w.db.list_accounts())[0]

    # 3. Приглашение сотрудника.
    code = await w.hub.invite_code()
    await w.send(200, f"/start join_{code}")
    assert (await w.db.get_member(200))["role"] == "manager"

    # 4. Входящее сообщение приходит обоим.
    w.olx.threads_data = [thread("T1", 1, 1)]
    w.olx.msgs["T1"] = [msg(1, text="Добрый день, продаёте?")]
    w.s.calls.clear()
    await w.poller.poll_account(acc["id"])
    incoming = [c for c in w.s.calls if type(c).__name__ == "SendMessage"]
    assert {c.chat_id for c in incoming} == {100, 200}
    assert "Добрый день" in incoming[0].text and "Второй магазин" in incoming[0].text

    # 5. Ответ свайпом.
    owner_msg_id = w.s.next_id - 1  # сообщения шли владельцу, затем сотруднику
    link = await w.db.get_link(100, owner_msg_id)
    assert link, "входящее сообщение должно быть привязано к чату"
    await w.send(100, "Да, продаю", reply_to=owner_msg_id)
    assert w.olx.outbox[-1] == (acc["id"], "T1", "Да, продаю")
    assert any("Отправлено в OLX" in t for t in w.s.texts(100))
    assert any("Да, продаю" in t for t in w.s.texts(200))  # сотрудник видит ответ

    # 6. Ответ через кнопку, а кнопка меню посреди шага не уходит в OLX.
    await w.press(200, ThreadCb(action="reply", acc=acc["id"], tid="T1").pack())
    await w.send(200, BTN_ACCOUNTS)
    assert len(w.olx.outbox) == 1
    await w.press(200, ThreadCb(action="reply", acc=acc["id"], tid="T1").pack())
    await w.send(200, "Могу отправить Новой почтой")
    assert w.olx.outbox[-1][2] == "Могу отправить Новой почтой"

    # 7. Обычный текст без ответа — подсказка, в OLX ничего не уходит.
    await w.send(100, "привет")
    assert len(w.olx.outbox) == 2 and "смахните" in w.s.texts(100)[-1]

    # 8. Переименование и удаление (удалять может только владелец).
    await w.press(100, AccCb(action="rename", acc=acc["id"]).pack())
    await w.send(100, "Запчасти Киев")
    assert (await w.db.get_account(acc["id"]))["label"] == "Запчасти Киев"
    await w.press(200, AccCb(action="del_yes", acc=acc["id"]).pack())
    assert await w.db.get_account(acc["id"])
    alerts = [c for c in w.s.calls if isinstance(c, AnswerCallbackQuery) and c.show_alert]
    assert alerts
    await w.press(100, AccCb(action="del_yes", acc=acc["id"]).pack())
    assert await w.db.get_account(acc["id"]) is None
