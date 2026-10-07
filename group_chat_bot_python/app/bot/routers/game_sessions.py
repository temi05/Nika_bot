"""
Активные сессии чата и экономики — shared между роутерами economy.py, general_admin.py и web.py.
"""
from __future__ import annotations

# Займы и кредиты (/loan, /ask_loan)
LOAN_SESSIONS: dict[str, dict] = {}

# Залоги для выхода из тюрьмы (/bail)
BAIL_SESSIONS: dict[str, dict] = {}

# Авто-события чата (дропы печенек и викторины)
AUTO_DROP_SESSIONS: dict[str, dict] = {}
AUTO_QUIZ_SESSIONS: dict[str, dict] = {}
AUTO_CLAIMED_EVENTS: set[str] = set()
