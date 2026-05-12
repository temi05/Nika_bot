-- Таблица описаний достижений
CREATE TABLE IF NOT EXISTS bot_achievements (
    id SERIAL PRIMARY KEY,
    code VARCHAR(50) UNIQUE NOT NULL, -- Уникальный код (например, 'rich_1')
    title VARCHAR(100) NOT NULL,      -- Название ('Миллионер')
    description TEXT,                 -- Описание ('Накопить 5000 печенек')
    icon VARCHAR(10) NOT NULL,        -- Эмодзи бейджа ('💰')
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Таблица полученных достижений пользователями
CREATE TABLE IF NOT EXISTS user_achievements (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,          -- Внутренний ID пользователя из таблицы users
    achievement_id INTEGER REFERENCES bot_achievements(id) ON DELETE CASCADE,
    earned_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(user_id, achievement_id)
);

-- Начальный набор достижений
INSERT INTO bot_achievements (code, title, description, icon) VALUES
('first_step', 'Первый шаг', 'Написать первое сообщение Нике', '👣'),
('rich_1', 'Зажиточный', 'Накопить 1000 печенек', '🍪'),
('rich_2', 'Миллионер', 'Накопить 5000 печенек', '💰'),
('gambler_1', 'Игрок', 'Сыграть 50 раз в казино', '🎲'),
('gambler_2', 'Лудоман', 'Сыграть 200 раз в казино', '🎰'),
('miner_1', 'Копатель', 'Сходить в шахту 20 раз', '⛏️'),
('speaker_1', 'Собеседник', 'Написать 500 сообщений', '🗣️'),
('speaker_2', 'Душа компании', 'Написать 2000 сообщений', '🌟')
ON CONFLICT (code) DO NOTHING;

-- Функция получения бейджей пользователя
CREATE OR REPLACE FUNCTION get_user_badges(p_user_id BIGINT)
RETURNS TABLE(icon VARCHAR) AS $$
BEGIN
    RETURN QUERY
    SELECT a.icon
    FROM bot_achievements a
    JOIN user_achievements ua ON a.id = achievement_id
    WHERE ua.user_id = p_user_id
    ORDER BY ua.earned_at DESC;
END;
$$ LANGUAGE plpgsql;

-- Функция выдачи ачивки с проверкой на дубликаты
CREATE OR REPLACE FUNCTION award_achievement_by_code(p_user_id BIGINT, p_code VARCHAR)
RETURNS JSONB AS $$
DECLARE
    v_achievement_id INTEGER;
    v_achievement_title VARCHAR;
    v_achievement_icon VARCHAR;
BEGIN
    SELECT id, title, icon INTO v_achievement_id, v_achievement_title, v_achievement_icon
    FROM bot_achievements
    WHERE code = p_code;

    IF v_achievement_id IS NULL THEN
        RETURN NULL;
    END IF;

    -- Проверяем, есть ли уже ачивка
    IF EXISTS (SELECT 1 FROM user_achievements WHERE user_id = p_user_id AND achievement_id = v_achievement_id) THEN
        RETURN NULL;
    END IF;

    -- Добавляем
    INSERT INTO user_achievements (user_id, achievement_id)
    VALUES (p_user_id, v_achievement_id);

    RETURN jsonb_build_object(
        'code', p_code,
        'title', v_achievement_title,
        'icon', v_achievement_icon
    );
END;
$$ LANGUAGE plpgsql;
