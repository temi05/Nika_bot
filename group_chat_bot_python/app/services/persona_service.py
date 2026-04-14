from __future__ import annotations

from app.services.supabase_db import SupabaseDB


class PersonaService:
    def __init__(self, db: SupabaseDB) -> None:
        self.db = db

    def get_state(self, chat_id: int, user_id: int) -> dict:
        state = self.db.get_persona_state(chat_id, user_id)
        if state:
            return state
        return {
            "troll": 0.42,
            "warmth": 0.58,
            "chaos": 0.32,
            "attachment": 0.18,
            "stage": "fresh",
            "exchanges": 0,
        }

    def bump_exchange(self, chat_id: int, user_id: int) -> dict:
        state = self.get_state(chat_id, user_id)
        state["exchanges"] = int(state.get("exchanges", 0)) + 1
        if state["exchanges"] > 30:
            state["stage"] = "regular"
        self.db.upsert_persona_state(chat_id, user_id, state)
        return state
