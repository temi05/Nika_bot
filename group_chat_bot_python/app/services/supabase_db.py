from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from postgrest.exceptions import APIError
from supabase import Client, create_client

from app.config import Settings
from app.models import ChatSettings, ChatUser, MemoryRecord, Reminder, Sender, VerificationChallenge
from app.utils import birthday_is_today, normalize_search_text


class SupabaseDB:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: Client = create_client(settings.supabase_url, settings.supabase_key)
        self.message_authors: dict[int, dict[int, int]] = {}
        self.reaction_cooldowns: dict[str, float] = {}
        self.command_cooldowns: dict[int, dict[str, float]] = {}
        self.pending_verifications: dict[int, VerificationChallenge] = {}
        self.last_birthday_check: dict[int, str] = {}

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
        response = self._safe_execute(
            self._users().select("*").eq("chat_id", chat_id).eq("user_id", sender.user_id).limit(1),
            fallback=None,
            context=f"get_or_create_user.select chat_id={chat_id} user_id={sender.user_id}",
        )
        if response and response.data:
            return self.reset_expired_warns(self._user_from_row(response.data[0]))

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
        }
        created = self._safe_execute(
            self._users().insert(payload),
            fallback=None,
            context=f"get_or_create_user.insert chat_id={chat_id} user_id={sender.user_id}",
        )
        if created and created.data:
            return self._user_from_row(created.data[0])
        return ChatUser(
            id=0,
            chat_id=chat_id,
            user_id=sender.user_id,
            first_name=sender.first_name or "Unknown",
            username=sender.username,
        )

    def get_user_by_platform_id(self, chat_id: int, user_id: int) -> ChatUser | None:
        response = self._safe_execute(
            self._users().select("*").eq("chat_id", chat_id).eq("user_id", user_id).limit(1),
            fallback=None,
            context=f"get_user_by_platform_id chat_id={chat_id} user_id={user_id}",
        )
        if not response or not response.data:
            return None
        return self.reset_expired_warns(self._user_from_row(response.data[0]))

    def update_user(self, db_id: int, updates: dict[str, Any]) -> ChatUser | None:
        payload = dict(updates)
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
        now_ms = int(time.time() * 1000)
        if now_ms - user.last_message_time < 60_000:
            return user, False

        gained = random.randint(15, 25)
        xp = user.xp + gained
        level = user.level
        level_up = False
        while xp >= self.get_next_level_xp(level):
            level += 1
            level_up = True

        updated = self.update_user(
            user.id,
            {"xp": xp, "level": level, "last_message_time": now_ms},
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
        is_rep_gained = random.random() < 0.1
        new_reputation = user.reputation + (1 if is_rep_gained else 0)
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
            "is_rep_gained": is_rep_gained,
            "new_reputation": new_reputation,
            "new_level": updated.level if updated else level,
            "level_up": level_up,
        }

    def get_top_users(self, chat_id: int, limit: int = 10) -> list[ChatUser]:
        response = self._safe_execute(
            self._users().select("*").eq("chat_id", chat_id).order("level", desc=True).order("xp", desc=True).limit(limit),
            fallback=None,
            context=f"get_top_users chat_id={chat_id}",
        )
        if not response:
            return []
        return [self.reset_expired_warns(self._user_from_row(row)) for row in response.data or []]

    def get_all_users(self, chat_id: int) -> list[ChatUser]:
        response = self._safe_execute(
            self._users().select("*").eq("chat_id", chat_id).limit(300),
            fallback=None,
            context=f"get_all_users chat_id={chat_id}",
        )
        if not response:
            return []
        return [self.reset_expired_warns(self._user_from_row(row)) for row in response.data or []]

    def get_chat_settings(self, chat_id: int) -> ChatSettings:
        response = self._safe_execute(
            self._chats().select("*").eq("chat_id", chat_id).limit(1),
            fallback=None,
            context=f"get_chat_settings.select chat_id={chat_id}",
        )
        if response and response.data:
            row = response.data[0]
            return ChatSettings(chat_id=chat_id, link_filter_enabled=bool(row.get("link_filter_enabled", True)))

        created = self._safe_execute(
            self._chats().insert(
                {"chat_id": chat_id, "link_filter_enabled": self.settings.link_filter_default}
            ),
            fallback=None,
            context=f"get_chat_settings.insert chat_id={chat_id}",
        )
        if created and created.data:
            row = created.data[0]
            return ChatSettings(chat_id=chat_id, link_filter_enabled=bool(row.get("link_filter_enabled", True)))

        # Safe fallback when DB is temporarily unavailable.
        return ChatSettings(chat_id=chat_id, link_filter_enabled=self.settings.link_filter_default)

    def update_chat_settings(self, chat_id: int, **updates: Any) -> bool:
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
            ),
        )
        return updated

    def search_user(self, chat_id: int, query: str) -> ChatUser | None:
        normalized = normalize_search_text(query).replace("@", "")
        response = self._safe_execute(
            self._users().select("*").eq("chat_id", chat_id).limit(100),
            fallback=None,
            context=f"search_user chat_id={chat_id}",
        )
        if not response:
            return None
        candidates = response.data or []
        if not normalized:
            return None

        def score(row: dict[str, Any]) -> int:
            fields = [
                normalize_search_text(row.get("first_name")),
                normalize_search_text(row.get("username")),
                normalize_search_text(row.get("bio")),
                normalize_search_text(row.get("ai_notes")),
            ]
            best = 0
            for field in fields:
                if not field:
                    continue
                if field == normalized:
                    best = max(best, 140)
                elif field.startswith(normalized):
                    best = max(best, 120)
                elif normalized in field:
                    best = max(best, 100)
            return best

        ranked = sorted(((score(row), row) for row in candidates), key=lambda item: item[0], reverse=True)
        if not ranked or ranked[0][0] < 100:
            return None
        return self.reset_expired_warns(self._user_from_row(ranked[0][1]))

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

    def purchase_item(self, user: ChatUser, item_id: int) -> tuple[bool, str]:
        if item_id == 1:
            cost = 500
            if user.reputation < cost:
                return False, f"Недостаточно печенек. Нужно {cost} 🍪."
            self.update_user(user.id, {"level": user.level + 1, "xp": 0, "reputation": user.reputation - cost})
            return True, f"Уровень куплен. Теперь у тебя {user.level + 1} уровень."
        if item_id == 2:
            cost = 200
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

    def store_memory(self, chat_id: int, memory: MemoryRecord) -> None:
        self._safe_execute(
            self._knowledge().insert(
                {
                    "chat_id": chat_id,
                    "fact": memory.fact,
                    "fact_type": memory.source,
                    "confidence": memory.confidence,
                    "status": "confirmed",
                    "meta": memory.meta or {},
                    "last_seen_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
            fallback=None,
            context=f"store_memory chat_id={chat_id}",
        )

    def memory_exists(self, chat_id: int, fact: str) -> bool:
        rows = self._safe_execute(
            self._knowledge().select("id").eq("chat_id", chat_id).eq("fact", fact).limit(1),
            fallback=None,
            context=f"memory_exists chat_id={chat_id}",
        )
        return bool(rows and rows.data)

    def search_memory(self, chat_id: int, query: str, limit: int = 5) -> list[str]:
        if not query.strip():
            return []
        rows = self._safe_execute(
            self._knowledge()
            .select("fact,last_seen_at")
            .eq("chat_id", chat_id)
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
            self._knowledge().select("fact,last_seen_at").eq("chat_id", chat_id).order("last_seen_at", desc=True).limit(limit),
            fallback=None,
            context=f"get_recent_memories chat_id={chat_id}",
        )
        if not rows:
            return []
        return [row["fact"] for row in rows.data or []]

    def get_all_user_facts(self, chat_id: int, user_name: str, limit: int = 10) -> list[str]:
        rows = self._safe_execute(
            self._knowledge().select("fact").eq("chat_id", chat_id).ilike("fact", f"%{user_name}%").limit(limit),
            fallback=None,
            context=f"get_all_user_facts chat_id={chat_id}",
        )
        if not rows:
            return []
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

    def add_reputation(self, user: ChatUser, amount: int) -> ChatUser | None:
        """Добавить или отнять репутацию (печеньки)"""
        new_rep = user.reputation + amount
        if new_rep < 0:
            new_rep = 0
        return self.update_user(user.id, {"reputation": new_rep})
