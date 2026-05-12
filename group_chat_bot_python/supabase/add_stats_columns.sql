-- Добавляем колонки для отслеживания статистики достижений
ALTER TABLE users ADD COLUMN IF NOT EXISTS casino_plays INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS mine_plays INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS total_messages INTEGER DEFAULT 0;

-- Индекс для ускорения топов по статистике в будущем
CREATE INDEX IF NOT EXISTS idx_users_stats ON users (casino_plays, mine_plays, total_messages);

-- Функция для инкремента статистики
CREATE OR REPLACE FUNCTION increment_user_stat(p_user_id BIGINT, p_column VARCHAR)
RETURNS VOID AS $$
BEGIN
    EXECUTE format('UPDATE users SET %I = %I + 1 WHERE id = $1', p_column, p_column)
    USING p_user_id;
END;
$$ LANGUAGE plpgsql;
