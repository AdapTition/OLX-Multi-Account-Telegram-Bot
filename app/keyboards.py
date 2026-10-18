from aiogram.filters.callback_data import CallbackData
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardMarkup)
from aiogram.utils.keyboard import InlineKeyboardBuilder

BTN_ADD = "➕ Добавить аккаунт OLX"
BTN_ACCOUNTS = "📋 Мои аккаунты"
BTN_UNREAD = "📬 Непрочитанные"
BTN_HELP = "❓ Помощь"
BTN_SETTINGS = "⚙️ Настройки"
MENU_BUTTONS = {BTN_ADD, BTN_ACCOUNTS, BTN_UNREAD, BTN_HELP, BTN_SETTINGS}


class ThreadCb(CallbackData, prefix="t"):
    action: str  # reply | hist | read
    acc: int
    tid: str


class AccCb(CallbackData, prefix="a"):
    action: str  # open | rename | pause | resume | reauth | del | del_yes | list
    acc: int


class MemberCb(CallbackData, prefix="m"):
    action: str  # del
    uid: int


class SetCb(CallbackData, prefix="s"):
    action: str  # keys | team | newinvite | cancel


def main_menu(is_owner: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_ADD)],
        [KeyboardButton(text=BTN_ACCOUNTS), KeyboardButton(text=BTN_UNREAD)],
        [KeyboardButton(text=BTN_HELP)] + ([KeyboardButton(text=BTN_SETTINGS)] if is_owner else []),
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def thread_kb(acc: int, tid: str, with_history: bool = True) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="↩️ Ответить", callback_data=ThreadCb(action="reply", acc=acc, tid=tid))
    if with_history:
        b.button(text="📜 Переписка", callback_data=ThreadCb(action="hist", acc=acc, tid=tid))
    b.button(text="✔️ Прочитано", callback_data=ThreadCb(action="read", acc=acc, tid=tid))
    b.adjust(2, 1) if with_history else b.adjust(2)
    return b.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✖️ Отмена", callback_data=SetCb(action="cancel").pack())]])


def login_kb(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Войти в OLX", url=url)],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data=SetCb(action="cancel").pack())],
    ])


def accounts_kb(accounts: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for a in accounts:
        mark = {"active": "", "paused": " ⏸", "auth_error": " ⚠️"}.get(a["status"], "")
        b.button(text=f"{a['emoji']} {a['label']}{mark}", callback_data=AccCb(action="open", acc=a["id"]))
    b.adjust(1)
    return b.as_markup()


def account_kb(acc: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Переименовать", callback_data=AccCb(action="rename", acc=acc["id"]))
    if acc["status"] == "paused":
        b.button(text="▶️ Включить", callback_data=AccCb(action="resume", acc=acc["id"]))
    else:
        b.button(text="⏸ Пауза", callback_data=AccCb(action="pause", acc=acc["id"]))
    b.button(text="🔑 Переподключить", callback_data=AccCb(action="reauth", acc=acc["id"]))
    b.button(text="🗑 Удалить", callback_data=AccCb(action="del", acc=acc["id"]))
    b.button(text="⬅️ К списку", callback_data=AccCb(action="list", acc=0))
    b.adjust(2, 2, 1)
    return b.as_markup()


def confirm_delete_kb(acc_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Да, удалить", callback_data=AccCb(action="del_yes", acc=acc_id))
    b.button(text="Отмена", callback_data=AccCb(action="open", acc=acc_id))
    return b.as_markup()


def reauth_kb(acc_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🔑 Переподключить", callback_data=AccCb(action="reauth", acc=acc_id).pack())]])


def settings_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔑 Ключи OLX API", callback_data=SetCb(action="keys"))
    b.button(text="👥 Сотрудники", callback_data=SetCb(action="team"))
    b.adjust(1)
    return b.as_markup()


def team_kb(members: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for m in members:
        if m["role"] != "owner":
            b.button(text=f"❌ Убрать {m['name'] or m['tg_id']}", callback_data=MemberCb(action="del", uid=m["tg_id"]))
    b.button(text="🔄 Новая ссылка-приглашение", callback_data=SetCb(action="newinvite"))
    b.adjust(1)
    return b.as_markup()
