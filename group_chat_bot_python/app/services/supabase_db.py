from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from postgrest.exceptions import APIError
from supabase import Client, create_client

from app.config import Settings
from app.models import ChatSettings, ChatUser, MemoryRecord, Reminder, Sender, VerificationChallenge
from app.utils import birthday_is_today, normalize_search_text, transliterate_for_search


def _safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


class SupabaseDB:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: Client = create_client(settings.supabase_url, settings.supabase_key)
        self.message_authors: dict[int, dict[int, int]] = {}
        self.reaction_cooldowns: dict[str, float] = {}
        self.command_cooldowns: dict[int, dict[str, float]] = {}
        self.active_chats: dict[int, float] = {}
        self.pending_verifications: dict[int, VerificationChallenge] = {}
        self.last_birthday_check: dict[int, str] = {}
        self._rich_message_logs_supported: bool | None = None
        self._knowledge_entities_supported: bool | None = None
        
        # In-memory Cache
        self._user_cache: dict[str, tuple[ChatUser, float]] = {} # key -> (user, expires_at)
        self._chat_settings_cache: dict[int, tuple[ChatSettings, float]] = {}
        self._cache_ttl = 300 # 5 минут

    def _users(self):
        return self.client.table("users")

    def _bad_words(self):
        return self.client.table("bad_words")

    def _chats(self):
        return self.client.table("chats")

    def _message_logs(self):
        return self.client.table("message_logs")

    def _knowledge(self):
        return self.client.table("bot_knowledge")

    def _reminders(self):
        return self.client.table("reminders")

    def _feedback(self):
        return self.client.table("bot_feedback")

    def _persona(self):
        return self.client.table("bot_persona_state")

    def _debts(self):
        return self.client.table("bot_debts")

    def _sign_orders(self):
        return self.client.table("sign_orders")

    def _sign_price_options(self):
        return self.client.table("sign_price_options")

    def _bot_assets(self):
        return self.client.table("bot_assets")


    def _safe_execute(self, query_builder, *, fallback=None, context: str = ""):
        """
        Execute Supabase request with one retry to survive transient 502/HTML gateway responses.
        """
        last_error = None
        for attempt in range(2):
            try:
                return query_builder.execute()
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.2)
                    continue
        print(f"[DB:error] context={context} error={last_error}")
        return fallback

    def _user_from_row(self, row: dict[str, Any]) -> ChatUser:
        return ChatUser(
            id=row["id"],
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            first_name=row.get("first_name") or "Инкогнито",
            username=row.get("username"),
            xp=int(row.get("xp") or 0),
            level=int(row.get("level") or 1),
            reputation=int(row.get("reputation") or 0),
            warns=int(row.get("warns") or 0),
            last_message_time=int(row.get("last_message_time") or 0),
            birthday=row.get("birthday"),
            bio=row.get("bio"),
            ai_notes=row.get("ai_notes"),
            photo_url=row.get("photo_url"),
            last_daily_claim=row.get("last_daily_claim"),
            last_warn_at=row.get("last_warn_at"),
            flavor=row.get("flavor"),
            debt=int(row.get("debt") or 0),
            last_loan_at=row.get("last_loan_at"),
            jailed_until=row.get("jailed_until"),
            jail_reason=row.get("jail_reason"),
            steal_fail_streak=int(row.get("steal_fail_streak") or 0),
            steal_success_streak=int(row.get("steal_success_streak") or 0),
            sign_price=int(row.get("sign_price") or 0),
        )

    def _fallback_user(self, chat_id: int, sender: Sender) -> ChatUser:
        return ChatUser(
            id=0,
            chat_id=chat_id,
            user_id=sender.user_id,
            first_name=sender.first_name or "Unknown",
            username=sender.username,
            xp=0,
            level=1,
            reputation=0,
            warns=0,
            last_message_time=0,
        )

    def _reminder_from_row(self, row: dict[str, Any]) -> Reminder:
        return Reminder(
            id=row["id"],
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            text=row["text"],
            trigger_time=datetime.fromisoformat(str(row["trigger_time"]).replace("Z", "+00:00")),
            user_name=row.get("user_name"),
            is_sent=bool(row.get("is_sent", False)),
        )

    def get_or_create_user(self, chat_id: int, sender: Sender) -> ChatUser:
        cache_key = f"{chat_id}_{sender.user_id}"
        if cache_key in self._user_cache:
            user, expires_at = self._user_cache[cache_key]
            if time.time() < expires_at:
                return user
        
        response = self._safe_execute(
            self._users().select("*").eq("chat_id", chat_id).eq("user_id", sender.user_id).limit(1),
            fallback=None,
            context=f"get_or_create_user.select chat_id={chat_id} user_id={sender.user_id}",
        )
        if response and response.data:
            user = self.reset_expired_warns(self._user_from_row(response.data[0]))
            self._user_cache[cache_key] = (user, time.time() + self._cache_ttl)
            return user

        payload = {
            "chat_id": chat_id,
            "user_id": sender.user_id,
            "username": sender.username or "",
            "first_name": sender.first_name or "Инкогнито",
            "xp": 0,
            "level": 1,
            "reputation": 0,
            "warns": 0,
            "last_message_time": 0,
            "bio": "",
            "ai_notes": "",
            "debt": 0,
        }
        created = self._safe_execute(
            self._users().insert(payload),
            fallback=None,
            context=f"get_or_create_user.insert chat_id={chat_id} user_id={sender.user_id}",
        )
        if created and created.data:
            return self._user_from_row(created.data[0])
        return self._fallback_user(chat_id, sender)

    def get_user_by_platform_id(self, chat_id: int, user_id: int) -> ChatUser | None:
        cache_key = f"{chat_id}_{user_id}"
        if cache_key in self._user_cache:
            user, expires_at = self._user_cache[cache_key]
            if time.time() < expires_at:
                return user
                
        response = self._safe_execute(
            self._users().select("*").eq("chat_id", chat_id).eq("user_id", user_id).limit(1),
            fallback=None,
            context=f"get_user_by_platform_id chat_id={chat_id} user_id={user_id}",
        )
        if not response or not response.data:
            return None
        user = self.reset_expired_warns(self._user_from_row(response.data[0]))
        self._user_cache[cache_key] = (user, time.time() + self._cache_ttl)
        return user

    def update_last_message_time(self, chat_id: int, user_id: int) -> None:
        now = int(time.time())
        self._safe_execute(
            self._users().update({"last_message_time": now}).eq("chat_id", chat_id).eq("user_id", user_id),
            fallback=None,
            context=f"update_last_message_time chat_id={chat_id} user_id={user_id}",
        )

    def mark_chat_active(self, chat_id: int) -> None:
        self.active_chats[chat_id] = time.time()

    def update_user(self, db_id: int, updates: dict[str, Any]) -> ChatUser | None:
        payload = dict(updates)
        for field in ("reputation", "debt", "warns", "xp"):
            if field in payload and payload[field] is not None:
                payload[field] = max(0, int(payload[field]))
        if "level" in payload and payload["level"] is not None:
            payload["level"] = max(1, int(payload["level"]))
            
        # Сброс кэша для этого пользователя
        # Так как db_id не содержит chat_id/user_id, мы просто очищаем весь кэш пользователей 
        # или ищем совпадение. Для надежности при обновлении по db_id очистим кэш.
        self._user_cache.clear() 

        try:
            result = self._safe_execute(
                self._users().update(payload).eq("id", db_id),
                fallback=None,
                context=f"update_user db_id={db_id}",
            )
            if result is None:
                return None
        except APIError as exc:
            # Backward compatibility: some deployments do not yet have `last_warn_at`.
            # Retry once without that field so runtime does not crash on moderation events.
            if "last_warn_at" in payload and "last_warn_at" in str(exc):
                payload.pop("last_warn_at", None)
                result = self._safe_execute(
                    self._users().update(payload).eq("id", db_id),
                    fallback=None,
                    context=f"update_user.retry_without_last_warn_at db_id={db_id}",
                )
            else:
                print(f"[DB:error] context=update_user db_id={db_id} error={exc}")
                return None
        except Exception as exc:
            print(f"[DB:error] context=update_user db_id={db_id} error={exc}")
            return None

        if not result or not result.data:
            return None
        return self._user_from_row(result.data[0])

    def get_next_level_xp(self, level: int) -> int:
        return 50 * level * level + 50 * level

    def reset_expired_warns(self, user: ChatUser) -> ChatUser:
        if not user.warns or not user.last_warn_at:
            return user
        try:
            last_warn = datetime.fromisoformat(user.last_warn_at.replace("Z", "+00:00"))
        except ValueError:
            return user
        if datetime.now(timezone.utc) - last_warn < timedelta(days=self.settings.warn_decay_days):
            return user
        updated = self.update_user(user.id, {"warns": 0, "last_warn_at": None})
        if updated:
            return updated
        user.warns = 0
        user.last_warn_at = None
        return user

    def apply_message_xp(self, user: ChatUser) -> tuple[ChatUser | None, bool]:
        if user.id <= 0:
            return user, False
        now = int(time.time())
        # Кулдаун 60 секунд на получение опыта
        if now - user.last_message_time < 60:
            return user, False

        gained = random.randint(15, 25)
        xp = user.xp + gained
        level = user.level
        level_up = False
        while xp >= self.get_next_level_xp(level):
            level += 1
            level_up = True

        # Проверка долга и штрафы
        if user.debt > 0 and user.last_loan_at:
            try:
                loan_time = datetime.fromisoformat(user.last_loan_at.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - loan_time > timedelta(days=1):
                    # Если долгу больше 24ч, есть 10% шанс потерять 1 печеньку вместо получения опыта
                    if random.random() < 0.1:
                        self.update_user(user.id, {"reputation": max(0, user.reputation - 1)})
                        return user, False
            except Exception:
                pass

        updated = self.update_user(
            user.id,
            {"xp": xp, "level": level, "last_message_time": now},
        )
        return updated, level_up

    def claim_daily_bonus(self, user: ChatUser) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        if user.last_daily_claim:
            last = datetime.fromisoformat(user.last_daily_claim.replace("Z", "+00:00"))
            if now - last < timedelta(hours=24):
                remaining = timedelta(hours=24) - (now - last)
                hours = remaining.seconds // 3600
                minutes = (remaining.seconds % 3600) // 60
                return {"success": False, "hours": hours, "minutes": minutes}

        bonus_xp = random.randint(self.settings.daily_xp_min, self.settings.daily_xp_max)
        level_bonus = min(60, max(0, user.level - 1) * 4)
        bonus_reputation = random.randint(90, 160) + level_bonus
        lucky_bonus = 100 if random.random() < 0.06 else 0
        bonus_reputation += lucky_bonus
        is_rep_gained = bonus_reputation > 0
        new_reputation = user.reputation + bonus_reputation
        xp = user.xp + bonus_xp
        level = user.level
        level_up = False
        while xp >= self.get_next_level_xp(level):
            level += 1
            level_up = True

        updated = self.update_user(
            user.id,
            {
                "xp": xp,
                "level": level,
                "reputation": new_reputation,
                "last_daily_claim": now.isoformat(),
            },
        )
        return {
            "success": True,
            "bonus_xp": bonus_xp,
            "bonus_reputation": bonus_reputation,
            "lucky_bonus": lucky_bonus,
            "is_rep_gained": is_rep_gained,
            "new_reputation": new_reputation,
            "new_level": updated.level if updated else level,
            "level_up": level_up,
        }

    def get_top_users(self, chat_id: int, limit: int = 10, order_by: str = "xp") -> list[ChatUser]:
        response = self._safe_execute(
            self._users().select("*").eq("chat_id", chat_id).order(order_by, desc=True).limit(limit),
            fallback=None,
            context=f"get_top_users chat_id={chat_id} order_by={order_by}",
        )
        if not response or not response.data:
            return []
        return [self._user_from_row(row) for row in response.data]

    def get_user_rank(self, chat_id: int, user_id: int, column: str = "xp") -> int:
        """Получить место пользователя в топе чата по выбранной колонке"""
        user_resp = self._safe_execute(
            self._users().select(column).eq("chat_id", chat_id).eq("user_id", user_id).limit(1),
            fallback=None
        )
        if not user_resp or not user_resp.data:
            return 0
            
        value = user_resp.data[0].get(column, 0)
        
        # Считаем количество пользователей, у которых значение больше
        # Используем rpc или просто запрос с count
        count_resp = self._safe_execute(
            self._users().select("id", count="exact").eq("chat_id", chat_id).gt(column, value),
            fallback=None
        )
        if count_resp and count_resp.count is not None:
            return count_resp.count + 1
        return 0

    def get_active_users(self, chat_id: int, minutes: int = 60, limit: int = 20) -> list[ChatUser]:
        """Get users active in the last X minutes"""
        now = int(time.time())
        since = now - (minutes * 60)
        
        response = self._safe_execute(
            self._users()
            .select("*")
            .eq("chat_id", chat_id)
            .gt("last_message_time", since)
            .order("last_message_time", desc=True)
            .limit(limit),
            fallback=None,
            context=f"get_active_users chat_id={chat_id}",
        )
        
        if not response or not response.data:
            return []
            
        users = []
        for row in response.data:
            user_time = int(row.get("last_message_time") or 0)
            
            # Защита от миллисекунд (если число слишком большое, делим на 1000)
            # 10**11 - это порог, выше которого начинаются миллисекунды для нашего времени
            if user_time > 10**11:
                user_time //= 1000
                # Обновляем в базе, чтобы больше не спотыкаться
                self._users().update({"last_message_time": user_time}).eq("id", row["id"]).execute()
            
            # Финальная проверка: действительно ли это было в пределах окна (на случай если gt не сработал из-за типов)
            if user_time >= since:
                users.append(self._user_from_row({**row, "last_message_time": user_time}))
                
        return users

    def get_all_users(self, chat_id: int) -> list[ChatUser]:
        response = self._safe_execute(
            self._users().select("*").eq("chat_id", chat_id).limit(300),
            fallback=None,
            context=f"get_all_users chat_id={chat_id}",
        )
        if not response:
            return []
        return [self.reset_expired_warns(self._user_from_row(row)) for row in response.data or []]

    def get_active_chat_ids(self, minutes: int = 180, limit: int = 50) -> list[int]:
        now = time.time()
        since_float = now - minutes * 60
        chat_ids: list[int] = []
        for chat_id, last_seen in sorted(self.active_chats.items(), key=lambda item: item[1], reverse=True):
            if last_seen < since_float:
                self.active_chats.pop(chat_id, None)
                continue
            chat_ids.append(chat_id)
            if len(chat_ids) >= limit:
                break
        return chat_ids

    def get_chat_settings(self, chat_id: int) -> ChatSettings:
        if chat_id in self._chat_settings_cache:
            settings, expires_at = self._chat_settings_cache[chat_id]
            if time.time() < expires_at:
                return settings

        response = self._safe_execute(
            self._chats().select("*").eq("chat_id", chat_id).limit(1),
            fallback=None,
            context=f"get_chat_settings.select chat_id={chat_id}",
        )
        if response and response.data:
            row = response.data[0]
            settings = ChatSettings(
                chat_id=chat_id,
                link_filter_enabled=bool(row.get("link_filter_enabled", True)),
                casino_jackpot=int(row.get("casino_jackpot", 0))
            )
            self._chat_settings_cache[chat_id] = (settings, time.time() + self._cache_ttl)
            return settings
        created = self._safe_execute(
            self._chats().insert(
                {
                    "chat_id": chat_id, 
                    "link_filter_enabled": self.settings.link_filter_default,
                    "casino_jackpot": 0
                }
            ),
            fallback=None,
            context=f"get_chat_settings.insert chat_id={chat_id}",
        )
        if created and created.data:
            row = created.data[0]
            return ChatSettings(
                chat_id=chat_id, 
                link_filter_enabled=bool(row.get("link_filter_enabled", True)),
                casino_jackpot=int(row.get("casino_jackpot", 0))
            )

        # Safe fallback when DB is temporarily unavailable.
        return ChatSettings(chat_id=chat_id, link_filter_enabled=self.settings.link_filter_default)

    def update_chat_settings(self, chat_id: int, **updates: Any) -> bool:
        self._chat_settings_cache.pop(chat_id, None)
        result = self._safe_execute(
            self._chats().update(updates).eq("chat_id", chat_id),
            fallback=None,
            context=f"update_chat_settings chat_id={chat_id}",
        )
        return bool(result)

    def get_bad_words(self, chat_id: int) -> list[str]:
        response = self._safe_execute(
            self._bad_words().select("word").eq("chat_id", chat_id),
            fallback=None,
            context=f"get_bad_words chat_id={chat_id}",
        )
        if not response:
            return []
        return [row["word"] for row in response.data or []]

    def add_bad_word(self, chat_id: int, word: str) -> None:
        self._safe_execute(
            self._bad_words().insert({"chat_id": chat_id, "word": word.lower()}),
            fallback=None,
            context=f"add_bad_word chat_id={chat_id}",
        )

    def remove_bad_word(self, chat_id: int, word: str) -> None:
        self._safe_execute(
            self._bad_words().delete().eq("chat_id", chat_id).eq("word", word.lower()),
            fallback=None,
            context=f"remove_bad_word chat_id={chat_id}",
        )

    def set_bio(self, user: ChatUser, bio: str) -> ChatUser | None:
        clean_bio = bio[:100]
        updated = self.update_user(user.id, {"bio": clean_bio})
        self.store_memory(
            user.chat_id,
            MemoryRecord(
                fact=f"{user.display_name} рассказал о себе: {clean_bio}",
                source="profile_bio",
                confidence=0.95,
                meta={"user_id": user.user_id},
                entity_user_id=user.user_id,
                entity_name=user.display_name,
            ),
        )
        return updated

    def append_ai_note(self, user: ChatUser, note: str) -> ChatUser | None:
        clean_note = note.strip()[:300]
        if not clean_note:
            return user
        old_notes = (user.ai_notes or "").strip()
        final_notes = f"{old_notes}\n- {clean_note}".strip() if old_notes else f"- {clean_note}"
        updated = self.update_user(user.id, {"ai_notes": final_notes})
        self.store_memory(
            user.chat_id,
            MemoryRecord(
                fact=f"Заметка о {user.display_name}: {clean_note}",
                source="ai_note",
                confidence=0.85,
                meta={"user_id": user.user_id},
                entity_user_id=user.user_id,
                entity_name=user.display_name,
            ),
        )
        return updated

    def set_birthday(self, user: ChatUser, birthday: str) -> ChatUser | None:
        updated = self.update_user(user.id, {"birthday": birthday})
        self.store_memory(
            user.chat_id,
            MemoryRecord(
                fact=f"День рождения {user.display_name}: {birthday}",
                source="profile_birthday",
                confidence=0.98,
                meta={"user_id": user.user_id},
                entity_user_id=user.user_id,
                entity_name=user.display_name,
            ),
        )
        return updated

    def search_user(self, chat_id: int, query: str) -> ChatUser | None:
        if not query:
            return None

        stripped = query.strip()

        # Поиск по числовому user_id
        if re.fullmatch(r"-?\d+", stripped):
            target_id = int(stripped)
            return self.get_user_by_platform_id(chat_id, target_id)

        normalized = normalize_search_text(stripped).replace("@", "")
        if not normalized:
            return None

        # Транслитерированный вариант запроса (cyr→lat или lat→cyr)
        translit = transliterate_for_search(stripped)
        translit_norm = normalize_search_text(translit).replace("@", "") if translit != normalized else ""

        response = self._safe_execute(
            self._users().select("*").eq("chat_id", chat_id).limit(200),
            fallback=None,
            context=f"search_user chat_id={chat_id}",
        )
        if not response:
            return None
        candidates = response.data or []

        def _all_fields(row: dict[str, Any]) -> list[str]:
            """Возвращает все поля для поиска, включая транслит-варианты."""
            fields = []
            first_name = normalize_search_text(row.get("first_name"))
            username = normalize_search_text(row.get("username"))
            bio = normalize_search_text(row.get("bio"))
            notes = normalize_search_text(row.get("ai_notes"))

            for f in (first_name, username, bio, notes):
                if f:
                    fields.append(f)
                    # Добавляем транслит-вариант каждого поля
                    tr = normalize_search_text(transliterate_for_search(f))
                    if tr and tr != f:
                        fields.append(tr)
            return fields

        def score(row: dict[str, Any]) -> int:
            fields = _all_fields(row)
            queries = [q for q in [normalized, translit_norm] if q]
            best = 0
            for q in queries:
                for field in fields:
                    if not field:
                        continue
                    if field == q:
                        best = max(best, 140)
                    elif field.startswith(q):
                        best = max(best, 120)
                    elif q in field:
                        best = max(best, 100)
                    elif any(q == tok for tok in field.split()):
                        best = max(best, 110)
            return best

        def fuzzy_score(row: dict[str, Any]) -> float:
            from difflib import SequenceMatcher
            fields = _all_fields(row)
            queries = [q for q in [normalized, translit_norm] if q]
            best = 0.0
            for q in queries:
                for field in fields:
                    if not field or len(field) < 2:
                        continue
                    ratio = SequenceMatcher(None, q, field).ratio()
                    best = max(best, ratio)
                    if len(q) >= 3 and len(field) > len(q):
                        for start in range(0, len(field) - len(q) + 1):
                            sub = field[start:start + len(q)]
                            r = SequenceMatcher(None, q, sub).ratio()
                            best = max(best, r)
            return best

        ranked = sorted(((score(row), row) for row in candidates), key=lambda item: item[0], reverse=True)

        if ranked and ranked[0][0] >= 100:
            return self.reset_expired_warns(self._user_from_row(ranked[0][1]))

        # Fuzzy fallback: порог 0.72
        if len(normalized) >= 3:
            fuzzy_ranked = sorted(
                ((fuzzy_score(row), row) for row in candidates),
                key=lambda item: item[0],
                reverse=True,
            )
            if fuzzy_ranked and fuzzy_ranked[0][0] >= 0.72:
                return self.reset_expired_warns(self._user_from_row(fuzzy_ranked[0][1]))

        return None

    def get_birthdays_today(self, chat_id: int) -> list[ChatUser]:
        return [user for user in self.get_all_users(chat_id) if birthday_is_today(user.birthday)]

    def apply_warn(self, user: ChatUser) -> ChatUser | None:
        return self.update_user(
            user.id,
            {"warns": user.warns + 1, "last_warn_at": datetime.now(timezone.utc).isoformat()},
        )

    def clear_warns(self, user: ChatUser) -> ChatUser | None:
        return self.update_user(user.id, {"warns": 0, "last_warn_at": None})

    def transfer_cookies(self, sender: ChatUser, receiver: ChatUser, amount: int) -> bool:
        if amount <= 0 or sender.reputation < amount or sender.user_id == receiver.user_id:
            return False
        self.update_user(sender.id, {"reputation": sender.reputation - amount})
        self.update_user(receiver.id, {"reputation": receiver.reputation + amount})
        return True

    def set_sign_price(self, user: ChatUser, amount: int) -> ChatUser | None:
        return self.update_user(user.id, {"sign_price": max(0, amount)})

    def save_bot_asset(self, asset_key: str, payload_base64: str, mime_type: str, updated_by: int | None = None) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        result = self._safe_execute(
            self._bot_assets().upsert(
                {
                    "asset_key": asset_key,
                    "mime_type": mime_type,
                    "payload_base64": payload_base64,
                    "updated_at": now,
                    "updated_by": updated_by,
                }
            ),
            fallback=None,
            context=f"save_bot_asset key={asset_key}",
        )
        return bool(result and result.data)

    def get_bot_asset(self, asset_key: str) -> dict[str, Any] | None:
        result = self._safe_execute(
            self._bot_assets().select("*").eq("asset_key", asset_key).limit(1),
            fallback=None,
            context=f"get_bot_asset key={asset_key}",
        )
        if result and result.data:
            return result.data[0]
        return None

    def create_sign_price_option(self, user: ChatUser, title: str, price: int, description: str = "") -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        result = self._safe_execute(
            self._sign_price_options().insert(
                {
                    "chat_id": user.chat_id,
                    "user_id": user.user_id,
                    "title": title[:80],
                    "description": description[:300],
                    "price": price,
                    "is_active": True,
                    "created_at": now,
                    "updated_at": now,
                }
            ),
            fallback=None,
            context=f"create_sign_price_option chat_id={user.chat_id} user_id={user.user_id}",
        )
        if result and result.data:
            return result.data[0]
        return None

    def list_sign_price_options(self, chat_id: int, user_id: int, *, active_only: bool = True) -> list[dict[str, Any]]:
        query = self._sign_price_options().select("*").eq("chat_id", chat_id).eq("user_id", user_id)
        if active_only:
            query = query.eq("is_active", True)
        result = self._safe_execute(
            query.order("price", desc=False).order("created_at", desc=False),
            fallback=None,
            context=f"list_sign_price_options chat_id={chat_id} user_id={user_id}",
        )
        return list(result.data) if result and result.data else []

    def get_sign_price_option(self, chat_id: int, user_id: int, option_id: int) -> dict[str, Any] | None:
        result = self._safe_execute(
            self._sign_price_options()
            .select("*")
            .eq("chat_id", chat_id)
            .eq("user_id", user_id)
            .eq("id", option_id)
            .eq("is_active", True)
            .limit(1),
            fallback=None,
            context=f"get_sign_price_option chat_id={chat_id} option_id={option_id}",
        )
        if result and result.data:
            return result.data[0]
        return None

    def disable_sign_price_option(self, chat_id: int, user_id: int, option_id: int) -> bool:
        result = self._safe_execute(
            self._sign_price_options()
            .update({"is_active": False, "updated_at": datetime.now(timezone.utc).isoformat()})
            .eq("chat_id", chat_id)
            .eq("user_id", user_id)
            .eq("id", option_id),
            fallback=None,
            context=f"disable_sign_price_option chat_id={chat_id} option_id={option_id}",
        )
        return bool(result and result.data)

    def create_sign_order(
        self,
        buyer: ChatUser,
        author: ChatUser,
        price: int,
        text: str,
        option_id: int | None = None,
        option_title: str | None = None,
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        result = self._safe_execute(
            self._sign_orders().insert(
                {
                    "chat_id": buyer.chat_id,
                    "buyer_id": buyer.user_id,
                    "buyer_name": buyer.display_name,
                    "author_id": author.user_id,
                    "author_name": author.display_name,
                    "price": price,
                    "option_id": option_id,
                    "option_title": option_title,
                    "escrow_amount": 0,
                    "text": text[:500],
                    "status": "pending",
                    "created_at": now,
                    "updated_at": now,
                }
            ),
            fallback=None,
            context=f"create_sign_order chat_id={buyer.chat_id}",
        )
        if result and result.data:
            return result.data[0]
        return None

    def get_sign_order(self, chat_id: int, order_id: int) -> dict[str, Any] | None:
        result = self._safe_execute(
            self._sign_orders().select("*").eq("chat_id", chat_id).eq("id", order_id).limit(1),
            fallback=None,
            context=f"get_sign_order chat_id={chat_id} order_id={order_id}",
        )
        if result and result.data:
            return result.data[0]
        return None

    def update_sign_order(self, order_id: int, updates: dict[str, Any]) -> dict[str, Any] | None:
        payload = dict(updates)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        result = self._safe_execute(
            self._sign_orders().update(payload).eq("id", order_id),
            fallback=None,
            context=f"update_sign_order order_id={order_id}",
        )
        if result and result.data:
            return result.data[0]
        return None

    def list_sign_orders(self, chat_id: int, user_id: int, role: str = "author", limit: int = 10) -> list[dict[str, Any]]:
        column = "buyer_id" if role == "buyer" else "author_id"
        result = self._safe_execute(
            self._sign_orders()
            .select("*")
            .eq("chat_id", chat_id)
            .eq(column, user_id)
            .order("created_at", desc=True)
            .limit(limit),
            fallback=None,
            context=f"list_sign_orders chat_id={chat_id} user_id={user_id} role={role}",
        )
        return list(result.data) if result and result.data else []

    def sign_order_stats(self, chat_id: int, user_id: int) -> dict[str, int]:
        authored = self.list_sign_orders(chat_id, user_id, "author", limit=100)
        bought = self.list_sign_orders(chat_id, user_id, "buyer", limit=100)
        earned = sum(int(row.get("price") or 0) for row in authored if row.get("status") == "paid")
        spent = sum(int(row.get("price") or 0) for row in bought if row.get("status") == "paid")
        return {
            "authored_total": len(authored),
            "authored_active": sum(1 for row in authored if row.get("status") in {"pending", "accepted", "delivered"}),
            "authored_paid": sum(1 for row in authored if row.get("status") == "paid"),
            "bought_total": len(bought),
            "bought_active": sum(1 for row in bought if row.get("status") in {"pending", "accepted", "delivered"}),
            "earned": earned,
            "spent": spent,
        }

    def create_debt(self, lender: ChatUser, borrower: ChatUser, amount: int, due_hours: int = 48) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        result = self._safe_execute(
            self._debts().insert(
                {
                    "chat_id": borrower.chat_id,
                    "lender_id": lender.user_id,
                    "lender_name": lender.display_name,
                    "borrower_id": borrower.user_id,
                    "borrower_name": borrower.display_name,
                    "amount": amount,
                    "paid_amount": 0,
                    "forgiven_amount": 0,
                    "status": "active",
                    "created_at": now.isoformat(),
                    "due_at": (now + timedelta(hours=due_hours)).isoformat(),
                }
            ),
            fallback=None,
            context=f"create_debt chat_id={borrower.chat_id}",
        )
        if result and result.data:
            return result.data[0]
        return None

    def get_active_debts_for_borrower(self, chat_id: int, borrower_id: int) -> list[dict[str, Any]]:
        response = self._safe_execute(
            self._debts()
            .select("*")
            .eq("chat_id", chat_id)
            .eq("borrower_id", borrower_id)
            .eq("status", "active")
            .order("created_at", desc=False),
            fallback=None,
            context=f"get_active_debts_for_borrower chat_id={chat_id} borrower_id={borrower_id}",
        )
        return response.data if response and response.data else []

    def get_active_debts_for_lender(self, chat_id: int, lender_id: int) -> list[dict[str, Any]]:
        response = self._safe_execute(
            self._debts()
            .select("*")
            .eq("chat_id", chat_id)
            .eq("lender_id", lender_id)
            .eq("status", "active")
            .order("created_at", desc=False),
            fallback=None,
            context=f"get_active_debts_for_lender chat_id={chat_id} lender_id={lender_id}",
        )
        return response.data if response and response.data else []

    def repay_debts(self, borrower: ChatUser, amount: int) -> dict[str, Any]:
        if amount <= 0 or borrower.reputation < amount:
            return {"paid": 0, "payments": []}

        debts = self.get_active_debts_for_borrower(borrower.chat_id, borrower.user_id)
        remaining_to_pay = min(amount, borrower.debt)
        paid_total = 0
        payments: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc).isoformat()

        for debt in debts:
            if remaining_to_pay <= 0:
                break
            debt_amount = int(debt.get("amount") or 0)
            paid_amount = int(debt.get("paid_amount") or 0)
            forgiven_amount = int(debt.get("forgiven_amount") or 0)
            open_amount = max(0, debt_amount - paid_amount - forgiven_amount)
            if open_amount <= 0:
                continue

            chunk = min(open_amount, remaining_to_pay)
            new_paid = paid_amount + chunk
            updates: dict[str, Any] = {"paid_amount": new_paid}
            if new_paid + forgiven_amount >= debt_amount:
                updates["status"] = "repaid"
                updates["repaid_at"] = now

            self._safe_execute(
                self._debts().update(updates).eq("id", debt["id"]),
                fallback=None,
                context=f"repay_debts debt_id={debt['id']}",
            )

            lender = self.get_user_by_platform_id(borrower.chat_id, int(debt["lender_id"]))
            if lender:
                self.update_user(lender.id, {"reputation": lender.reputation + chunk})

            paid_total += chunk
            remaining_to_pay -= chunk
            payments.append({"lender_name": debt.get("lender_name") or str(debt["lender_id"]), "amount": chunk})

        if paid_total > 0:
            self.update_user(
                borrower.id,
                {"reputation": borrower.reputation - paid_total, "debt": max(0, borrower.debt - paid_total)},
            )
        return {"paid": paid_total, "payments": payments}

    def forgive_debts(self, lender: ChatUser, borrower_id: int, amount: int) -> dict[str, Any]:
        if amount <= 0:
            return {"forgiven": 0, "borrower": None}

        response = self._safe_execute(
            self._debts()
            .select("*")
            .eq("chat_id", lender.chat_id)
            .eq("lender_id", lender.user_id)
            .eq("borrower_id", borrower_id)
            .eq("status", "active")
            .order("created_at", desc=False),
            fallback=None,
            context=f"forgive_debts chat_id={lender.chat_id}",
        )
        debts = response.data if response and response.data else []
        borrower = self.get_user_by_platform_id(lender.chat_id, borrower_id)
        remaining = amount
        forgiven_total = 0
        now = datetime.now(timezone.utc).isoformat()

        for debt in debts:
            if remaining <= 0:
                break
            debt_amount = int(debt.get("amount") or 0)
            paid_amount = int(debt.get("paid_amount") or 0)
            forgiven_amount = int(debt.get("forgiven_amount") or 0)
            open_amount = max(0, debt_amount - paid_amount - forgiven_amount)
            if open_amount <= 0:
                continue
            chunk = min(open_amount, remaining)
            new_forgiven = forgiven_amount + chunk
            updates: dict[str, Any] = {"forgiven_amount": new_forgiven}
            if paid_amount + new_forgiven >= debt_amount:
                updates["status"] = "forgiven"
                updates["forgiven_at"] = now
            self._safe_execute(
                self._debts().update(updates).eq("id", debt["id"]),
                fallback=None,
                context=f"forgive_debts debt_id={debt['id']}",
            )
            forgiven_total += chunk
            remaining -= chunk

        if borrower and forgiven_total > 0:
            self.update_user(borrower.id, {"debt": max(0, borrower.debt - forgiven_total)})
        return {"forgiven": forgiven_total, "borrower": borrower}

    def purchase_item(self, user: ChatUser, item_id: int) -> tuple[bool, str]:
        if item_id == 1:
            cost = 1200
            if user.reputation < cost:
                return False, f"Недостаточно печенек. Нужно {cost} 🍪."
            self.update_user(user.id, {"level": user.level + 1, "xp": 0, "reputation": user.reputation - cost})
            return True, f"Уровень куплен. Теперь у тебя {user.level + 1} уровень."
        if item_id == 2:
            cost = 600
            if user.reputation < cost:
                return False, "Недостаточно печенек."
            self.update_user(user.id, {"warns": 0, "reputation": user.reputation - cost, "last_warn_at": None})
            return True, "Все предупреждения сняты."
        return False, "Неизвестный товар."

    def can_use_command(self, chat_id: int, command_name: str, cooldown_seconds: int) -> tuple[bool, int]:
        now = time.time()
        store = self.command_cooldowns.setdefault(chat_id, {})
        last_used = store.get(command_name, 0.0)
        if now - last_used < cooldown_seconds:
            return False, int(cooldown_seconds - (now - last_used) + 0.999)
        store[command_name] = now
        return True, 0

    def can_user_use_command(self, chat_id: int, user_id: int, command_name: str, cooldown_seconds: int) -> tuple[bool, int]:
        now = time.time()
        key = f"{chat_id}_{user_id}_{command_name}"
        last_used = self.reaction_cooldowns.get(key, 0.0)
        if now - last_used < cooldown_seconds:
            return False, int(cooldown_seconds - (now - last_used) + 0.999)
        self.reaction_cooldowns[key] = now
        return True, 0

    def can_adjust_reputation(self, actor_id: int, target_id: int, cooldown_seconds: int = 20) -> bool:
        key = f"{actor_id}_{target_id}"
        now = time.time()
        if now - self.reaction_cooldowns.get(key, 0.0) < cooldown_seconds:
            return False
        self.reaction_cooldowns[key] = now
        return True

    def store_message_author(self, chat_id: int, message_id: int, user_id: int) -> None:
        authors = self.message_authors.setdefault(chat_id, {})
        authors[message_id] = user_id
        if len(authors) > 1000:
            for key in list(authors.keys())[:-1000]:
                authors.pop(key, None)
        self._safe_execute(
            self._message_logs().upsert(
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "user_id": user_id,
                },
                on_conflict="chat_id,message_id",
            ),
            fallback=None,
            context=f"store_message_author chat_id={chat_id} message_id={message_id}",
        )

    def store_message_context(
        self,
        chat_id: int,
        message_id: int,
        sender: Sender,
        text: str,
        *,
        message_type: str = "text",
        reply_to_message_id: int | None = None,
    ) -> None:
        authors = self.message_authors.setdefault(chat_id, {})
        authors[message_id] = sender.user_id
        if len(authors) > 1000:
            for key in list(authors.keys())[:-1000]:
                authors.pop(key, None)

        base_payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "user_id": sender.user_id,
        }

        if self._rich_message_logs_supported is not False:
            rich_payload = {
                **base_payload,
                "sender_name": sender.display_name,
                "username": sender.username or "",
                "is_bot": sender.is_bot,
                "text": (text or "").strip()[:2000],
                "message_type": (message_type or "text")[:40],
                "reply_to_message_id": reply_to_message_id,
            }
            rich_result = self._safe_execute(
                self._message_logs().upsert(rich_payload, on_conflict="chat_id,message_id"),
                fallback=None,
                context=f"store_message_context.rich chat_id={chat_id} message_id={message_id}",
            )
            if rich_result is not None:
                self._rich_message_logs_supported = True
                return
            self._rich_message_logs_supported = False

        self._safe_execute(
            self._message_logs().upsert(base_payload, on_conflict="chat_id,message_id"),
            fallback=None,
            context=f"store_message_context.basic chat_id={chat_id} message_id={message_id}",
        )

    def get_message_author(self, chat_id: int, message_id: int) -> int | None:
        cached = self.message_authors.get(chat_id, {}).get(message_id)
        if cached:
            return cached
        response = self._safe_execute(
            self._message_logs().select("user_id").eq("chat_id", chat_id).eq("message_id", message_id).limit(1),
            fallback=None,
            context=f"get_message_author chat_id={chat_id} message_id={message_id}",
        )
        if response and response.data:
            user_id = int(response.data[0]["user_id"])
            self.message_authors.setdefault(chat_id, {})[message_id] = user_id
            return user_id
        return None

    def get_recent_message_context(
        self,
        chat_id: int,
        *,
        limit: int = 12,
        exclude_message_id: int | None = None,
    ) -> list[str]:
        if self._rich_message_logs_supported is False:
            return []

        safe_limit = max(0, min(50, int(limit or 0)))
        if safe_limit <= 0:
            return []

        response = self._safe_execute(
            self._message_logs()
            .select("message_id,user_id,sender_name,username,is_bot,text,message_type,reply_to_message_id,created_at")
            .eq("chat_id", chat_id)
            .order("message_id", desc=True)
            .limit(safe_limit + 3),
            fallback=None,
            context=f"get_recent_message_context chat_id={chat_id}",
        )
        if not response or not response.data:
            if self._rich_message_logs_supported is None:
                self._rich_message_logs_supported = False
            return []

        self._rich_message_logs_supported = True
        # Первый проход: собираем имена по message_id для раскрытия reply_to
        id_to_name: dict[int, str] = {}
        for row in response.data:
            mid = int(row.get("message_id") or 0)
            sname = str(row.get("sender_name") or "").strip()
            uid = int(row.get("user_id") or 0)
            if mid and sname:
                id_to_name[mid] = f"{sname} (user_id={uid})"

        lines: list[str] = []
        for row in reversed(response.data):
            message_id = int(row.get("message_id") or 0)
            if exclude_message_id is not None and message_id == exclude_message_id:
                continue

            raw_text = str(row.get("text") or "").strip()
            if not raw_text:
                continue

            sender_name = str(row.get("sender_name") or "").strip()
            if not sender_name:
                username = str(row.get("username") or "").strip()
                sender_name = f"@{username}" if username else f"user_id={row.get('user_id')}"

            user_id = int(row.get("user_id") or 0)
            message_type = str(row.get("message_type") or "text").strip()
            reply_id = row.get("reply_to_message_id")

            # Раскрываем reply_to в читаемое имя автора
            if reply_id:
                reply_author = id_to_name.get(int(reply_id))
                if reply_author:
                    reply_note = f" ↩ ответ_на=[{reply_author}]"
                else:
                    reply_note = f" ↩ ответ_на=[msg_id={reply_id}]"
            else:
                reply_note = ""

            type_note = "" if message_type in ("text", "bot_reply") else f" [{message_type}]"
            lines.append(f"{sender_name} (user_id={user_id}, msg={message_id}{type_note}){reply_note}: {raw_text[:900]}")
            if len(lines) >= safe_limit:
                break
        return lines


    def store_memory(self, chat_id: int, memory: MemoryRecord) -> None:
        meta = memory.meta or {}
        entity_user_id = memory.entity_user_id or _safe_int(meta.get("user_id") or meta.get("entity_user_id"))
        entity_name = memory.entity_name or str(meta.get("user_name") or meta.get("entity_name") or "").strip() or None
        source_message_id = memory.source_message_id or _safe_int(meta.get("source_message_id"))
        base_payload = {
            "chat_id": chat_id,
            "fact": memory.fact,
            "fact_type": memory.source,
            "confidence": memory.confidence,
            "status": "confirmed",
            "meta": meta,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
        }

        if self._knowledge_entities_supported is not False:
            rich_payload = {
                **base_payload,
                "entity_user_id": entity_user_id,
                "entity_name": entity_name,
                "source_message_id": source_message_id,
            }
            result = self._safe_execute(
                self._knowledge().insert(rich_payload),
                fallback=None,
                context=f"store_memory.rich chat_id={chat_id}",
            )
            if result is not None:
                self._knowledge_entities_supported = True
                return
            self._knowledge_entities_supported = False

        self._safe_execute(
            self._knowledge().insert(base_payload),
            fallback=None,
            context=f"store_memory chat_id={chat_id}",
        )

    def memory_exists(self, chat_id: int, fact: str) -> bool:
        rows = self._safe_execute(
            self._knowledge().select("id").eq("chat_id", chat_id).eq("fact", fact).eq("status", "confirmed").limit(1),
            fallback=None,
            context=f"memory_exists chat_id={chat_id}",
        )
        return bool(rows and rows.data)

    def delete_memory(self, chat_id: int, query: str) -> int:
        if not query.strip():
            return 0
        rows = self._safe_execute(
            self._knowledge().delete().eq("chat_id", chat_id).ilike("fact", f"%{query}%"),
            fallback=None,
            context=f"delete_memory chat_id={chat_id}",
        )
        if not rows or not rows.data:
            return 0
        return len(rows.data)

    def search_memory(self, chat_id: int, query: str, limit: int = 5) -> list[str]:
        if not query.strip():
            return []
        rows = self._safe_execute(
            self._knowledge()
            .select("fact,last_seen_at")
            .eq("chat_id", chat_id)
            .eq("status", "confirmed")
            .ilike("fact", f"%{query}%")
            .order("last_seen_at", desc=True)
            .limit(limit),
            fallback=None,
            context=f"search_memory chat_id={chat_id}",
        )
        if not rows:
            return []
        return [row["fact"] for row in rows.data or []]

    def get_recent_memories(self, chat_id: int, limit: int = 5) -> list[str]:
        rows = self._safe_execute(
            self._knowledge()
            .select("fact,last_seen_at")
            .eq("chat_id", chat_id)
            .eq("status", "confirmed")
            .order("last_seen_at", desc=True)
            .limit(limit),
            fallback=None,
            context=f"get_recent_memories chat_id={chat_id}",
        )
        if not rows:
            return []
        return [row["fact"] for row in rows.data or []]

    def get_all_user_facts(self, chat_id: int, user_name: str, limit: int = 10) -> list[str]:
        rows = self._safe_execute(
            self._knowledge()
            .select("fact")
            .eq("chat_id", chat_id)
            .eq("status", "confirmed")
            .ilike("fact", f"%{user_name}%")
            .limit(limit),
            fallback=None,
            context=f"get_all_user_facts chat_id={chat_id}",
        )
        if not rows:
            return []
        return [row["fact"] for row in rows.data or []]

    def get_user_facts_by_id(self, chat_id: int, user_id: int, limit: int = 10) -> list[str]:
        if self._knowledge_entities_supported is False or not user_id:
            return []

        rows = self._safe_execute(
            self._knowledge()
            .select("fact,last_seen_at")
            .eq("chat_id", chat_id)
            .eq("entity_user_id", user_id)
            .eq("status", "confirmed")
            .order("last_seen_at", desc=True)
            .limit(limit),
            fallback=None,
            context=f"get_user_facts_by_id chat_id={chat_id} user_id={user_id}",
        )
        if not rows:
            if self._knowledge_entities_supported is None:
                self._knowledge_entities_supported = False
            return []

        self._knowledge_entities_supported = True
        return [row["fact"] for row in rows.data or []]

    def get_persona_state(self, chat_id: int, user_id: int) -> dict[str, Any] | None:
        response = self._safe_execute(
            self._persona().select("*").eq("chat_id", chat_id).eq("user_id", user_id).limit(1),
            fallback=None,
            context=f"get_persona_state chat_id={chat_id} user_id={user_id}",
        )
        return response.data[0] if response and response.data else None

    def upsert_persona_state(self, chat_id: int, user_id: int, payload: dict[str, Any]) -> None:
        row = {"chat_id": chat_id, "user_id": user_id, **payload, "updated_at": datetime.now(timezone.utc).isoformat()}
        try:
            result = self._safe_execute(
                self._persona().upsert(row),
                fallback=None,
                context=f"upsert_persona_state chat_id={chat_id} user_id={user_id}",
            )
            if result is None:
                return
        except APIError as exc:
            message = str(exc)
            if "respect" not in message:
                print(f"[DB:error] context=upsert_persona_state error={exc}")
                return

            legacy_row = dict(row)
            legacy_row.pop("respect", None)
            self._safe_execute(
                self._persona().upsert(legacy_row),
                fallback=None,
                context=f"upsert_persona_state.legacy chat_id={chat_id} user_id={user_id}",
            )

    def insert_reminder(self, chat_id: int, user_id: int, user_name: str, text: str, trigger_time: datetime) -> Reminder | None:
        result = self._safe_execute(
            self._reminders().insert(
                {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "user_name": user_name,
                    "text": text,
                    "trigger_time": trigger_time.astimezone(timezone.utc).isoformat(),
                    "is_sent": False,
                }
            ),
            fallback=None,
            context=f"insert_reminder chat_id={chat_id} user_id={user_id}",
        )
        if not result or not result.data:
            return None
        return self._reminder_from_row(result.data[0])

    def get_due_reminders(self) -> list[Reminder]:
        now = datetime.now(timezone.utc).isoformat()
        response = self._safe_execute(
            self._reminders().select("*").eq("is_sent", False).lte("trigger_time", now),
            fallback=None,
            context="get_due_reminders",
        )
        if not response:
            return []
        return [self._reminder_from_row(row) for row in response.data or []]

    def mark_reminder_sent(self, reminder_id: int) -> None:
        self._safe_execute(
            self._reminders().update({"is_sent": True}).eq("id", reminder_id),
            fallback=None,
            context=f"mark_reminder_sent id={reminder_id}",
        )

    def set_verification(self, challenge: VerificationChallenge) -> None:
        self.pending_verifications[challenge.user_id] = challenge

    def get_verification(self, user_id: int) -> VerificationChallenge | None:
        return self.pending_verifications.get(user_id)

    def pop_verification(self, user_id: int) -> VerificationChallenge | None:
        return self.pending_verifications.pop(user_id, None)

    # === Feedback (предложения и жалобы) ===

    def create_feedback(self, chat_id: int, user_id: int, user_name: str, category: str, text: str) -> int | None:
        result = self._safe_execute(
            self._feedback().insert(
                {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "user_name": user_name,
                    "category": category,
                    "text": text,
                    "status": "pending",
                }
            ),
            fallback=None,
            context=f"create_feedback chat_id={chat_id}",
        )
        if result and result.data:
            return result.data[0]["id"]
        return None

    def get_user_feedbacks(self, chat_id: int, user_id: int) -> list[dict[str, Any]]:
        response = self._safe_execute(
            self._feedback()
            .select("id,category,text,status,response,created_at")
            .eq("chat_id", chat_id)
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(20),
            fallback=None,
            context=f"get_user_feedbacks chat_id={chat_id} user_id={user_id}",
        )
        if not response or not response.data:
            return []
        return [
            {
                "id": row["id"],
                "category": row["category"],
                "text": row["text"],
                "status": row["status"],
                "response": row.get("response"),
            }
            for row in response.data
        ]

    def cancel_feedback(self, chat_id: int, user_id: int, feedback_id: int) -> bool:
        result = self._safe_execute(
            self._feedback()
            .update({"status": "cancelled"})
            .eq("id", feedback_id)
            .eq("chat_id", chat_id)
            .eq("user_id", user_id)
            .eq("status", "pending"),
            fallback=None,
            context=f"cancel_feedback id={feedback_id}",
        )
        return bool(result and result.data)

    def get_all_feedbacks(self, chat_id: int) -> list[dict[str, Any]]:
        response = self._safe_execute(
            self._feedback()
            .select("id,user_name,category,text,status,response,created_at")
            .eq("chat_id", chat_id)
            .order("created_at", desc=True)
            .limit(50),
            fallback=None,
            context=f"get_all_feedbacks chat_id={chat_id}",
        )
        if not response or not response.data:
            return []
        return response.data

    def delete_feedback(self, chat_id: int, feedback_id: int) -> bool:
        result = self._safe_execute(
            self._feedback().delete().eq("id", feedback_id).eq("chat_id", chat_id),
            fallback=None,
            context=f"delete_feedback id={feedback_id}",
        )
        return bool(result and result.data)

    def add_reputation(self, user: ChatUser, amount: int) -> ChatUser | None:
        """Добавить или отнять репутацию (печеньки)"""
        new_rep = user.reputation + amount
        if new_rep < 0:
            new_rep = 0
        return self.update_user(user.id, {"reputation": new_rep})

    def add_xp(self, user: ChatUser, amount: int) -> dict[str, Any]:
        """Добавить XP и обработать уровень"""
        new_xp = user.xp + amount
        new_level = user.level
        level_up = False
        
        while True:
            next_xp = self.get_next_level_xp(new_level)
            if new_xp >= next_xp:
                new_level += 1
                level_up = True
            else:
                break
                
        updated = self.update_user(user.id, {"xp": new_xp, "level": new_level})
        return {
            "success": bool(updated),
            "new_xp": new_xp,
            "new_level": new_level,
            "level_up": level_up
        }

    def get_user_badges(self, db_id: int) -> list[str]:
        """Возвращает список иконок всех достижений пользователя."""
        response = self._safe_execute(
            self.client.rpc("get_user_badges", {"p_user_id": db_id}),
            fallback=[],
            context=f"get_user_badges db_id={db_id}",
        )
        if hasattr(response, "data") and isinstance(response.data, list):
            return [str(row.get("icon", "")) for row in response.data if row.get("icon")]
        return []

    def award_achievement(self, db_id: int, code: str) -> dict[str, Any] | None:
        """Присваивает достижение пользователю, если его еще нет."""
        # Пытаемся вызвать RPC функцию, которая проверит наличие и добавит
        response = self._safe_execute(
            self.client.rpc("award_achievement_by_code", {"p_user_id": db_id, "p_code": code}),
            fallback=None,
            context=f"award_achievement db_id={db_id} code={code}",
        )
        if response and response.data:
            # Сбрасываем кэш пользователя, так как у него появились новые бейджи
            self._user_cache.clear()
            return response.data # Вернет инфо об ачивке, если она была выдана
        return None

    def check_and_award_achievements(self, user: ChatUser) -> list[dict[str, Any]]:
        """Проверяет условия и выдает новые ачивки."""
        new_awards = []
        
        # 💰 Богатство
        if user.reputation >= 5000:
            res = self.award_achievement(user.id, "rich_2")
            if res: new_awards.append(res)
        elif user.reputation >= 1000:
            res = self.award_achievement(user.id, "rich_1")
            if res: new_awards.append(res)
            
        # 🎰 Казино
        plays = self._get_stat(user.id, "casino_plays")
        if plays >= 200:
            res = self.award_achievement(user.id, "gambler_2")
            if res: new_awards.append(res)
        elif plays >= 50:
            res = self.award_achievement(user.id, "gambler_1")
            if res: new_awards.append(res)

        # ⛏️ Шахта
        mines = self._get_stat(user.id, "mine_plays")
        if mines >= 20:
            res = self.award_achievement(user.id, "miner_1")
            if res: new_awards.append(res)

        # 🗣️ Общение
        msgs = self._get_stat(user.id, "total_messages")
        if msgs >= 2000:
            res = self.award_achievement(user.id, "speaker_2")
            if res: new_awards.append(res)
        elif msgs >= 500:
            res = self.award_achievement(user.id, "speaker_1")
            if res: new_awards.append(res)
            
        # 👣 Первый шаг
        res = self.award_achievement(user.id, "first_step")
        if res: new_awards.append(res)
        
        return new_awards

    def _get_stat(self, db_id: int, column: str) -> int:
        """Вспомогательный метод для получения одного значения статистики."""
        res = self._safe_execute(
            self._users().select(column).eq("id", db_id).limit(1),
            fallback=None
        )
        if res and res.data:
            return int(res.data[0].get(column) or 0)
        return 0

    def increment_stat(self, db_id: int, column: str) -> None:
        """Увеличивает счетчик статистики на 1."""
        self._safe_execute(
            self.client.rpc("increment_user_stat", {"p_user_id": db_id, "p_column": column}),
            fallback=None,
            context=f"increment_stat {column}"
        )
