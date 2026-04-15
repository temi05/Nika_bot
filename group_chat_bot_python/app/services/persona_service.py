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
            "chaos": 0.30,
            "attachment": 0.18,
            "respect": 0.50,
            "stage": "fresh",
            "exchanges": 0,
        }

    def observe_user_message(
        self,
        chat_id: int,
        user_id: int,
        text: str,
        *,
        reply_to_bot: bool,
        mentioned: bool,
    ) -> dict:
        state = self.get_state(chat_id, user_id)
        lowered = text.lower()

        if reply_to_bot or mentioned:
            state["attachment"] = self._clamp(state.get("attachment", 0.2) + 0.015)

        if any(token in lowered for token in ["спасибо", "люблю", "солныш", "умница", "красотка", "обожаю"]):
            state["warmth"] = self._clamp(state.get("warmth", 0.6) + 0.05)
            state["attachment"] = self._clamp(state.get("attachment", 0.2) + 0.03)
            state["troll"] = self._clamp(state.get("troll", 0.4) - 0.02)

        if any(token in lowered for token in ["дура", "тупая", "идиот", "заткнись", "нахуй", "пошла"]):
            state["troll"] = self._clamp(state.get("troll", 0.4) + 0.06)
            state["chaos"] = self._clamp(state.get("chaos", 0.3) + 0.04)
            state["warmth"] = self._clamp(state.get("warmth", 0.6) - 0.05)
            state["respect"] = self._clamp(state.get("respect", 0.5) - 0.06)
            state["stage"] = "volatile"

        self._refresh_stage(state)
        self.db.upsert_persona_state(chat_id, user_id, state)
        return state

    def bump_exchange(self, chat_id: int, user_id: int) -> dict:
        state = self.get_state(chat_id, user_id)
        state["exchanges"] = int(state.get("exchanges", 0)) + 1

        if state["exchanges"] % 6 == 0:
            state["attachment"] = self._clamp(state.get("attachment", 0.2) + 0.02)
        if state["exchanges"] % 10 == 0:
            state["warmth"] = self._clamp(state.get("warmth", 0.6) + 0.01)

        self._refresh_stage(state)
        self.db.upsert_persona_state(chat_id, user_id, state)
        return state

    def _refresh_stage(self, state: dict) -> None:
        if state.get("stage") == "volatile" and state.get("warmth", 0.5) > 0.55:
            state["stage"] = "regular"

        exchanges = int(state.get("exchanges", 0))
        attachment = float(state.get("attachment", 0.2))
        warmth = float(state.get("warmth", 0.6))

        if state.get("stage") == "volatile":
            return
        if exchanges >= 60 or attachment >= 0.72:
            state["stage"] = "inner_circle"
        elif exchanges >= 24 or attachment >= 0.45:
            state["stage"] = "regular"
        elif exchanges >= 8 or warmth >= 0.62:
            state["stage"] = "familiar"
        else:
            state["stage"] = "fresh"

    def _clamp(self, value: float, lower: float = 0.0, upper: float = 1.0) -> float:
        return max(lower, min(upper, value))
