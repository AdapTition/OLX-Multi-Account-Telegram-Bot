import logging
import secrets
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject

from .keyboards import (BTN_ACCOUNTS, BTN_ADD, BTN_HELP, BTN_SETTINGS, BTN_UNREAD, MENU_BUTTONS, AccCb, MemberCb,
                        SetCb, ThreadCb, account_kb, accounts_kb, cancel_kb, confirm_delete_kb,
                        login_kb, main_menu, settings_kb, team_kb, thread_kb)
from .olx import OlxAuthError, OlxError, parse_auth_reply
from .poller import Poller
from .services import Hub, esc

log = logging.getLogger(__name__)
router = Router()

# Текст, который не является кнопкой меню (кнопки меню работают из любого шага).
TEXT = F.text & ~F.text.in_(MENU_BUTTONS) & ~F.text.startswith("/")

STATUS = {"active": "🟢 работает", "paused": "⏸ на паузе", "auth_error": "⚠️ нужно переподключить"}

HELP = (
    "<b>Как пользоваться</b>\n\n"
    "📥 <b>Входящие.</b> Каждое новое сообщение с OLX приходит сюда. Вверху — цветная метка и "
    "название аккаунта, ниже — объявление и имя покупателя.\n\n"
    "↩️ <b>Ответить.</b> Два способа:\n"
    "• смахните сообщение влево (или «Ответить» в меню сообщения) и напишите текст;\n"
    "• или нажмите кнопку «↩️ Ответить» и напишите текст.\n"
    "Ответ уйдёт в OLX от того же аккаунта, в тот же чат. Бот подтвердит «✅ Отправлено».\n\n"
    "📜 <b>Переписка</b> — показать последние сообщения этого чата.\n"
    "📬 <b>Непрочитанные</b> — все чаты, где есть непрочитанные сообщения.\n"
    "➕ <b>Добавить аккаунт</b> — подключить ещё один аккаунт OLX (количество не ограничено).\n"
    "📋 <b>Мои аккаунты</b> — переименовать, поставить на паузу, переподключить или удалить.\n\n"
    "ℹ️ Отправлять в OLX можно только текст."
)


class Setup(StatesGroup):
    client_id = State()
    client_secret = State()


class AddAcc(StatesGroup):
    waiting = State()


class Rename(StatesGroup):
    waiting = State()


class ReplyTo(StatesGroup):
    waiting = State()


class AccessMiddleware(BaseMiddleware):
    """Пропускает только владельца и приглашённых сотрудников."""

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return None
        hub: Hub = data["hub"]
        member = await hub.db.get_member(user.id)
        data["member"] = member
        if member is None:
            is_start = isinstance(event, Message) and (event.text or "").startswith("/start")
            if not is_start:
                if isinstance(event, CallbackQuery):
                    await event.answer("Нет доступа", show_alert=True)
                else:
                    await event.answer("⛔ Это закрытый бот. Попросите владельца прислать ссылку-приглашение.")
                return None
        return await handler(event, data)


def is_owner(member: dict | None) -> bool:
    return bool(member and member["role"] == "owner")


# ======================= старт =======================

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, hub: Hub,
                    member: dict | None) -> None:
    await state.clear()
    user = message.from_user
    if member is None:
        members = await hub.db.list_members()
        has_owner = any(m["role"] == "owner" for m in members)
        arg = command.args or ""
        invite = await hub.db.get_setting("invite_code")
        if arg.startswith("join_") and invite and secrets.compare_digest(arg[5:], invite):
            await hub.db.add_member(user.id, "manager", user.full_name)
            await hub.notify(f"👥 К боту подключился сотрудник: <b>{esc(user.full_name)}</b>",
                             exclude=user.id)
        elif not has_owner and hub.cfg.owner_id in (None, user.id):
            await hub.db.add_member(user.id, "owner", user.full_name)
        else:
            await message.answer("⛔ Это закрытый бот. Попросите владельца прислать ссылку-приглашение.")
            return
        member = await hub.db.get_member(user.id)

    owner = is_owner(member)
    await message.answer(
        f"👋 Здравствуйте, {esc(user.first_name)}!\n\n"
        "Сюда приходят сообщения со всех ваших аккаунтов OLX, и отсюда же можно отвечать покупателям.",
        reply_markup=main_menu(owner))
    if not await hub.has_credentials():
        if owner:
            await ask_keys(message, state, hub)
        else:
            await message.answer("⏳ Владелец ещё не закончил настройку бота.")
    elif not await hub.db.list_accounts():
        await message.answer(f"Подключите первый аккаунт OLX кнопкой «{BTN_ADD}».")


# ======================= ключи OLX API =======================

async def ask_keys(message: Message, state: FSMContext, hub: Hub) -> None:
    await state.set_state(Setup.client_id)
    await message.answer(
        "🔑 <b>Разовая настройка: ключи OLX API</b>\n\n"
        "OLX разрешает сторонним программам работать с сообщениями только через официальный API. "
        "Ключи получают один раз, на все аккаунты сразу:\n\n"
        f"1. Откройте <a href=\"https://developer.{hub.cfg.olx_domain.removeprefix('www.')}\">портал разработчиков OLX</a> "
        "и войдите любым своим аккаунтом OLX.\n"
        "2. Нажмите «Подключить приложение» и заполните форму.\n"
        f"   В поле адреса перенаправления (Redirect URI) укажите: <code>{esc(hub.cfg.olx_redirect_uri)}</code>\n"
        "3. Дождитесь письма от OLX о подтверждении. В кабинете появятся <b>Client ID</b> и <b>Client Secret</b>.\n\n"
        "👉 Пришлите сюда <b>Client ID</b>:",
        reply_markup=cancel_kb(), disable_web_page_preview=True)


@router.message(Setup.client_id, TEXT)
async def got_client_id(message: Message, state: FSMContext) -> None:
    await state.update_data(client_id=message.text.strip())
    await state.set_state(Setup.client_secret)
    await message.answer("Теперь пришлите <b>Client Secret</b>:", reply_markup=cancel_kb())


@router.message(Setup.client_secret, TEXT)
async def got_client_secret(message: Message, state: FSMContext, hub: Hub) -> None:
    secret = message.text.strip()
    client_id = (await state.get_data())["client_id"]
    try:
        await message.delete()  # не храним секрет в переписке
    except TelegramBadRequest:
        pass
    wait = await message.answer("⏳ Проверяю ключи в OLX…")
    try:
        await hub.olx.check_app_credentials(client_id, secret)
    except OlxAuthError:
        await wait.edit_text("❌ OLX не принял эти ключи. Проверьте, что приложение уже подтверждено, "
                             "и пришлите <b>Client ID</b> ещё раз:", reply_markup=cancel_kb())
        await state.set_state(Setup.client_id)
        return
    except (OlxError, OSError) as e:
        log.warning("Не удалось проверить ключи: %s", e)
    await hub.db.set_setting("olx_client_id", client_id)
    await hub.db.set_setting("olx_client_secret", secret)
    await state.clear()
    await wait.edit_text(f"✅ Ключи сохранены. Теперь подключите аккаунт OLX кнопкой «{BTN_ADD}».")


# ======================= меню =======================

@router.message(F.text == BTN_HELP)
@router.message(Command("help"))
async def menu_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP)


@router.message(F.text == BTN_ADD)
async def menu_add(message: Message, state: FSMContext, hub: Hub, member: dict) -> None:
    await state.clear()
    await start_login(message, state, hub, member, reauth_id=None)


@router.message(F.text == BTN_ACCOUNTS)
async def menu_accounts(message: Message, state: FSMContext, hub: Hub) -> None:
    await state.clear()
    text, kb = await accounts_view(hub)
    await message.answer(text, reply_markup=kb)


@router.message(F.text == BTN_UNREAD)
async def menu_unread(message: Message, state: FSMContext, hub: Hub) -> None:
    await state.clear()
    rows = await hub.db.unread_threads(limit=50)
    if not rows:
        await message.answer("🎉 Непрочитанных чатов нет.")
        return
    shown = rows[:10]
    for t in shown:
        acc = await hub.db.get_account(t["account_id"])
        head = await hub.header(acc, t)
        sent = await message.answer(f"{head}\n\n📩 Непрочитанных: <b>{t['unread_count']}</b>",
                                    reply_markup=thread_kb(acc["id"], t["thread_id"]))
        await hub.db.add_link(sent.chat.id, sent.message_id, acc["id"], t["thread_id"])
    if len(rows) > len(shown):
        await message.answer(f"…и ещё {len(rows) - len(shown)}. Ответьте на эти — список обновится.")


@router.message(F.text == BTN_SETTINGS)
async def menu_settings(message: Message, state: FSMContext, hub: Hub, member: dict) -> None:
    await state.clear()
    if not is_owner(member):
        await message.answer("Настройки доступны только владельцу.")
        return
    client_id, _, redirect = await hub.credentials()
    src = "из файла .env" if hub.cfg.olx_client_id else "сохранены в боте"
    keys = f"<code>{esc(client_id[:6])}…</code> ({src})" if client_id else "❌ не заданы"
    members = await hub.db.list_members()
    await message.answer(
        "⚙️ <b>Настройки</b>\n\n"
        f"🔑 Ключи OLX API: {keys}\n"
        f"↪️ Redirect URI: <code>{esc(redirect)}</code>\n"
        f"👥 Людей с доступом к боту: {len(members)}\n"
        f"⏱ Проверка новых сообщений: каждые {hub.cfg.poll_interval} с",
        reply_markup=settings_kb())


# ======================= подключение аккаунта =======================

async def start_login(message: Message, state: FSMContext, hub: Hub, member: dict,
                      reauth_id: int | None) -> None:
    if not await hub.has_credentials():
        if is_owner(member):
            await ask_keys(message, state, hub)
        else:
            await message.answer("Владелец ещё не ввёл ключи OLX API в настройках.")
        return
    auth_state = secrets.token_urlsafe(8)
    client_id, _, redirect = await hub.credentials()
    url = hub.client.authorize_url(client_id, redirect, auth_state)
    await state.set_state(AddAcc.waiting)
    await state.update_data(auth_state=auth_state, reauth_id=reauth_id)
    await message.answer(
        "🔐 <b>Подключение аккаунта OLX</b>\n\n"
        "1. Скопируйте ссылку ниже и откройте её в браузере <b>в режиме инкогнито</b> "
        "(на компьютере Ctrl+Shift+N). Так вы точно войдёте в нужный аккаунт, "
        "а не в тот, что уже открыт в браузере.\n"
        "2. Войдите в аккаунт OLX, который хотите подключить, и нажмите «Разрешить».\n"
        f"3. Откроется страница <b>{esc(redirect)}</b> — так и должно быть. "
        "Скопируйте <b>весь адрес</b> из адресной строки браузера и пришлите его сюда.\n\n"
        f"Ссылка для входа (нажмите, чтобы скопировать):\n<code>{esc(url)}</code>",
        reply_markup=login_kb(url), disable_web_page_preview=True)


@router.message(AddAcc.waiting, TEXT)
async def got_auth_reply(message: Message, state: FSMContext, hub: Hub, poller: Poller) -> None:
    code, got_state, error = parse_auth_reply(message.text)
    data = await state.get_data()
    if error:
        await message.answer("Похоже, в OLX нажали «Отклонить». Откройте ссылку ещё раз и нажмите «Разрешить».")
        return
    if not code:
        await message.answer("Не нашёл код в сообщении. Пришлите <b>полный адрес</b> страницы, "
                             "на которую вас перекинуло после входа (в нём есть <code>code=</code>).")
        return
    if got_state and got_state != data.get("auth_state"):
        await message.answer("Эта ссылка от прошлой попытки. Откройте ссылку для входа из последнего "
                             "сообщения бота и пришлите новый адрес.")
        return

    wait = await message.answer("⏳ Подключаю аккаунт…")
    try:
        token = await hub.olx.exchange_code(code)
        me = await hub.olx.me(token["access_token"])
    except OlxAuthError:
        await wait.edit_text("❌ OLX не принял код — он одноразовый и живёт недолго. "
                             "Откройте ссылку для входа ещё раз и пришлите новый адрес.")
        return
    except (OlxError, OSError) as e:
        log.warning("Ошибка подключения: %s", e)
        await wait.edit_text("❌ OLX сейчас не отвечает. Попробуйте ещё раз через минуту.")
        return

    olx_uid = str(me["id"])
    label = me.get("name") or me.get("email") or f"OLX {olx_uid}"
    acc_id, created = await hub.db.upsert_account(olx_uid, label, me.get("email"), token)
    await state.clear()
    try:
        await poller.poll_account(acc_id)
    except (OlxError, OSError) as e:
        log.warning("Первичная синхронизация не удалась: %s", e)
    acc = await hub.db.get_account(acc_id)
    total, unread = await hub.db.count_threads(acc_id)

    title = "✅ Аккаунт подключён" if created else "🔄 Аккаунт переподключён"
    text = (f"{title}: {acc['emoji']} <b>{esc(acc['label'])}</b>"
            + (f" ({esc(acc['email'])})" if acc["email"] else "")
            + f"\n\nЧатов: {total}, с непрочитанными: {unread}.\n"
              "Все новые сообщения будут приходить сюда.")
    if unread:
        text += f"\nНепрочитанные старые чаты — кнопка «{BTN_UNREAD}»."
    reauth_id = data.get("reauth_id")
    if reauth_id and reauth_id != acc_id:
        text += ("\n\n⚠️ Вы вошли не в тот аккаунт, который переподключали. "
                 "Если это ошибка — повторите в режиме инкогнито.")
    await wait.edit_text(text)
    if created:
        await hub.notify(f"➕ Подключён новый аккаунт OLX: {acc['emoji']} <b>{esc(acc['label'])}</b>",
                         exclude=message.chat.id)


# ======================= аккаунты =======================

async def accounts_view(hub: Hub):
    accounts = await hub.db.list_accounts()
    if not accounts:
        return f"Аккаунтов пока нет. Нажмите «{BTN_ADD}».", None
    lines = [f"{a['emoji']} <b>{esc(a['label'])}</b> — {STATUS.get(a['status'], a['status'])}"
             for a in accounts]
    return f"📋 <b>Аккаунты OLX ({len(accounts)})</b>\n\n" + "\n".join(lines) + \
        "\n\nНажмите на аккаунт, чтобы управлять им.", accounts_kb(accounts)


async def account_view(hub: Hub, acc: dict):
    total, unread = await hub.db.count_threads(acc["id"])
    text = (f"{acc['emoji']} <b>{esc(acc['label'])}</b>\n\n"
            f"Статус: {STATUS.get(acc['status'], acc['status'])}\n"
            f"Email: {esc(acc['email'] or '—')}\n"
            f"ID в OLX: <code>{esc(acc['olx_user_id'])}</code>\n"
            f"Чатов: {total}, с непрочитанными: {unread}")
    return text, account_kb(acc)


async def safe_edit(cb: CallbackQuery, text: str, kb=None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=kb)


@router.callback_query(AccCb.filter())
async def cb_account(cb: CallbackQuery, callback_data: AccCb, state: FSMContext, hub: Hub,
                     member: dict) -> None:
    action = callback_data.action
    if action == "list":
        text, kb = await accounts_view(hub)
        await safe_edit(cb, text, kb)
        await cb.answer()
        return
    acc = await hub.db.get_account(callback_data.acc)
    if acc is None:
        await cb.answer("Аккаунт уже удалён", show_alert=True)
        return

    if action == "rename":
        await state.set_state(Rename.waiting)
        await state.update_data(acc_id=acc["id"])
        await cb.message.answer(f"Пришлите новое название для {acc['emoji']} <b>{esc(acc['label'])}</b> "
                                "(например: «Автозапчасти Киев»):", reply_markup=cancel_kb())
    elif action in ("pause", "resume"):
        await hub.db.set_account_field(acc["id"], "status", "paused" if action == "pause" else "active")
        text, kb = await account_view(hub, await hub.db.get_account(acc["id"]))
        await safe_edit(cb, text, kb)
    elif action == "reauth":
        await start_login(cb.message, state, hub, member, reauth_id=acc["id"])
    elif action == "del":
        if not is_owner(member):
            await cb.answer("Удалять аккаунты может только владелец", show_alert=True)
            return
        await safe_edit(cb, f"Удалить {acc['emoji']} <b>{esc(acc['label'])}</b> из бота?\n"
                            "Сообщения с него перестанут приходить. На самом OLX ничего не изменится.",
                        confirm_delete_kb(acc["id"]))
    elif action == "del_yes":
        if not is_owner(member):
            await cb.answer("Удалять аккаунты может только владелец", show_alert=True)
            return
        await hub.db.delete_account(acc["id"])
        text, kb = await accounts_view(hub)
        await safe_edit(cb, f"🗑 Аккаунт «{esc(acc['label'])}» удалён.\n\n{text}", kb)
    else:  # open
        text, kb = await account_view(hub, acc)
        await safe_edit(cb, text, kb)
    await cb.answer()


@router.message(Rename.waiting, TEXT)
async def got_new_name(message: Message, state: FSMContext, hub: Hub) -> None:
    name = message.text.strip()[:40]
    acc_id = (await state.get_data())["acc_id"]
    await hub.db.set_account_field(acc_id, "label", name)
    await state.clear()
    acc = await hub.db.get_account(acc_id)
    if acc:
        text, kb = await account_view(hub, acc)
        await message.answer("✅ Название изменено.\n\n" + text, reply_markup=kb)


# ======================= чаты: ответ, история, прочитано =======================

@router.callback_query(ThreadCb.filter())
async def cb_thread(cb: CallbackQuery, callback_data: ThreadCb, state: FSMContext, hub: Hub) -> None:
    acc = await hub.db.get_account(callback_data.acc)
    if acc is None:
        await cb.answer("Этот аккаунт удалён из бота", show_alert=True)
        return
    tid = callback_data.tid
    if callback_data.action == "reply":
        thread = await hub.db.get_thread(acc["id"], tid) or {}
        name = await hub.user_name(acc["id"], thread.get("interlocutor_id") or "")
        await state.set_state(ReplyTo.waiting)
        await state.update_data(acc_id=acc["id"], tid=tid)
        prompt = await cb.message.answer(
            f"✍️ Напишите ответ для <b>{esc(name)}</b> от {acc['emoji']} <b>{esc(acc['label'])}</b>:",
            reply_markup=cancel_kb())
        await hub.db.add_link(prompt.chat.id, prompt.message_id, acc["id"], tid)
        await cb.answer()
    elif callback_data.action == "hist":
        await cb.answer("Загружаю…")
        try:
            text = await hub.history(acc["id"], tid)
        except OlxError as e:
            await cb.message.answer(f"❌ Не удалось загрузить переписку ({e.status}).")
            return
        sent = await cb.message.answer(text, reply_markup=thread_kb(acc["id"], tid, with_history=False))
        await hub.db.add_link(sent.chat.id, sent.message_id, acc["id"], tid)
    elif callback_data.action == "read":
        try:
            await hub.olx.mark_read(acc["id"], tid)
            await hub.db.set_unread(acc["id"], tid, 0)
            await cb.answer("Отмечено прочитанным в OLX")
        except OlxError:
            await cb.answer("OLX не ответил, попробуйте позже", show_alert=True)


@router.message(ReplyTo.waiting, TEXT)
async def got_reply_text(message: Message, state: FSMContext, hub: Hub) -> None:
    data = await state.get_data()
    await state.clear()
    await hub.reply(data["acc_id"], data["tid"], message.text, message)


async def linked_reply(message: Message, hub: Hub) -> dict | bool:
    """Фильтр: сообщение — это ответ (свайп) на сообщение бота, привязанное к чату OLX."""
    if not message.reply_to_message:
        return False
    link = await hub.db.get_link(message.chat.id, message.reply_to_message.message_id)
    return {"link": link} if link else False


@router.message(F.text, linked_reply)
async def swipe_reply(message: Message, state: FSMContext, hub: Hub, link: dict) -> None:
    await state.clear()
    await hub.reply(link["account_id"], link["thread_id"], message.text, message)


# ======================= настройки владельца =======================

async def team_view(hub: Hub, bot: Bot, regenerate: bool = False):
    code = await hub.invite_code(regenerate)
    me = await bot.me()
    members = await hub.db.list_members()
    lines = [f"{'👑' if m['role'] == 'owner' else '👤'} {esc(m['name'] or m['tg_id'])}" for m in members]
    text = ("👥 <b>Сотрудники</b>\n\n" + "\n".join(lines) +
            "\n\nЧтобы дать доступ сотруднику, перешлите ему эту ссылку. Он будет получать все сообщения "
            "и сможет отвечать покупателям:\n"
            f"<code>https://t.me/{me.username}?start=join_{code}</code>")
    return text, team_kb(members)


@router.callback_query(SetCb.filter())
async def cb_settings(cb: CallbackQuery, callback_data: SetCb, state: FSMContext, hub: Hub,
                      bot: Bot, member: dict) -> None:
    if callback_data.action == "cancel":
        await state.clear()
        await safe_edit(cb, "Отменено.")
        await cb.answer()
        return
    if not is_owner(member):
        await cb.answer("Только для владельца", show_alert=True)
        return
    if callback_data.action == "keys":
        await ask_keys(cb.message, state, hub)
    elif callback_data.action in ("team", "newinvite"):
        text, kb = await team_view(hub, bot, regenerate=callback_data.action == "newinvite")
        await safe_edit(cb, text, kb)
    await cb.answer()


@router.callback_query(MemberCb.filter())
async def cb_member(cb: CallbackQuery, callback_data: MemberCb, hub: Hub, bot: Bot,
                    member: dict) -> None:
    if not is_owner(member):
        await cb.answer("Только для владельца", show_alert=True)
        return
    await hub.db.remove_member(callback_data.uid)
    text, kb = await team_view(hub, bot)
    await safe_edit(cb, text, kb)
    await cb.answer("Доступ отозван")


# ======================= всё остальное =======================

@router.message(ReplyTo.waiting)
@router.message(F.reply_to_message, linked_reply)
async def non_text_reply(message: Message) -> None:
    await message.answer("В OLX можно отправить только текст. Напишите сообщение словами.")


@router.message()
async def fallback(message: Message, member: dict) -> None:
    await message.answer(
        "Чтобы ответить покупателю — смахните его сообщение влево (ответ) "
        "или нажмите под ним «↩️ Ответить».",
        reply_markup=main_menu(is_owner(member)))
