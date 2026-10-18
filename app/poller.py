"""Фоновый опрос всех аккаунтов OLX и пересылка новых сообщений в Telegram."""
import asyncio
import logging
import time

from .keyboards import reauth_kb
from .olx import OlxAuthError, OlxError, message_key, thread_key
from .services import Hub, esc

log = logging.getLogger(__name__)

PARALLEL_ACCOUNTS = 5
ALERT_AFTER_FAILS = 30  # ~5 минут подряд ошибок при интервале 10 с


class Poller:
    def __init__(self, hub: Hub, interval: int):
        self.hub = hub
        self.db = hub.db
        self.olx = hub.olx
        self.interval = interval
        self._sem = asyncio.Semaphore(PARALLEL_ACCOUNTS)
        self._locks: dict[int, asyncio.Lock] = {}
        self._fails: dict[int, int] = {}

    async def run(self) -> None:
        last_cleanup = 0.0
        while True:
            started = time.monotonic()
            try:
                accounts = [a for a in await self.db.list_accounts() if a["status"] == "active"]
                await asyncio.gather(*(self._guarded(a["id"]) for a in accounts))
                if time.time() - last_cleanup > 86400:
                    await self.db.cleanup()
                    last_cleanup = time.time()
            except Exception:
                log.exception("Сбой цикла опроса")
            await asyncio.sleep(max(1.0, self.interval - (time.monotonic() - started)))

    async def _guarded(self, account_id: int) -> None:
        async with self._sem:
            try:
                await self.poll_account(account_id)
                if self._fails.get(account_id, 0) >= ALERT_AFTER_FAILS:
                    await self._on_recovered(account_id)
                self._fails[account_id] = 0
            except OlxAuthError:
                await self._on_auth_error(account_id)
            except (OlxError, OSError, asyncio.TimeoutError) as e:
                await self._on_error(account_id, e)
            except Exception as e:
                log.exception("Неожиданная ошибка аккаунта %s", account_id)
                await self._on_error(account_id, e)

    async def poll_account(self, account_id: int) -> None:
        # Один и тот же аккаунт не опрашиваем параллельно (фон + ручное подключение).
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            await self._poll(account_id)

    async def _poll(self, account_id: int) -> None:
        acc = await self.db.get_account(account_id)
        if acc is None:
            return
        threads = await self.olx.threads(account_id)
        known = await self.db.get_threads(account_id)
        first_run = not acc["baseline_done"]

        for t in threads:
            tid = thread_key(t)
            total = int(t.get("total_count") or 0)
            unread = int(t.get("unread_count") or 0)
            prev = known.get(tid)
            if prev and prev["total_count"] == total and prev["unread_count"] == unread:
                continue
            new_count = 0 if first_run else total - (prev["total_count"] if prev else 0)
            if new_count > 0:
                msgs = await self.olx.messages(account_id, tid)
                fresh = [m for m in msgs[-new_count:] if m.get("type") == "received"]
                fresh = [m for m in fresh if not await self.db.is_seen(account_id, message_key(m))]
                if fresh:
                    await self.hub.deliver(acc, t, tid, fresh)
            await self.db.save_thread(account_id, tid, t.get("advert_id"), t.get("interlocutor_id"),
                                      total, unread)

        if first_run:
            # Первое подключение: старую переписку не дублируем, только запоминаем.
            await self.db.set_account_field(account_id, "baseline_done", 1)

    async def _on_auth_error(self, account_id: int) -> None:
        acc = await self.db.get_account(account_id)
        if acc is None:
            return
        await self.db.set_account_field(account_id, "status", "auth_error")
        log.warning("Аккаунт %s потерял авторизацию", account_id)
        await self.hub.notify(
            f"⚠️ {acc['emoji']} <b>{esc(acc['label'])}</b>: OLX отозвал доступ "
            f"(сменили пароль, вышли из всех устройств или истёк срок).\n"
            f"Сообщения с этого аккаунта не приходят. Нажмите кнопку, чтобы переподключить.",
            reauth_kb(account_id))

    async def _on_error(self, account_id: int, err: Exception) -> None:
        n = self._fails.get(account_id, 0) + 1
        self._fails[account_id] = n
        log.warning("Ошибка опроса аккаунта %s (%s подряд): %s", account_id, n, err)
        if n == ALERT_AFTER_FAILS:
            acc = await self.db.get_account(account_id)
            if acc:
                await self.hub.notify(
                    f"⚠️ {acc['emoji']} <b>{esc(acc['label'])}</b>: OLX не отвечает уже несколько минут. "
                    f"Бот продолжает попытки и сам сообщит, когда связь восстановится.")

    async def _on_recovered(self, account_id: int) -> None:
        acc = await self.db.get_account(account_id)
        if acc:
            await self.hub.notify(f"✅ {acc['emoji']} <b>{esc(acc['label'])}</b>: связь с OLX восстановлена.")
