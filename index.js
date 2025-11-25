require('dotenv').config();
const TelegramBot = require('node-telegram-bot-api');
const { createClient } = require('@supabase/supabase-js');
const express = require('express');

// Проверяем наличие токенов
const token = process.env.TELEGRAM_BOT_TOKEN;
const supabaseUrl = process.env.SUPABASE_URL;
const supabaseKey = process.env.SUPABASE_KEY;

if (!token || !supabaseUrl || !supabaseKey) {
    console.error('Ошибка: Не заданы переменные окружения (TELEGRAM_BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY)');
    process.exit(1);
}

// Инициализация Supabase
const supabase = createClient(supabaseUrl, supabaseKey);

// Создаем бота (без Polling)
const bot = new TelegramBot(token);

// --- НАСТРОЙКА ДЛЯ ХОСТИНГА (WEBHOOK) ---
const app = express();
app.use(express.json()); // Важно для обработки обновлений от Telegram

const PORT = process.env.PORT || 3000;
const RENDER_EXTERNAL_URL = process.env.RENDER_EXTERNAL_URL; // Render сам дает эту переменную

// Главная страница (для проверки)
app.get('/', (req, res) => {
    res.send('Бот работает! 🤖 (Webhook Mode)');
});

// Роут для приема сообщений от Telegram
app.post(`/bot${token}`, (req, res) => {
    // console.log('[DEBUG] Webhook request received:', JSON.stringify(req.body).substring(0, 100) + '...');

    // Ручная обработка реакций (если библиотека не поддерживает)
    if (req.body.message_reaction) {
        handleReaction(req.body.message_reaction);
    }

    bot.processUpdate(req.body);
    res.sendStatus(200);
});

app.listen(PORT, async () => {
    console.log(`Веб-сервер запущен на порту ${PORT}`);
    console.log(`Ожидаю запросы на: /bot${token.substring(0, 10)}...`);

    // Устанавливаем вебхук, если мы на Render
    if (RENDER_EXTERNAL_URL) {
        const webhookUrl = `${RENDER_EXTERNAL_URL}/bot${token}`;
        console.log(`Ставим вебхук: ${webhookUrl}`);
        try {
            // Явно указываем allowed_updates, чтобы Telegram присылал реакции
            await bot.setWebHook(webhookUrl, {
                allowed_updates: ['message', 'message_reaction', 'chat_member', 'callback_query']
            });
            console.log('✅ Вебхук успешно установлен (с реакциями)!');
        } catch (err) {
            console.error('❌ Ошибка установки вебхука:', err.message);
        }
    } else {
        console.log('Мы не на Render (нет RENDER_EXTERNAL_URL), вебхук не ставим.');
    }
});

// Временное хранилище авторов сообщений (для реакций)
// Очищается при перезапуске, но это не критично
const messageAuthors = {};

// Хранилище таймеров верификации
const pendingVerifications = {};

// --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ БД ---

async function getUser(chatId, userId, userInfo = {}) {
    // Пытаемся найти пользователя
    let { data: user, error } = await supabase
        .from('users')
        .select('*')
        .eq('chat_id', chatId)
        .eq('user_id', userId)
        .single();

    if (error && error.code !== 'PGRST116') { // PGRST116 = не найдено
        console.error('Ошибка получения пользователя:', error.message);
        return null;
    }

    // Если нет - создаем
    if (!user) {
        console.log(`[DEBUG] Creating new user: ${userId}`);
        const newUser = {
            chat_id: chatId,
            user_id: userId,
            username: userInfo.username || '',
            first_name: userInfo.first_name || '',
            xp: 0,
            level: 1,
            reputation: 0,
            warns: 0,
            last_message_time: 0
        };
        const { data, error: createError } = await supabase
            .from('users')
            .insert([newUser])
            .select()
            .single();

        if (createError) {
            console.error('Ошибка создания пользователя:', createError.message);
            return null;
        }
        return data;
    }

    return user;
}

async function updateUser(id, updates) {
    const { error } = await supabase
        .from('users')
        .update(updates)
        .eq('id', id);
    if (error) console.error('Ошибка обновления пользователя:', error);
}

async function getBadWords(chatId) {
    const { data, error } = await supabase
        .from('bad_words')
        .select('word')
        .eq('chat_id', chatId);

    if (error) {
        console.error('Ошибка получения плохих слов:', error);
        return [];
    }
    return data.map(item => item.word);
}

// --- ЛОГИКА БОТА ---

function getNextLevelXp(level) {
    return 50 * level * level + 50 * level;
}

function getUserName(user) {
    return user.username ? `@${user.username}` : user.first_name;
}

function sendTimedMessage(chatId, text, delay = 15000, options = {}) {
    bot.sendMessage(chatId, text, options).then(sentMsg => {
        setTimeout(() => {
            bot.deleteMessage(chatId, sentMsg.message_id).catch(() => { });
        }, delay);
    });
}

async function isAdmin(chatId, userId) {
    try {
        const member = await bot.getChatMember(chatId, userId);
        return ['creator', 'administrator'].includes(member.status);
    } catch (e) {
        return false;
    }
}

// Верификация новых участников
bot.on('new_chat_members', async (msg) => {
    const chatId = msg.chat.id;
    const newMembers = msg.new_chat_members;

    for (const member of newMembers) {
        if (member.is_bot) continue;

        const name = member.username ? `@${member.username}` : member.first_name;

        try {
            await bot.restrictChatMember(chatId, member.id, {
                can_send_messages: false
            });
        } catch (err) {
            sendTimedMessage(chatId, `👋 Привет, ${name}! (Дайте боту права админа для верификации)`);
            continue;
        }

        const opts = {
            reply_markup: {
                inline_keyboard: [[{ text: '✅ Я человек', callback_data: `verify_${member.id}` }]]
            }
        };

        bot.sendMessage(chatId,
            `👋 Привет, ${name}! Добро пожаловать в чат!\nПросим пройти небольшую проверку перед тем, как начать общение.\n(У вас есть 2 минуты)`,
            opts
        ).then(sentMsg => {
            const timeoutId = setTimeout(async () => {
                try {
                    await bot.banChatMember(chatId, member.id);
                    await bot.unbanChatMember(chatId, member.id);
                    bot.deleteMessage(chatId, sentMsg.message_id).catch(() => { });
                    sendTimedMessage(chatId, `🚪 ${name} не прошел проверку и был исключен.`);
                } catch (err) {
                    console.error(`Не удалось кикнуть ${name}:`, err.message);
                }
                delete pendingVerifications[member.id];
            }, 120000);

            pendingVerifications[member.id] = timeoutId;
        });
    }
});

// Callback Query (Кнопка верификации)
bot.on('callback_query', async (query) => {
    const chatId = query.message.chat.id;
    const userId = query.from.id;
    const data = query.data;

    if (data.startsWith('verify_')) {
        const targetId = parseInt(data.split('_')[1]);

        if (userId !== targetId) {
            bot.answerCallbackQuery(query.id, { text: 'Это кнопка не для тебя! 🚫', show_alert: true });
            return;
        }

        if (pendingVerifications[userId]) {
            clearTimeout(pendingVerifications[userId]);
            delete pendingVerifications[userId];
        }

        try {
            await bot.restrictChatMember(chatId, userId, {
                can_send_messages: true,
                can_send_media_messages: true,
                can_send_polls: true,
                can_send_other_messages: true,
                can_add_web_page_previews: true,
                can_invite_users: true
            });

            bot.deleteMessage(chatId, query.message.message_id).catch(() => { });
            sendTimedMessage(chatId, `✅ ${query.from.first_name} успешно прошел проверку!`, 10000);

            const stickerUrl = 'CAACAgQAAxkBAAMFaSLQBhQRZU1oViIWgRlxI2j8G6oAAuUdAALyBzBQW_Qa2_ysFF82BA';
            bot.sendSticker(chatId, stickerUrl).then(sentMsg => {
                setTimeout(() => {
                    bot.deleteMessage(chatId, sentMsg.message_id).catch(() => { });
                }, 30000);
            });

        } catch (err) {
            bot.answerCallbackQuery(query.id, { text: 'Ошибка! Убедитесь, что бот — админ.', show_alert: true });
        }
    }
});

// --- ОБРАБОТКА СООБЩЕНИЙ (XP, Репутация, Фильтр) ---
bot.on('message', async (msg) => {
    const chatId = msg.chat.id;
    const userId = msg.from.id;
    const user = msg.from;

    // Логируем сообщение для реакций (Persistent Reputation)
    if (msg.message_id) {
        // Кэш для быстрого доступа
        if (!messageAuthors[chatId]) messageAuthors[chatId] = {};
        messageAuthors[chatId][msg.message_id] = userId;

        // БД для надежности
        await supabase.from('message_logs').insert([{
            chat_id: chatId,
            message_id: msg.message_id,
            user_id: userId
        }]).then(({ error }) => {
            if (error) console.error('Ошибка лога сообщения:', error.message);
            else console.log(`[DEBUG] Message ${msg.message_id} saved to DB`);
        });
    }

    // --- ФИЛЬТР ---
    if (msg.text) {
        const text = msg.text.toLowerCase();
        const badWords = await getBadWords(chatId);
        const isPromo = text.includes('t.me/') || text.includes('telegram.me/');

        const foundBadWord = badWords.find(word => {
            const escapedWord = word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
            const regex = new RegExp(`(^|\\s|[.,!?;:()"])${escapedWord}($|\\s|[.,!?;:()"])`, 'i');
            return regex.test(text);
        });

        if (foundBadWord || isPromo) {
            bot.deleteMessage(chatId, msg.message_id).catch(() => { });

            const dbUser = await getUser(chatId, userId, user);
            if (dbUser) {
                const newWarns = dbUser.warns + 1;
                await updateUser(dbUser.id, { warns: newWarns });

                if (newWarns >= 3) {
                    await updateUser(dbUser.id, { warns: 0 });
                    const untilDate = Math.floor(Date.now() / 1000) + 3600;
                    bot.restrictChatMember(chatId, userId, {
                        until_date: untilDate,
                        can_send_messages: false
                    }).then(() => {
                        bot.sendMessage(chatId, `⛔ ${getUserName(user)} получил мут на 1 час.`);
                    }).catch(() => {
                        sendTimedMessage(chatId, `⚠️ ${getUserName(user)} нарушает, но я не могу дать мут!`);
                    });
                } else {
                    sendTimedMessage(chatId, `⚠️ ${getUserName(user)}, нарушение! Предупреждение ${newWarns}/3.`, 15000);
                }
            }
            return;
        }
    }

    // --- XP SYSTEM ---
    // Проверяем текст ИЛИ медиа
    const isMedia = msg.photo || msg.voice || msg.video_note || msg.sticker || msg.animation || msg.document;

    if ((msg.text || isMedia) && !msg.text?.startsWith('/')) {
        const dbUser = await getUser(chatId, userId, user);
        if (dbUser) {
            const currentTime = Date.now();
            if (currentTime - dbUser.last_message_time >= 60000) {
                const xpGain = Math.floor(Math.random() * 11) + 15;
                const newXp = dbUser.xp + xpGain;
                const nextLevelXp = getNextLevelXp(dbUser.level);

                let newLevel = dbUser.level;
                let message = null;

                if (newXp >= nextLevelXp) {
                    newLevel += 1;
                    message = `🎉 Поздравляем, ${getUserName(user)}! Ты достиг уровня ${newLevel}!`;
                }

                await updateUser(dbUser.id, {
                    xp: newXp,
                    level: newLevel,
                    last_message_time: currentTime
                });

                console.log(`[XP] ${getUserName(user)}: +${xpGain} XP`);
                if (message) sendTimedMessage(chatId, message, 30000);
            }
        }
    }

    // --- РЕПУТАЦИЯ (ОТВЕТЫ) ---
    if (msg.reply_to_message && msg.text) {
        const receiverId = msg.reply_to_message.from.id;
        if (userId !== receiverId && !msg.reply_to_message.from.is_bot) {
            const reputationTriggers = ['+', 'спасибо', 'спс', 'thx', 'благодарю', '👍', '🔥', '❤️', 'top'];
            const text = msg.text.toLowerCase();

            if (reputationTriggers.some(trigger => text.includes(trigger))) {
                const receiver = await getUser(chatId, receiverId, msg.reply_to_message.from);
                if (receiver) {
                    await updateUser(receiver.id, { reputation: receiver.reputation + 1 });
                    const senderName = getUserName(user);
                    const receiverName = getUserName(msg.reply_to_message.from);
                    sendTimedMessage(chatId, `🌟 ${senderName} повысил репутацию ${receiverName}! (+1)`);
                }
            }
        }
    }
});

// --- РЕПУТАЦИЯ (РЕАКЦИИ) ---
async function handleReaction(reaction) {
    console.log('[DEBUG] Reaction received:', JSON.stringify(reaction));

    const chatId = reaction.chat.id;
    const messageId = reaction.message_id;
    const userWhoReacted = reaction.user; // Тот, кто поставил лайк

    // Игнорируем снятие реакции (old_reaction есть, new_reaction пусто)
    if (reaction.new_reaction.length === 0) return;

    // 1. Ищем автора сообщения (кому поставили лайк)
    let authorId = null;

    // Сначала ищем в кэше
    if (messageAuthors[chatId] && messageAuthors[chatId][messageId]) {
        authorId = messageAuthors[chatId][messageId];
        console.log(`[DEBUG] Author found in cache: ${authorId}`);
    }
    // Если нет в кэше - ищем в БД
    else {
        console.log(`[DEBUG] Author not in cache, checking DB for msg ${messageId}...`);
        const { data, error } = await supabase
            .from('message_logs')
            .select('user_id')
            .eq('chat_id', chatId)
            .eq('message_id', messageId)
            .single();

        if (data) {
            authorId = data.user_id;
            console.log(`[DEBUG] Author found in DB: ${authorId}`);
            // Обновляем кэш
            if (!messageAuthors[chatId]) messageAuthors[chatId] = {};
            messageAuthors[chatId][messageId] = authorId;
        } else {
            console.log(`[DEBUG] Author not found in DB. Error: ${error?.message}`);
        }
    }

    if (!authorId) {
        console.log('[DEBUG] Could not determine message author. Ignoring.');
        return;
    }

    // Нельзя лайкать самого себя
    if (userWhoReacted.id === authorId) {
        console.log('[DEBUG] User liked themselves. Ignoring.');
        return;
    }

    // Начисляем репутацию автору сообщения
    const author = await getUser(chatId, authorId);
    if (author) {
        await updateUser(author.id, { reputation: author.reputation + 1 });
        console.log(`[REP] User ${authorId} reputation +1 (Reaction)`);
        // Не пишем сообщение в чат, чтобы не спамить
    }
}
// Подстраховка: если библиотека все же решит сама вызвать событие
bot.on('message_reaction', handleReaction);

// Хранилище кулдаунов команд
const commandCooldowns = {};
const COMMAND_COOLDOWN_TIME = 120000; // 2 минуты

// Вспомогательная функция для удаления сообщения
function deleteMsg(chatId, msgId, delay = 60000) {
    setTimeout(() => {
        bot.deleteMessage(chatId, msgId).catch(() => { });
    }, delay);
}

// --- КОМАНДЫ ---

// Команда /help
bot.onText(/\/help/, async (msg) => {
    const chatId = msg.chat.id;
    const userId = msg.from.id;

    // Удаляем команду пользователя через 1 минуту
    deleteMsg(chatId, msg.message_id);

    // Проверка кулдауна (для всех, кроме админов)
    if (!(await isAdmin(chatId, userId))) {
        const lastTime = commandCooldowns[userId] || 0;
        if (Date.now() - lastTime < COMMAND_COOLDOWN_TIME) {
            const remaining = Math.ceil((COMMAND_COOLDOWN_TIME - (Date.now() - lastTime)) / 60000);
            sendTimedMessage(chatId, `⏳ ${getUserName(msg.from)}, подожди ${remaining} мин. перед следующей командой!`, 60000);
            return;
        }
        commandCooldowns[userId] = Date.now();
    }

    const helpText = `🤖 *Что я умею:*

👤 *Для всех:*
/me — Посмотреть свою статистику (уровень, опыт, репутация)
/top — Топ-10 активных участников
/kto <вопрос> — Выбрать случайного участника (например: /kto кто сегодня платит?)
👍 *Репутация:* Ставь реакции (🔥, 👍, ❤️) или отвечай "спасибо", чтобы повысить репутацию другим!

👮‍♂️ *Для админов:*
/banword <слово> — Запретить слово
/unbanword <слово> — Разрешить слово
/listwords — Список запрещенных слов

_Я также защищаю чат от спама и проверяю новичков!_`;

    sendTimedMessage(chatId, helpText, 60000, { parse_mode: 'Markdown' });
});

bot.onText(/\/banword (.+)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const userId = msg.from.id;

    deleteMsg(chatId, msg.message_id);

    if (!(await isAdmin(chatId, userId))) {
        sendTimedMessage(chatId, '⛔ У тебя нет прав админа для этой команды!', 60000);
        return;
    }

    const word = match[1].trim();
    const { data } = await supabase.from('bad_words').select('*').eq('chat_id', chatId).eq('word', word).single();

    if (!data) {
        await supabase.from('bad_words').insert([{ chat_id: chatId, word: word }]);
        sendTimedMessage(chatId, `✅ Слово "${word}" добавлено в бан.`, 60000);
    } else {
        sendTimedMessage(chatId, `ℹ️ Слово "${word}" уже в списке.`, 60000);
    }
});

bot.onText(/\/unbanword (.+)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const userId = msg.from.id;

    deleteMsg(chatId, msg.message_id);

    if (!(await isAdmin(chatId, userId))) {
        sendTimedMessage(chatId, '⛔ У тебя нет прав админа для этой команды!', 60000);
        return;
    }

    const word = match[1].trim();
    const { error } = await supabase.from('bad_words').delete().eq('chat_id', chatId).eq('word', word);

    if (!error) {
        sendTimedMessage(chatId, `✅ Слово "${word}" удалено из бана.`, 60000);
    } else {
        sendTimedMessage(chatId, `ℹ️ Ошибка или слово не найдено.`, 60000);
    }
});

bot.onText(/\/listwords/, async (msg) => {
    const chatId = msg.chat.id;
    const userId = msg.from.id;

    deleteMsg(chatId, msg.message_id);

    if (!(await isAdmin(chatId, userId))) {
        sendTimedMessage(chatId, '⛔ У тебя нет прав админа для этой команды!', 60000);
        return;
    }

    const words = await getBadWords(chatId);
    if (words.length === 0) {
        sendTimedMessage(chatId, 'Список пуст.', 60000);
    } else {
        sendTimedMessage(chatId, `🚫 Запрещенные слова:\n${words.join(', ')}`, 60000);
    }
});

bot.onText(/\/me/, async (msg) => {
    const chatId = msg.chat.id;
    const userId = msg.from.id;

    deleteMsg(chatId, msg.message_id);

    // Проверка кулдауна
    if (!(await isAdmin(chatId, userId))) {
        const lastTime = commandCooldowns[userId] || 0;
        if (Date.now() - lastTime < COMMAND_COOLDOWN_TIME) {
            const remaining = Math.ceil((COMMAND_COOLDOWN_TIME - (Date.now() - lastTime)) / 60000);
            sendTimedMessage(chatId, `⏳ ${getUserName(msg.from)}, подожди ${remaining} мин. перед следующей командой!`, 60000);
            return;
        }
        commandCooldowns[userId] = Date.now();
    }

    const user = await getUser(chatId, userId, msg.from);

    if (!user) return;

    const nextLevelXp = getNextLevelXp(user.level);
    const xpNeeded = nextLevelXp - user.xp;

    const message = `📊 *Твоя статистика:*\n` +
        `👤 Пользователь: ${getUserName(user)}\n` +
        `⭐ Уровень: ${user.level}\n` +
        `✨ Опыт: ${user.xp}\n` +
        `🍪 Репутация: ${user.reputation}\n` +
        `📈 До следующего уровня: ${xpNeeded} XP`;

    sendTimedMessage(chatId, message, 60000, { parse_mode: 'Markdown' });
});

bot.onText(/\/top/, async (msg) => {
    const chatId = msg.chat.id;
    const userId = msg.from.id;

    deleteMsg(chatId, msg.message_id);

    // Проверка кулдауна
    if (!(await isAdmin(chatId, userId))) {
        const lastTime = commandCooldowns[userId] || 0;
        if (Date.now() - lastTime < COMMAND_COOLDOWN_TIME) {
            const remaining = Math.ceil((COMMAND_COOLDOWN_TIME - (Date.now() - lastTime)) / 60000);
            sendTimedMessage(chatId, `⏳ ${getUserName(msg.from)}, подожди ${remaining} мин. перед следующей командой!`, 60000);
            return;
        }
        commandCooldowns[userId] = Date.now();
    }

    const { data: users } = await supabase
        .from('users')
        .select('*')
        .eq('chat_id', chatId)
        .order('level', { ascending: false })
        .order('xp', { ascending: false })
        .limit(10);

    if (!users || users.length === 0) {
        sendTimedMessage(chatId, 'В этом чате еще нет активности.', 60000);
        return;
    }

    let message = '🏆 *Топ активных участников:*\n';
    users.forEach((u, index) => {
        message += `${index + 1}. ${getUserName(u)} — ${u.level} ур. (${u.reputation} 🍪)\n`;
    });
    sendTimedMessage(chatId, message, 60000, { parse_mode: 'Markdown' });
});

bot.onText(/\/kto (.+)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const userId = msg.from.id;

    deleteMsg(chatId, msg.message_id);

    // Проверка кулдауна
    if (!(await isAdmin(chatId, userId))) {
        const lastTime = commandCooldowns[userId] || 0;
        if (Date.now() - lastTime < COMMAND_COOLDOWN_TIME) {
            const remaining = Math.ceil((COMMAND_COOLDOWN_TIME - (Date.now() - lastTime)) / 60000);
            sendTimedMessage(chatId, `⏳ ${getUserName(msg.from)}, подожди ${remaining} мин. перед следующей командой!`, 60000);
            return;
        }
        commandCooldowns[userId] = Date.now();
    }

    const question = match[1];

    // Получаем всех пользователей чата (можно оптимизировать через random, но пока так)
    const { data: users } = await supabase.from('users').select('*').eq('chat_id', chatId);

    if (!users || users.length === 0) {
        sendTimedMessage(chatId, 'Я еще никого не знаю...', 60000);
        return;
    }

    const randomUser = users[Math.floor(Math.random() * users.length)];
    sendTimedMessage(chatId, `🤔 Я думаю, что ${question} — это ${getUserName(randomUser)}!`, 60000);
});

bot.on('sticker', (msg) => {
    console.log(`[STICKER] ID: ${msg.sticker.file_id}`);
});

console.log('Бот запущен (Supabase Edition)...');
