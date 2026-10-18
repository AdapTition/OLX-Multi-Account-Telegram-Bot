"""Общая логика: доставка сообщений OLX в Telegram и ответы обратно."""
import asyncio
import html
import logging
import secrets
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, Message

from .config import Config
from .db import Database
from .keyboards import thread_kb
from .olx import OlxAccounts, OlxClient, OlxError, message_key

log = logging.getLogger(__name__)

MAX_TEXT = 3500


def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def short_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%d.%m %H:%M")
    except ValueError:
        return iso


class Hub:
    def __init__(self, bot: Bot, db: Database, cfg: Config):
        self.bot = bot
        self.db = db
        self.cfg = cfg
        self.client = OlxClient(cfg.olx_domain)
        self.olx = OlxAccounts(db, self.client, self.credentials)

    # ---------- ключи приложения OLX ----------
    async def credentials(self) -> tuple[str, str, str]:
        client_id = self.cfg.olx_client_id or await self.db.get_setting("olx_client_id") or ""
        secret = self.cfg.olx_client_secret or await self.db.get_setting("olx_client_secret") or ""
        return client_id, secret, self.cfg.olx_redirect_uri

    async def has_credentials(self) -> bool:
        client_id, secret, _ = await self.credentials()
        return bool(client_id and secret)

    async def invite_code(self, regenerate: bool = False) -> str:
        code = await self.db.get_setting("invite_code")
        if regenerate or not code:
            code = secrets.token_urlsafe(12)
            await self.db.set_setting("invite_code", code)
        return code

    # ---------- рассылка команде ----------
    async def recipients(self) -> list[int]:
        return [m["tg_id"] for m in await self.db.list_members()]

    async def notify(self, text: str, kb: InlineKeyboardMarkup | None = None,
                     exclude: int | None = None) -> None:
        for chat_id in await self.recipients():
            if chat_id == exclude:
                continue
            try:
                await self.bot.send_message(chat_id, text, reply_markup=kb)
            except (TelegramForbiddenError, TelegramBadRequest) as e:
                log.warning("Не удалось отправить уведомление %s: %s", chat_id, e)

    # ---------- названия объявлений и имена ----------
    async def advert_info(self, account_id: int, advert_id: str) -> tuple[str, str | None]:
        if not advert_id:
            return "Без объявления", None
        title = await self.db.cache_get(account_id, "advert_title", advert_id)
        url = await self.db.cache_get(account_id, "advert_url", advert_id)
        if title is None:
            try:
                ad = await self.olx.advert(account_id, advert_id)
                title, url = ad.get("title") or f"Объявление #{advert_id}", ad.get("url")
            except OlxError:
                # Чужое объявление (аккаунт сам писал продавцу) — API его не отдаёт.
                title, url = f"Объявление #{advert_id}", None
            await self.db.cache_set(account_id, "advert_title", advert_id, title)
            await self.db.cache_set(account_id, "advert_url", advert_id, url or "")
        return title, url or None

    async def user_name(self, account_id: int, user_id: str) -> str:
        if not user_id:
            return "Покупатель"
        name = await self.db.cache_get(account_id, "user", user_id)
        if name is None:
            try:
                name = (await self.olx.user(account_id, user_id)).get("name") or "Покупатель"
            except OlxError:
                name = "Покупатель"
            await self.db.cache_set(account_id, "user", user_id, name)
        return name

    async def header(self, acc: dict, thread: dict) -> str:
        title, url = await self.advert_info(acc["id"], thread.get("advert_id") or "")
        name = await self.user_name(acc["id"], thread.get("interlocutor_id") or "")
        ad = f'<a href="{esc(url)}">{esc(title)}</a>' if url else esc(title)
        return (f"{acc['emoji']} <b>{esc(acc['label'])}</b>\n"
                f"📦 {ad}\n"
                f"👤 {esc(name)}")

    # ---------- входящие ----------
    async def deliver(self, acc: dict, thread: dict, thread_id: str, msgs: list[dict]) -> None:
        """Отправляет новые сообщения покупателя всем участникам команды."""
        head = await self.header(acc, thread)
        for msg in msgs:
            body = esc((msg.get("text") or "").strip()[:MAX_TEXT]) or "<i>(без текста)</i>"
            files = [a.get("url") for a in (msg.get("attachments") or []) + (msg.get("cvs") or [])
                     if a.get("url")]
            text = f"{head}\n\n{body}"
            if files:
                text += "\n\n" + "\n".join(f'📎 <a href="{esc(u)}">Вложение {i}</a>'
                                           for i, u in enumerate(files, 1))
            kb = thread_kb(acc["id"], thread_id)
            for chat_id in await self.recipients():
                try:
                    sent = await self.bot.send_message(chat_id, text, reply_markup=kb)
                    await self.db.add_link(chat_id, sent.message_id, acc["id"], thread_id)
                except (TelegramForbiddenError, TelegramBadRequest) as e:
                    log.warning("Не доставлено в %s: %s", chat_id, e)
            await self.db.mark_seen(acc["id"], message_key(msg))

    # ---------- ответ ----------
    async def reply(self, account_id: int, thread_id: str, text: str, message: Message) -> None:
        acc = await self.db.get_account(account_id)
        if acc is None:
            await message.answer("⚠️ Этот аккаунт OLX уже удалён из бота.")
            return
        text = text.strip()
        if not text:
            await message.answer("Пустое сообщение не отправлено.")
            return
        try:
            await self.olx.send(account_id, thread_id, text)
        except OlxError as e:
            log.warning("Ошибка отправки в OLX: %s", e)
            await message.reply(f"❌ OLX не принял сообщение ({e.status}). Попробуйте ещё раз через минуту.\n"
                                f"<code>{esc(e.message[:200])}</code>")
            return
        try:
            await self.olx.mark_read(account_id, thread_id)
            await self.db.set_unread(account_id, thread_id, 0)
        except OlxError:
            pass
        confirm = await message.reply(f"✅ Отправлено в OLX от {acc['emoji']} <b>{esc(acc['label'])}</b>")
        await self.db.add_link(confirm.chat.id, confirm.message_id, account_id, thread_id)
        thread = await self.db.get_thread(account_id, thread_id) or {}
        who = esc(message.from_user.full_name if message.from_user else "Сотрудник")
        name = await self.user_name(account_id, thread.get("interlocutor_id") or "")
        await self.notify(f"↩️ {who} ответил(а) {esc(name)} от {acc['emoji']} <b>{esc(acc['label'])}</b>:\n"
                          f"{esc(text[:1000])}", exclude=message.chat.id)

    # ---------- история ----------
    async def history(self, account_id: int, thread_id: str, limit: int = 12) -> str:
        acc = await self.db.get_account(account_id)
        thread = await self.db.get_thread(account_id, thread_id) or {}
        msgs, head = await asyncio.gather(self.olx.messages(account_id, thread_id),
                                          self.header(acc, thread))
        lines = []
        for m in msgs[-limit:]:
            who = "🙋 Вы" if m.get("type") == "sent" else "👤"
            txt = esc((m.get("text") or "").strip()[:600]) or "<i>(вложение)</i>"
            lines.append(f"<b>{who}</b> <i>{short_time(m.get('created_at'))}</i>\n{txt}")
        skipped = len(msgs) - len(lines)
        more = f"<i>…ещё {skipped} сообщений раньше</i>\n\n" if skipped > 0 else ""
        text = f"{head}\n\n{more}" + ("\n\n".join(lines) or "<i>Сообщений нет</i>")
        return text[:4000]
