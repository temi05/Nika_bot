-- Таблица для обратной связи (предложения и жалобы)
CREATE TABLE IF NOT EXISTS bot_feedback (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    user_name TEXT,
    category TEXT NOT NULL, -- suggestion, bug, complaint, feature, other
    text TEXT NOT NULL,
    status TEXT DEFAULT 'pending', -- pending, resolved, cancelled
    response TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Индекс для быстрого поиска обращений пользователя
CREATE INDEX IF NOT EXISTS idx_feedback_user ON bot_feedback(chat_id, user_id);

-- Индекс для поиска по статусу
CREATE INDEX IF NOT EXISTS idx_feedback_status ON bot_feedback(status);