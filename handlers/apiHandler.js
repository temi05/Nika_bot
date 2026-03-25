const express = require('express');
const { supabase } = require('../database');
const { bot, token, isAdmin } = require('../utils');
const AternosAPI = require('./aternosHandler');
const { aternos_session } = require('../config');
const crypto = require('crypto');

const router = express.Router();

// Middleware для валидации initData от Telegram
function validateTelegramWebAppData(req, res, next) {
    const initData = req.headers['x-tg-init-data'];
    if (!initData) {
        return res.status(401).json({ error: 'No init data' });
    }

    try {
        const urlParams = new URLSearchParams(initData);
        const hash = urlParams.get('hash');
        urlParams.delete('hash');
        
        urlParams.sort();
        let dataCheckString = '';
        for (const [key, value] of urlParams.entries()) {
            dataCheckString += `${key}=${value}\n`;
        }
        dataCheckString = dataCheckString.slice(0, -1);

        const secretKey = crypto.createHmac('sha256', 'WebAppData').update(token).digest();
        const calculatedHash = crypto.createHmac('sha256', secretKey).update(dataCheckString).digest('hex');

        if (calculatedHash === hash) {
            req.tgUser = JSON.parse(urlParams.get('user'));
            req.chatId = urlParams.get('chat_instance'); // Не всегда точный chatId, лучше передавать явно или через БД
            next();
        } else {
            res.status(401).json({ error: 'Invalid hash' });
        }
    } catch (e) {
        res.status(401).json({ error: 'Validation failed' });
    }
}

// Получение профиля пользователя
router.get('/profile', validateTelegramWebAppData, async (req, res) => {
    try {
        const userId = req.tgUser.id;
        // Мы берем данные из БД пользователя. У нас может быть один и тот же user_id в разных чатах.
        // Для dashboard мы можем либо взять статистику из конкретного чата (переданного в query), либо общую.
        // Пока берем первую попавшуюся запись
        
        const chatId = req.query.chat_id;
        let query = supabase.from('users').select('*').eq('user_id', userId);
        if (chatId) query = query.eq('chat_id', chatId);
        
        const { data, error } = await query.limit(1).maybeSingle();

        if (error || !data) {
            return res.json({ level: 1, xp: 0, reputation: 0, warns: 0, is_admin: false });
        }
        
        // Проверяем, админ ли он в этом чате
        let is_admin = false;
        if (chatId) {
            is_admin = await isAdmin(chatId, userId);
        }
        
        res.json({ ...data, is_admin });
    } catch (e) {
        res.status(500).json({ error: 'Server error' });
    }
});

// Получение лидерборда
router.get('/leaderboard', validateTelegramWebAppData, async (req, res) => {
    try {
        const chatId = req.query.chat_id;
        const type = req.query.type || 'level'; // 'level' или 'reputation'
        
        if (!chatId) return res.status(400).json({ error: 'chat_id required' });

        const { data, error } = await supabase
            .from('users')
            .select('user_id, username, first_name, level, reputation')
            .eq('chat_id', chatId)
            .order(type === 'reputation' ? 'reputation' : 'level', { ascending: false })
            .limit(10);
            
        if (error) throw error;
        res.json(data);
    } catch (e) {
        res.status(500).json({ error: 'Server error' });
    }
});

// Получение списка плохих слов (только для админов)
router.get('/badwords', validateTelegramWebAppData, async (req, res) => {
    try {
        const chatId = req.query.chat_id;
        const userId = req.tgUser.id;
        if (!chatId || !(await isAdmin(chatId, userId))) return res.status(403).json({ error: 'Access denied' });

        const { data, error } = await supabase.from('bad_words').select('word').eq('chat_id', chatId);
        if (error) throw error;
        res.json(data.map(i => i.word));
    } catch (e) {
        res.status(500).json({ error: 'Server error' });
    }
});

// Добавление/Удаление плохих слов
router.post('/badwords', validateTelegramWebAppData, async (req, res) => {
    try {
        const { chat_id, word, action } = req.body;
        const userId = req.tgUser.id;
        
        if (!chat_id || !word || !(await isAdmin(chat_id, userId))) {
            return res.status(403).json({ error: 'Access denied' });
        }

        if (action === 'add') {
            await supabase.from('bad_words').insert([{ chat_id: chat_id, word: word.toLowerCase() }]);
        } else if (action === 'remove') {
            await supabase.from('bad_words').delete().eq('chat_id', chat_id).eq('word', word.toLowerCase());
        }
        
        res.json({ success: true });
    } catch (e) {
        console.error(e);
        res.status(500).json({ error: 'Server error' });
    }
});

// --- ATERNOS API ---
const aternos = new AternosAPI(aternos_session);

router.get('/aternos/status', validateTelegramWebAppData, async (req, res) => {
    try {
        const status = await aternos.getStatus();
        res.json(status);
    } catch (e) {
        res.status(500).json({ error: 'Aternos error' });
    }
});

router.post('/aternos/start', validateTelegramWebAppData, async (req, res) => {
    try {
        const success = await aternos.startServer();
        res.json({ success });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

module.exports = router;
