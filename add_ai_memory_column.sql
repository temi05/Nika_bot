-- Добавляем колонку для хранения сжатой памяти ИИ
ALTER TABLE chats ADD COLUMN IF NOT EXISTS ai_memory TEXT DEFAULT '';
