"""
Глобальные состояния игровых сессий — shared между роутерами games.py и general_admin.py.

Все словари объявлены здесь и импортируются в обоих модулях,
чтобы избежать NameError от разорванных замыканий после рефакторинга.
"""
from __future__ import annotations

# Дубль-ставки после победы в /dice и /casino
CASINO_DOUBLE_SESSIONS: dict[str, dict] = {}

# Сессии Башни Фортуны (/tower)
TOWER_SESSIONS: dict[str, dict] = {}

# Анти-спам локи для колбеков Башни
TOWER_LOCKS: dict[str, float] = {}

# Дуэли КНБ (/duel)
DUEL_SESSIONS: dict[str, dict] = {}

# Кубодуэли (/diceduel)
DICE_DUEL_SESSIONS: dict[str, dict] = {}

# Займы и кредиты (/loan, /ask_loan)
LOAN_SESSIONS: dict[str, dict] = {}

# Залоги для выхода из тюрьмы (/bail)
BAIL_SESSIONS: dict[str, dict] = {}

# Авто-события чата
AUTO_DROP_SESSIONS: dict[str, dict] = {}
AUTO_QUIZ_SESSIONS: dict[str, dict] = {}
AUTO_CLAIMED_EVENTS: set[str] = set()
