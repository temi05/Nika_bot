import re
import sqlite3
import sys

def clean_and_normalize_facts(db_path: str) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("SELECT rowid, c0 FROM embedding_fulltext_search_content")
    rows = cur.fetchall()

    ephemeral_keywords = [
        "заснул", "хочет спать", "хочет пасочку", "покрасить яички", 
        "пошел кушать", "отрубилась", "легла", "потянувся", "проспала",
        "недопонимание", "эмоция:", "состояние:", "состояние "
    ]

    name_replacements = {
        r"\bДанила\b": "Danil (@Markvannes)",
        r"\bDanil\b(?!\s*\(@)": "Danil (@Markvannes)",
        r"\bЧика\b(?!\s*\(@)": "Чика (@SCTemi)",
        r"\bsanechk_aaa\b(?!\s*\(@)": "sanechk_aaa (@sanechkkk_aaa)",
        r"\bСанечка\b": "sanechk_aaa (@sanechkkk_aaa)",
        r"\bЛексон⁴²\b(?!\s*\(@)": "Лексон⁴² (@LeksikN6)",
        r"\bЛюбимый\b(?!\s*\(@)": "Любимый (@Lubimbi_director)",
    }

    deleted_count = 0
    updated_count = 0
    seen_facts = set()

    for rowid, text in rows:
        if not text:
            continue

        # 1. Проверка на случайный мыльный шум / сиюминутные состояния
        text_lower = text.casefold()
        if any(keyword in text_lower for keyword in ephemeral_keywords):
            cur.execute("DELETE FROM embedding_fulltext_search_content WHERE rowid=?", (rowid,))
            deleted_count += 1
            continue

        # 2. Очистка устаревших префиксов УЗЕЛ / СВЯЗЬ
        cleaned = text
        cleaned = re.sub(r"^УЗЕЛ:\s*", "", cleaned)
        cleaned = re.sub(r"^СВЯЗЬ:\s*", "", cleaned)
        cleaned = re.sub(r"\s*\|\s*интерес:\s*", " увлекается: ", cleaned)
        cleaned = re.sub(r"\s*\|\s*предпочитает:\s*", " предпочитает ", cleaned)
        cleaned = re.sub(r"\s*\|\s*факт:\s*", " — ", cleaned)
        cleaned = re.sub(r"\s*\|\s*роль:\s*", " имеет роль ", cleaned)
        cleaned = re.sub(r"\s*\|\s*прозвище:\s*", " имеет прозвище ", cleaned)

        # 3. Нормализация имен
        for pattern, replacement in name_replacements.items():
            cleaned = re.sub(pattern, replacement, cleaned)

        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        # 4. Дедупликация
        norm_key = re.sub(r"[^\w]+", "", cleaned.casefold())
        if norm_key in seen_facts:
            cur.execute("DELETE FROM embedding_fulltext_search_content WHERE rowid=?", (rowid,))
            deleted_count += 1
            continue

        seen_facts.add(norm_key)

        if cleaned != text:
            cur.execute("UPDATE embedding_fulltext_search_content SET c0=? WHERE rowid=?", (cleaned, rowid))
            updated_count += 1

    conn.commit()
    conn.close()
    print(f"✅ Очистка завершена! Удалено сиюминутных/дублей: {deleted_count}, обновлено и приведено к норме: {updated_count}")

if __name__ == "__main__":
    clean_and_normalize_facts("chroma_memory_backup/chroma.sqlite3")
