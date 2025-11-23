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

// Создаем бота
const bot = new TelegramBot(token, {
    polling: {
        params: {
            allowed_updates: ['message', 'message_reaction', 'chat_member', 'callback_query']
        }
    }
});

// --- НАСТРОЙКА ДЛЯ ХОСТИНГА ---
const app = express();
const PORT = process.env.PORT || 3000;

app.get('/', (req, res) => {
    res.send('Бот работает! 🤖');
});

app.listen(PORT, () => {
    console.log(`Веб-сервер запущен на порту ${PORT}`);
});
// ------------------------------

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
        console.error('Ошибка получения пользователя:', error);
        return null;
    }

    // Если нет - создаем
    if (!user) {
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
            console.error('Ошибка создания пользователя:', createError);
            return null;
        }
        return data;
    }

    // Обновляем инфо, если изменилось
    if (userInfo.username && (user.username !== userInfo.username || user.first_name !== userInfo.first_name)) {
        await supabase.from('users').update({
            username: userInfo.username,
            first_name: userInfo.first_name
        }).eq('id', user.id);
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

// Реакции (Репутация)
bot.on('message_reaction', async (reaction) => {
    const chatId = reaction.chat.id;
    const messageId = reaction.message_id;
    const userReacted = reaction.user;

    // Ищем автора сообщения в памяти
    const authorId = messageAuthors[`${chatId}_${messageId}`];
    if (!authorId) return;

    if (userReacted.id === authorId) return;

    const goodEmojis = ['👍', '🔥', '❤️', '🍪', '⚡', '🥰', '🎉'];
    const newEmojis = reaction.new_reaction.filter(r => r.type === 'emoji').map(r => r.emoji);
    const oldEmojis = reaction.old_reaction.filter(r => r.type === 'emoji').map(r => r.emoji);
    const addedEmojis = newEmojis.filter(emoji => !oldEmojis.includes(emoji));

    if (addedEmojis.some(emoji => goodEmojis.includes(emoji))) {
        const user = await getUser(chatId, authorId);
        if (user) {
            await updateUser(user.id, { reputation: user.reputation + 1 });
            sendTimedMessage(chatId, `🌟 Репутация ${getUserName(user)} повышена! (+1)`);
            console.log(`[REP] ${userReacted.first_name} лайкнул сообщение ${authorId}. Репутация +1`);
        }
    }
});

// Обработка сообщений
bot.on('message', async (msg) => {
    const chatId = msg.chat.id;
    const userId = msg.from.id;
    const user = msg.from;

    if (user.is_bot) return;

    // Сохраняем автора сообщения для реакций
    messageAuthors[`${chatId}_${msg.message_id}`] = userId;

    // Чистим память авторов (оставляем последние 2000)
    const keys = Object.keys(messageAuthors);
    if (keys.length > 2000) {
        delete messageAuthors[keys[0]];
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
    if (msg.text && !msg.text.startsWith('/')) {
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

// --- КОМАНДЫ ---

bot.onText(/\/banword (.+)/, async (msg, match) => {
    const chatId = msg.chat.id;
    if (!(await isAdmin(chatId, msg.from.id))) return;

    const word = match[1].trim();
    const { data } = await supabase.from('bad_words').select('*').eq('chat_id', chatId).eq('word', word).single();

    if (!data) {
        await supabase.from('bad_words').insert([{ chat_id: chatId, word: word }]);
        sendTimedMessage(chatId, `✅ Слово "${word}" добавлено в бан.`);
    } else {
        sendTimedMessage(chatId, `ℹ️ Слово "${word}" уже в списке.`);
    }
});

bot.onText(/\/unbanword (.+)/, async (msg, match) => {
    const chatId = msg.chat.id;
    if (!(await isAdmin(chatId, msg.from.id))) return;

    const word = match[1].trim();
    const { error } = await supabase.from('bad_words').delete().eq('chat_id', chatId).eq('word', word);

    if (!error) {
        sendTimedMessage(chatId, `✅ Слово "${word}" удалено из бана.`);
    } else {
        sendTimedMessage(chatId, `ℹ️ Ошибка или слово не найдено.`);
    }
});

bot.onText(/\/listwords/, async (msg) => {
    const chatId = msg.chat.id;
    if (!(await isAdmin(chatId, msg.from.id))) return;

    const words = await getBadWords(chatId);
    if (words.length === 0) {
        sendTimedMessage(chatId, 'Список пуст.');
    } else {
        bot.sendMessage(chatId, `🚫 Запрещенные слова:\n${words.join(', ')}`);
    }
});

bot.onText(/\/me/, async (msg) => {
    const chatId = msg.chat.id;
    const userId = msg.from.id;
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

    bot.sendMessage(chatId, message, { parse_mode: 'Markdown' });
});

bot.onText(/\/top/, async (msg) => {
    const chatId = msg.chat.id;

    const { data: users } = await supabase
        .from('users')
        .select('*')
        .eq('chat_id', chatId)
        .order('level', { ascending: false })
        .order('xp', { ascending: false })
        .limit(10);

    if (!users || users.length === 0) {
        bot.sendMessage(chatId, 'В этом чате еще нет активности.');
        return;
    }

    let message = '🏆 *Топ активных участников:*\n';
    users.forEach((u, index) => {
        message += `${index + 1}. ${getUserName(u)} — ${u.level} ур. (${u.reputation} 🍪)\n`;
    });
    bot.sendMessage(chatId, message, { parse_mode: 'Markdown' });
});

bot.onText(/\/kto (.+)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const question = match[1];

    // Получаем всех пользователей чата (можно оптимизировать через random, но пока так)
    const { data: users } = await supabase.from('users').select('*').eq('chat_id', chatId);

    if (!users || users.length === 0) {
        bot.sendMessage(chatId, 'Я еще никого не знаю...');
        return;
    }

    const randomUser = users[Math.floor(Math.random() * users.length)];
    bot.sendMessage(chatId, `🤔 Я думаю, что ${question} — это ${getUserName(randomUser)}!`);
});

bot.on('sticker', (msg) => {
    console.log(`[STICKER] ID: ${msg.sticker.file_id}`);
});

console.log('Бот запущен (Supabase Edition)...');
