-- Добавление новых колонок для профиля и ИИ
ALTER TABLE users ADD COLUMN IF NOT EXISTS birthday VARCHAR(5); -- Формат DD-MM
ALTER TABLE users ADD COLUMN IF NOT EXISTS bio VARCHAR(150); -- Краткое био
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_ai_time BIGINT DEFAULT 0; -- Защита от спама ИИ
