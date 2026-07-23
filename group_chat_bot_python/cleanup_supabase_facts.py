import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import Settings
from app.services.supabase_db import SupabaseDB

def clean_fact_text(text: str) -> str:
    if not text:
        return ""
    
    cleaned = text.strip()
    cleaned = re.sub(r"^УЗЕЛ:\s*", "", cleaned)
    cleaned = re.sub(r"^СВЯЗЬ:\s*", "", cleaned)
    cleaned = re.sub(r"\s*\|\s*интерес:\s*", " увлекается: ", cleaned)
    cleaned = re.sub(r"\s*\|\s*предпочитает:\s*", " предпочитает ", cleaned)
    cleaned = re.sub(r"\s*\|\s*факт:\s*", " — ", cleaned)
    cleaned = re.sub(r"\s*\|\s*роль:\s*", " имеет роль ", cleaned)
    cleaned = re.sub(r"\s*\|\s*прозвище:\s*", " имеет прозвище ", cleaned)

    # Имена участников
    name_map = {
        r"\bДанила\b": "Danil (@Markvannes)",
        r"\bDanil\b(?!\s*\(@)": "Danil (@Markvannes)",
        r"\bЧика\b(?!\s*\(@)": "Чика (@SCTemi)",
        r"\bsanechk_aaa\b(?!\s*\(@)": "sanechk_aaa (@sanechkkk_aaa)",
        r"\bСанечка\b": "sanechk_aaa (@sanechkkk_aaa)",
        r"\bЛексон⁴²\b(?!\s*\(@)": "Лексон⁴² (@LeksikN6)",
        r"\bЛюбимый\b(?!\s*\(@)": "Любимый (@Lubimbi_director)",
    }
    for pattern, repl in name_map.items():
        cleaned = re.sub(pattern, repl, cleaned)

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned

def run_supabase_cleanup():
    sys.stdout.reconfigure(encoding="utf-8")
    settings = Settings()
    if not settings.supabase_url or not settings.supabase_key:
        print("❌ Supabase URL / Key не найдены в окружении.")
        return

    db = SupabaseDB(settings)
    print("🔍 Подключение к Supabase memories...")
    
    response = db.client.table("memories").select("*").limit(2000).execute()
    if not response or not response.data:
        print("⚠️ Записи памяти в Supabase не найдены.")
        return

    records = response.data
    print(f"📊 Найдено записей в Supabase: {len(records)}")

    ephemeral_keywords = [
        "заснул", "хочет спать", "хочет пасочку", "покрасить яички", 
        "пошел кушать", "отрубилась", "легла", "потянувся", "проспала",
        "недопонимание", "эмоция:", "состояние:", "состояние "
    ]

    deleted_count = 0
    updated_count = 0
    seen_facts = set()

    for item in records:
        rec_id = item.get("id")
        fact_text = item.get("fact") or ""
        
        # 1. Сиюминутные состояния / эмоции
        fact_lower = fact_text.lower()
        if any(keyword in fact_lower for keyword in ephemeral_keywords):
            db.client.table("memories").delete().eq("id", rec_id).execute()
            deleted_count += 1
            continue

        # 2. Очистка текста
        cleaned = clean_fact_text(fact_text)
        if not cleaned or len(cleaned) < 5:
            db.client.table("memories").delete().eq("id", rec_id).execute()
            deleted_count += 1
            continue

        # 3. Дедупликация
        norm_key = cleaned.lower()
        if norm_key in seen_facts:
            db.client.table("memories").delete().eq("id", rec_id).execute()
            deleted_count += 1
            continue
        seen_facts.add(norm_key)

        # 4. Обновление если изменилось
        if cleaned != fact_text:
            db.client.table("memories").update({"fact": cleaned}).eq("id", rec_id).execute()
            updated_count += 1

    print(f"✅ Очистка Supabase завершена!")
    print(f"   Удалено сиюминутных/дубликатов: {deleted_count}")
    print(f"   Обновлено и приведено к нормальному тексту: {updated_count}")
    print(f"   Осталось чистых уникальных фактов: {len(seen_facts)}")

if __name__ == "__main__":
    run_supabase_cleanup()
