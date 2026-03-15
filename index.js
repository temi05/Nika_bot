require('dotenv').config();
const TelegramBot = require('node-telegram-bot-api');
const { createClient } = require('@supabase/supabase-js');
const express = require('express');
const captchapng = require('captchapng');

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
    console.log('[DEBUG] Webhook request received:', JSON.stringify(req.body).substring(0, 100) + '...');

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

// Хранилище кулдаунов реакций (и текстовой репутации)
const reactionCooldowns = {};
const REACTION_COOLDOWN_TIME = 60000; // 1 минута
const ANONYMOUS_ADMIN_ID = 1087968824; // ID GroupAnonymousBot

const commandCooldowns = {};
const COMMAND_COOLDOWN_TIME = 120000; // 2 минуты
const pendingVerifications = {};

// --- КЭШИРОВАНИЕ ---
const userCache = {}; // { userId: { data: userObj, expires: timestamp } }
const USER_CACHE_TTL = 1000 * 60 * 5; // 5 минут

const badWordsCache = {}; // { chatId: { words: [], expires: timestamp } }
const BAD_WORDS_CACHE_TTL = 1000 * 60 * 10; // 10 минут

// --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ БД ---

async function getUser(chatId, userId, userInfo = {}) {
    const cacheKey = `${chatId}_${userId}`;
    // 1. Проверяем кэш
    if (userCache[cacheKey] && Date.now() < userCache[cacheKey].expires) {
        return userCache[cacheKey].data;
    }

    // 2. Если нет в кэше - идем в БД
    let { data: user, error } = await supabase
        .from('users')
        .select('*')
        .eq('chat_id', chatId)
        .eq('user_id', userId)
        .single();

    if (error && error.code !== 'PGRST116') {
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
        user = data;
    }

    // 3. Сохраняем в кэш
    userCache[cacheKey] = {
        data: user,
        expires: Date.now() + USER_CACHE_TTL
    };

    return user;
}

async function updateUser(id, updates) {
    // Обновляем БД
    const { error } = await supabase
        .from('users')
        .update(updates)
        .eq('id', id);

    if (error) {
        console.error('Ошибка обновления пользователя:', error);
        return;
    }

    // Обновляем кэш (ищем пользователя в кэше по ID записи)
    const cacheKey = Object.keys(userCache).find(key => userCache[key].data.id === id);
    if (cacheKey) {
        userCache[cacheKey].data = { ...userCache[cacheKey].data, ...updates };
        userCache[cacheKey].expires = Date.now() + USER_CACHE_TTL; // Продлеваем жизнь
    }
}

async function getBadWords(chatId) {
    // Проверяем кэш
    if (badWordsCache[chatId] && Date.now() < badWordsCache[chatId].expires) {
        return badWordsCache[chatId].words;
    }

    const { data, error } = await supabase
        .from('bad_words')
        .select('word')
        .eq('chat_id', chatId);

    if (error) {
        console.error('Ошибка получения плохих слов:', error);
        return [];
    }

    const words = data.map(item => item.word);

    // Сохраняем в кэш
    badWordsCache[chatId] = {
        words: words,
        expires: Date.now() + BAD_WORDS_CACHE_TTL
    };

    return words;
}

// --- ЛОГИКА БОТА ---

function getNextLevelXp(level) {
    return 50 * level * level + 50 * level;
}

function getUserName(user) {
    const name = user.username ? `@${user.username}` : user.first_name;
    return name;
}

function escapeMarkdown(text) {
    if (text === undefined || text === null) return '';
    return String(text).replace(/[_*[\]()~`>#+\-=|{}.!\\]/g, '\\$&');
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

// Верификация новых участников и Анти-бот Защита

bot.on('new_chat_members', async (msg) => {
    const chatId = msg.chat.id;
    const newMembers = msg.new_chat_members;
    const botId = parseInt(token.split(':')[0]); // Извлекаем ID бота из токена

    for (const member of newMembers) {
        // Определяем того, кто пригласил или вошел сам
        const inviter = msg.from;
        
        // Вспомогательная функция для имен в HTML
        const safeName = (user) => {
            const name = user.username ? `@${user.username}` : user.first_name;
            return String(name).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        };

        const inviterName = safeName(inviter);

        // --- АНТИ-БОТ ЗАЩИТА (Официальные боты) ---
        if (member.is_bot) {
            if (member.id !== botId) {
                try {
                    await bot.banChatMember(chatId, member.id);
                    const alertMsg = `🚨 <b>ОБНАРУЖЕН БОТ!</b> 🚨\nПользователь ${inviterName} (ID: <code>${inviter.id}</code>) добавил стороннего бота.\nБот забанен. Обратите внимание на пригласившего!`;
                    sendTimedMessage(chatId, alertMsg, 300000, { parse_mode: 'HTML' }); // Оставляем на 5 минут
                    console.warn(`[ANTI-BOT] User ${inviter.id} added bot ${member.id}`);
                } catch (err) {
                    console.error('Ошибка при бане чужого бота:', err);
                }
            }
            continue;
        }

        // --- ANTI-INVITER (Отслеживание добавления людей/юзерботов) ---
        // Если ID присоединившегося НЕ совпадает с ID того, кто вызвал событие,
        // значит, его кто-то добавил (инфайт).
        if (inviter && member.id !== inviter.id && inviter.id !== botId) {
            const memberName = safeName(member);
            const inviteMsg = `👀 <b>Внимание модераторам!</b>\nПользователь ${inviterName} (ID: <code>${inviter.id}</code>) добавил в чат участника ${memberName} (ID: <code>${member.id}</code>).\nЕсли новичок начнет спамить, баньте обоих!`;
            sendTimedMessage(chatId, inviteMsg, 300000, { parse_mode: 'HTML' }); // Висит 5 минут
            console.log(`[ANTI-INVITER] ${inviter.id} added ${member.id}`);
        }

        // --- ОБЫЧНАЯ ВЕРИФИКАЦИЯ ЧЕЛОВЕКА ---
        const name = member.username ? `@${member.username}` : member.first_name;

        try {
            await bot.restrictChatMember(chatId, member.id, {
                can_send_messages: true, // Включаем сообщения, чтобы человек мог написать код
                can_send_other_messages: false, // Запрещает стикеры, гифки
                can_send_media_messages: false, // Запрещает фото/видео
                can_add_web_page_previews: false
            });
        } catch (err) {
            sendTimedMessage(chatId, `👋 Привет, ${name}! (Дайте боту права админа для верификации)`);
            continue;
        }

        // Генерация капчи (цифры)
        const captchaNumber = parseInt(Math.random() * 9000 + 1000); // 1000-9999
        // Делаем картинку квадратной (150x150), чтобы Telegram на телефоне не обрезал превью
        const p = new captchapng(150, 150, captchaNumber);
        p.color(30, 30, 30, 255);  // Фон: темно-серый
        p.color(255, 255, 255, 255); // Текст: белый
        const img = p.getBase64();
        const imgbase64 = Buffer.from(img, 'base64');

        const opts = {
            caption: `👋 Привет, ${name}! Добро пожаловать!\nПожалуйста, **напишите цифры с картинки** в чат, чтобы доказать, что вы человек.\n*(У вас есть 2 минуты)*`,
            parse_mode: 'Markdown'
        };

        bot.sendPhoto(chatId, imgbase64, opts).then(sentMsg => {
            const timeoutId = setTimeout(async () => {
                try {
                    // Бан на 60 секунд. Telegram сам разбанит его через минуту, но выкинет из чата (кик)
                    const untilDate = Math.floor(Date.now() / 1000) + 60;
                    await bot.banChatMember(chatId, member.id, { until_date: untilDate });

                    bot.deleteMessage(chatId, sentMsg.message_id).catch(() => { });
                    sendTimedMessage(chatId, `🚪 ${name} не прошел проверку и был исключен.`);
                } catch (err) {
                    console.error(`Не удалось кикнуть ${name}:`, err.message);
                }
                delete pendingVerifications[member.id];
            }, 120000);

            // Сохраняем ожидаемый ответ и ID сообщения капчи
            pendingVerifications[member.id] = { 
                timeoutId, 
                captchaNumber: captchaNumber.toString(),
                messageId: sentMsg.message_id
            };
        });
    }
});

// Callback Query (Кнопки бана инвайтерами)
bot.on('callback_query', async (query) => {
    const chatId = query.message.chat.id;
    const userId = query.from.id;
    const data = query.data;

    if (data.startsWith('ban_')) {
        const targetId = parseInt(data.split('_')[1]);

        if (!(await isAdmin(chatId, userId))) {
            bot.answerCallbackQuery(query.id, { text: 'Только админы могут банить при входе! 🚫', show_alert: true });
            return;
        }

        if (pendingVerifications[targetId]) {
            clearTimeout(pendingVerifications[targetId].timeoutId);
            delete pendingVerifications[targetId];
        }

        try {
            await bot.banChatMember(chatId, targetId);
            bot.deleteMessage(chatId, query.message.message_id).catch(() => { });
            sendTimedMessage(chatId, `🚫 Администратор ${escapeMarkdown(query.from.first_name)} забанил пользователя при входе.`, 15000, { parse_mode: 'MarkdownV2' });
            bot.answerCallbackQuery(query.id, { text: 'Пользователь забанен.' });
        } catch (err) {
            bot.answerCallbackQuery(query.id, { text: 'Ошибка! Бот не админ или цель тоже админ.', show_alert: true });
        }
    }
});

// --- ОБРАБОТКА СООБЩЕНИЙ (CAPTCHA, XP, Репутация, Фильтр) ---
bot.on('message', async (msg) => {
    const chatId = msg.chat.id;
    const userId = msg.from.id;
    const user = msg.from;

    // --- 1. ПРОВЕРКА CAPTCHA (ВВОД ЦИФР) ---
    if (pendingVerifications[userId]) {
        const pending = pendingVerifications[userId];
        
        if (msg.text && msg.text.trim() === pending.captchaNumber) {
            // Пользователь ввел правильные цифры
            clearTimeout(pending.timeoutId);
            delete pendingVerifications[userId];
            
            try {
                // Выдаем полные права
                await bot.restrictChatMember(chatId, userId, {
                    can_send_messages: true,
                    can_send_media_messages: true,
                    can_send_polls: true,
                    can_send_other_messages: true,
                    can_add_web_page_previews: true,
                    can_invite_users: true
                });

                // Удаляем капчу и удаляем сообщение пользователя с цифрами, чтобы не засорять чат
                bot.deleteMessage(chatId, pending.messageId).catch(() => { });
                bot.deleteMessage(chatId, msg.message_id).catch(() => { });
                
                sendTimedMessage(chatId, `✅ ${user.first_name} успешно прошел проверку! Добро пожаловать!`, 10000);
            } catch (err) {
                console.error('Ошибка при выдаче прав после капчи:', err);
            }
        } else {
            // Неправильные цифры или стикер/картинка (msg.text undefined)
            // Удаляем сообщение, чтобы предотвратить спам до прохождения капчи
            bot.deleteMessage(chatId, msg.message_id).catch(() => { });
        }
        
        // Прерываем обработку других правил для этого пользователя (XP, плохие слова и т.д.)
        return; 
    }

    // Логируем сообщение для реакций (Persistent Reputation)
    if (msg.message_id) {
        // Кэш для быстрого доступа
        if (!messageAuthors[chatId]) messageAuthors[chatId] = {};
        messageAuthors[chatId][msg.message_id] = userId;

        // БД для надежности (Fire-and-Forget, без await)
        supabase.from('message_logs').insert([{
            chat_id: chatId,
            message_id: msg.message_id,
            user_id: userId
        }]).then(({ error }) => {
            if (error) console.error('Ошибка лога сообщения:', error.message);
            // else console.log(`[DEBUG] Message ${msg.message_id} saved to DB`); // Отключаем лог для скорости
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

    // Игнорируем ботов (кроме анонимного админа)
    if (msg.from.is_bot && msg.from.id !== ANONYMOUS_ADMIN_ID) return;

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
        const isReceiverValid = !msg.reply_to_message.from.is_bot || receiverId === ANONYMOUS_ADMIN_ID;

        if (userId !== receiverId && isReceiverValid) {
            const text = msg.text.trim().toLowerCase();
            // Строгое соответствие: сообщение должно состоять ТОЛЬКО из триггера
            const positiveTriggers = ['+', 'спасибо', 'спс', 'благодарю', '👍'];
            const negativeTriggers = ['-', '👎', 'fu', 'дизлайк', 'фу'];

            let change = 0;
            if (positiveTriggers.includes(text)) change = 1;
            if (negativeTriggers.includes(text)) change = -1;

            if (change !== 0) {
                const cooldownKey = `${userId}_${receiverId}`;
                const lastReactionTime = reactionCooldowns[cooldownKey] || 0;

                if (Date.now() - lastReactionTime < REACTION_COOLDOWN_TIME) {
                    sendTimedMessage(chatId, `⏳ ${getUserName(user)}, подожди минуту перед изменением репутации!`, 10000);
                } else {
                    const receiver = await getUser(chatId, receiverId, msg.reply_to_message.from);
                    if (receiver) {
                        await updateUser(receiver.id, { reputation: receiver.reputation + change });
                        const senderName = escapeMarkdown(getUserName(user));
                        const receiverName = escapeMarkdown(getUserName(msg.reply_to_message.from));
                        const emoji = change > 0 ? '🌟' : '📉';
                        const actionText = change > 0 ? 'повысил' : 'понизил';
                        const sign = change > 0 ? '\\+' : '';
                        const changeVal = escapeMarkdown(change);

                        sendTimedMessage(chatId, `${emoji} ${senderName} ${actionText} репутацию ${receiverName}\\! \\(${sign}${changeVal}\\)`, 60000, { parse_mode: 'MarkdownV2' });
                        reactionCooldowns[cooldownKey] = Date.now();
                    }
                }
            }
        }
    }
});

// --- РЕПУТАЦИЯ (РЕАКЦИИ) ---
async function handleReaction(reaction) {
    const chatId = reaction.chat.id;
    const messageId = reaction.message_id;

    // Получаем автора сообщения
    let authorId = messageAuthors[chatId]?.[messageId];

    if (!authorId) {
        const { data } = await supabase
            .from('message_logs')
            .select('user_id')
            .eq('chat_id', chatId)
            .eq('message_id', messageId)
            .single();
        if (data) authorId = data.user_id;
    }

    if (!authorId) return;

    // Определяем пользователя, который поставил реакцию
    let actorId = reaction.user?.id;

    // Поддержка анонимных админов (GroupAnonymousBot)
    if (!reaction.user && reaction.actor_chat) {
        actorId = ANONYMOUS_ADMIN_ID; // ID GroupAnonymousBot
    }

    if (!actorId) return; // Не смогли определить кто поставил

    // Нельзя менять репутацию самому себе
    if (actorId === authorId) return;

    // --- ПРОВЕРКА ПРАВ ПОЛЬЗОВАТЕЛЯ (ЗАЩИТА ОТ БОТОВ/СПАМА) ---
    // Не учитываем реакции от анонимных админов и ботов
    if (actorId === ANONYMOUS_ADMIN_ID) return;

    try {
        const chatMember = await bot.getChatMember(chatId, actorId);
        // Если пользователь в муте (restricted) или бот - игнорируем
        if (chatMember.user.is_bot || chatMember.status === 'restricted' || chatMember.status === 'left' || chatMember.status === 'kicked') {
            return;
        }
    } catch (err) {
        console.error(`[REP] Учесть реакцию не удалось, ошибка getChatMember:`, err.message);
        return; // Если не смогли получить статус, лучше проигнорировать
    }

    // Проверка кулдауна
    const cooldownKey = `${actorId}_${authorId}`;
    if (reactionCooldowns[cooldownKey] && Date.now() - reactionCooldowns[cooldownKey] < REACTION_COOLDOWN_TIME) {
        return; // Кулдаун
    }

    // --- ЛОГИКА ДЕЛЬТЫ ---
    const getReactionScore = (reacts) => {
        if (!reacts || reacts.length === 0) return 0;
        const emoji = reacts[0]?.emoji;
        if (!emoji) return 0;

        const negativeReactions = ['👎', '💩', '🤮', '🤬', '😤'];
        return negativeReactions.includes(emoji) ? -1 : 1;
    };

    const oldScore = getReactionScore(reaction.old_reaction);
    const newScore = getReactionScore(reaction.new_reaction);
    const delta = newScore - oldScore;

    if (delta === 0) return; // Репутация не изменилась

    const author = await getUser(chatId, authorId);
    if (author) {
        await updateUser(author.id, { reputation: author.reputation + delta });

        const sign = delta > 0 ? '+' : '';
        console.log(`[REP] User ${authorId} reputation ${sign}${delta} (Reaction change)`);

        // Обновляем таймер кулдауна
        reactionCooldowns[cooldownKey] = Date.now();
    }
}

bot.on('message_reaction', handleReaction);



// Вспомогательная функция для удаления сообщения
function deleteMsg(chatId, msgId, delay = 60000) {
    setTimeout(() => {
        bot.deleteMessage(chatId, msgId).catch(() => { });
    }, delay);
}

// --- КОМАНДЫ ---

// Команда /help
bot.onText(/^\/help$/, async (msg) => {
    const chatId = msg.chat.id;
    const userId = msg.from.id;

    // Удаляем команду пользователя через 1 минуту
    deleteMsg(chatId, msg.message_id);

    // Проверка кулдауна (для всех, кроме админов)
    if (!(await isAdmin(chatId, userId))) {
        const lastTime = commandCooldowns[userId] || 0;
        if (Date.now() - lastTime < COMMAND_COOLDOWN_TIME) {
            const remaining = Math.ceil((COMMAND_COOLDOWN_TIME - (Date.now() - lastTime)) / 60000);
            sendTimedMessage(chatId, `⏳ ${getUserName(msg.from)}, подожди ${remaining} мин. перед следующей командой!`);
            return;
        }
        commandCooldowns[userId] = Date.now();
    }

    const helpText = `🤖 *Что я умею:*

👤 *Для всех:*
/me — Посмотреть свою статистику \\(уровень, опыт, репутация\\)
/top — Топ\\-10 активных участников
/kto <вопрос\\> — Выбрать случайного участника \\(например: /kto кто сегодня платит?\\)
👍 *Репутация:*
• Повысить \\(\\+1\\): Ответь "спасибо", "\\+", "👍" или поставь любую позитивную реакцию\\.
• Понизить \\(\\-1\\): Ответь "\\-", "👎", "фу" или поставь реакцию 👎, 💩, 🤮, 🤬, 😤\\.

👮‍♂️ *Для админов:*
/banword <слово\\> — Запретить слово
/unbanword <слово\\> — Разрешить слово
/listwords — Список запрещенных слов

_Я также защищаю чат от спама и проверяю новичков\\!_`;

    sendTimedMessage(chatId, helpText, 60000, { parse_mode: 'MarkdownV2' });
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

bot.onText(/^\/me$/, async (msg) => {
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

    if (!user) {
        sendTimedMessage(chatId, '❌ Ошибка получения данных пользователя.', 10000);
        return;
    }

    const nextLevelXp = getNextLevelXp(user.level);
    const xpNeeded = nextLevelXp - user.xp;

    const message = `📊 *Твоя статистика:*\n` +
        `👤 Пользователь: ${escapeMarkdown(getUserName(user))}\n` +
        `⭐ Уровень: ${escapeMarkdown(user.level)}\n` +
        `✨ Опыт: ${escapeMarkdown(user.xp)}\n` +
        `🍪 Репутация: ${escapeMarkdown(user.reputation)}\n` +
        `📈 До следующего уровня: ${escapeMarkdown(xpNeeded)} XP`;

    sendTimedMessage(chatId, message, 60000, { parse_mode: 'MarkdownV2' });
});

bot.onText(/^\/top$/, async (msg) => {
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
        message += `${index + 1}\\. ${escapeMarkdown(getUserName(u))} — ${escapeMarkdown(u.level)} ур\\. \\(${escapeMarkdown(u.reputation)} 🍪\\)\n`;
    });
    sendTimedMessage(chatId, message, 60000, { parse_mode: 'MarkdownV2' });
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

bot.onText(/\/ban(?:\s+(.+))?/, async (msg, match) => {
    const chatId = msg.chat.id;
    const userId = msg.from.id;

    deleteMsg(chatId, msg.message_id);

    if (!(await isAdmin(chatId, userId))) {
        sendTimedMessage(chatId, '⛔ Только админы могут банить!', 60000);
        return;
    }

    let targetId = null;
    let targetName = 'Пользователь';

    // 1. По реплаю
    if (msg.reply_to_message) {
        targetId = msg.reply_to_message.from.id;
        targetName = getUserName(msg.reply_to_message.from);
    }
    // 2. По аргументу (ID или Username)
    else if (match[1]) {
        const arg = match[1].trim();

        if (/^\d+$/.test(arg)) {
            // Это цифровой ID
            targetId = parseInt(arg, 10);
            targetName = `ID ${targetId}`;
        } else if (arg.startsWith('@')) {
            // Это юзернейм - ищем в нашей БД
            const username = arg.substring(1).toLowerCase(); // Убираем @
            const { data } = await supabase
                .from('users')
                .select('user_id, username, first_name')
                .eq('chat_id', chatId)
                .ilike('username', username)
                .limit(1)
                .maybeSingle(); // maybeSingle чтобы не кидало ошибку, если пусто

            if (data) {
                targetId = data.user_id;
                targetName = `@${data.username || username}`;
            } else {
                sendTimedMessage(chatId, `❌ Пользователь ${escapeMarkdown(arg)} не найден в базе данных этого чата\\. Попробуйте по ID\\.`, 60000, { parse_mode: 'MarkdownV2' });
                return;
            }
        } else {
            sendTimedMessage(chatId, `❌ Неверный формат\\. Используйте реплай, ID или @username\\.`, 60000, { parse_mode: 'MarkdownV2' });
            return;
        }
    } else {
        sendTimedMessage(chatId, `❌ Кого банить? Ответьте на сообщение или укажите ID/username\\.`, 60000, { parse_mode: 'MarkdownV2' });
        return;
    }

    // Защита от бана анонимного админа
    if (targetId === ANONYMOUS_ADMIN_ID) {
        sendTimedMessage(chatId, `❌ Я не могу забанить анонимного администратора\\.`, 60000, { parse_mode: 'MarkdownV2' });
        return;
    }

    // Проверка, не админ ли цель (если цель в чате)
    if (await isAdmin(chatId, targetId)) {
        sendTimedMessage(chatId, `❌ Нельзя забанить администратора\\.`, 60000, { parse_mode: 'MarkdownV2' });
        return;
    }

    try {
        await bot.banChatMember(chatId, targetId);
        sendTimedMessage(chatId, `🚫 Администратор ${escapeMarkdown(getUserName(msg.from))} забанил ${escapeMarkdown(targetName)}\\.`, 60000, { parse_mode: 'MarkdownV2' });
    } catch (err) {
        sendTimedMessage(chatId, `❌ Ошибка при попытке забанить\\. Убедитесь, что у меня есть права администратора\\.`, 60000, { parse_mode: 'MarkdownV2' });
        console.error('Ban command error:', err.message);
    }
});

bot.onText(/\/unban(?:\s+(.+))?/, async (msg, match) => {
    const chatId = msg.chat.id;
    const userId = msg.from.id;

    deleteMsg(chatId, msg.message_id);

    if (!(await isAdmin(chatId, userId))) {
        sendTimedMessage(chatId, '⛔ Только админы могут разбанивать!', 60000);
        return;
    }

    let targetId = null;
    let targetName = 'Пользователь';

    if (match[1]) {
        const arg = match[1].trim();

        if (/^\d+$/.test(arg)) {
            // Это цифровой ID
            targetId = parseInt(arg, 10);
            targetName = `ID ${targetId}`;
        } else if (arg.startsWith('@')) {
            // Это юзернейм - ищем в нашей БД
            const username = arg.substring(1).toLowerCase();
            const { data } = await supabase
                .from('users')
                .select('user_id, username')
                .eq('chat_id', chatId)
                .ilike('username', username)
                .limit(1)
                .maybeSingle();

            if (data) {
                targetId = data.user_id;
                targetName = `@${data.username || username}`;
            } else {
                sendTimedMessage(chatId, `❌ Пользователь ${escapeMarkdown(arg)} не найден в базе данных этого чата\\. Разбаньте по ID\\.`, 60000, { parse_mode: 'MarkdownV2' });
                return;
            }
        } else {
            sendTimedMessage(chatId, `❌ Неверный формат\\. Используйте ID или @username\\.`, 60000, { parse_mode: 'MarkdownV2' });
            return;
        }
    } else {
        sendTimedMessage(chatId, `❌ Кого разбанить? Укажите ID или @username\\.`, 60000, { parse_mode: 'MarkdownV2' });
        return;
    }

    try {
        await bot.unbanChatMember(chatId, targetId, { only_if_banned: true });
        sendTimedMessage(chatId, `✅ Администратор ${escapeMarkdown(getUserName(msg.from))} разбанил ${escapeMarkdown(targetName)}\\.`, 60000, { parse_mode: 'MarkdownV2' });
    } catch (err) {
        sendTimedMessage(chatId, `❌ Ошибка при попытке разбанить\\. Убедитесь, что пользователь был забанен\\.`, 60000, { parse_mode: 'MarkdownV2' });
        console.error('Unban command error:', err.message);
    }
});

bot.on('sticker', (msg) => {
    console.log(`[STICKER] ID: ${msg.sticker.file_id}`);
});

console.log('Бот запущен (Supabase Edition)...');

