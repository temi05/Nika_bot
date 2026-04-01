const { bot, escapeHTML, getUserName, getSenderData, sendTimedMessage, deleteMsg, isAdmin } = require('../utils');
const { getUser, updateUser, getBadWords, supabase, commandCooldowns, getNextLevelXp, ANONYMOUS_ADMIN_ID, claimDailyBonus, getChatSettings, updateChatSettings } = require('../database');

function registerCommands() {
    bot.onText(/^\/help$/, async (msg) => {
        const chatId = msg.chat.id;
        const helpText = `💎 <b>ГЛАВНОЕ МЕНЮ БОТА</b>\n` +
            `━━━━━━━━━━━━━━━━━━\n\n` +
            `👤 <b>Для пользователей:</b>\n` +
            `🔹 <code>/me</code> — Мой профиль и статистика\n` +
            `🔹 <code>/top</code> — Рейтинг самых активных\n` +
            `🔹 <code>/daily</code> — Ежедневный бонус 🎁\n` +
            `🔹 <code>/mybirthday DD.MM</code> — Твой день рождения\n` +
            `🔹 <code>/bio &lt;текст&gt;</code> — Кратко о себе\n` +
            `🔹 <code>/notes</code> — Что о тебе знает ИИ 🕵️‍♀️\n` +
            `🔹 <code>/give &lt;кол-во&gt;</code> — Передать печеньки (реплай)\n` +
            `🔹 <code>/kto &lt;текст&gt;</code> — Узнать, кто...\n` +
            `🔹 <code>/dashboard</code> — Панель управления (Mini App)\n\n` +
            `🛡 <b>Управление (Админ):</b>\n` +
            `🔸 <code>/ban</code>, <code>/unban</code> — Управление доступом\n` +
            `🔸 <code>/banword</code>, <code>/unbanword</code>, <code>/listwords</code> — Фильтр мата\n` +
            `🔸 <code>/linkfilter [on|off]</code> — 🔗 Разрешить/запретить ссылки t.me\n\n` +
            `━━━━━━━━━━━━━━━━━━\n` +
            `<i>Я защищаю этот чат и помогаю общаться!</i>`;
        bot.sendMessage(chatId, helpText, { parse_mode: 'HTML' });
    });

    // /dashboard
    bot.onText(/^\/dashboard$/, async (msg) => {
        const chatId = msg.chat.id;
        try {
            const botInfo = await bot.getMe();
            const { userId } = getSenderData(msg);
            
            // Telegram запрещает web_app кнопки в групповых чатах
            if (msg.chat.type !== 'private') {
                // Если юзер — обычный, ссылка общая (db_CHATID). Если канал — персональная (_uUSERID)
                const startParam = userId < 0 ? `db_${chatId}_u${userId}` : `db_${chatId}`;
                const opts = {
                    reply_markup: {
                        inline_keyboard: [
                            [{ text: "🖥 Открыть в личных сообщениях", url: `https://t.me/${botInfo.username}?start=${startParam}` }]
                        ]
                    }
                };
                return bot.sendMessage(chatId, "✨ Управление профилем и сервером Aternos доступно только в личных сообщениях с ботом!", opts)
                    .then(sentMsg => deleteMsg(chatId, sentMsg.message_id, 60000));
            }

            const baseUrl = process.env.RENDER_EXTERNAL_URL ? process.env.RENDER_EXTERNAL_URL.replace(/\/$/, '') : 'https://google.com';
            const url = `${baseUrl}/miniapp/index.html`;
            
            const opts = {
                reply_markup: {
                    inline_keyboard: [
                        [{ text: "🖥 Открыть Dashboard", web_app: { url: url } }]
                    ]
                }
            };
            bot.sendMessage(chatId, "✨ Нажми на кнопку ниже, чтобы открыть панель управления профилем и сервером Aternos:", opts);
        } catch (error) {
            console.error('Ошибка в /dashboard:', error);
        }
    });

    // /start (для обработки перехода по ссылке из группового чата)
    bot.onText(/^\/start(.*)/, async (msg, match) => {
        if (msg.chat.type !== 'private') return;
        const chatId = msg.chat.id;
        const arg = match[1] ? match[1].trim() : '';

        const baseUrl = process.env.RENDER_EXTERNAL_URL ? process.env.RENDER_EXTERNAL_URL.replace(/\/$/, '') : 'https://google.com';
        
        let url = `${baseUrl}/miniapp/index.html`;

        if (arg.startsWith('db_')) {
            // Формат: db_CHATID или db_CHATID_uUSERID
            const parts = arg.split('_u');
            const targetChatId = parts[0].substring(3);
            const targetUserId = parts[1] || null;

            url += `?chat_id=${targetChatId}`;
            if (targetUserId) url += `&as_user=${targetUserId}`;
            
            const opts = {
                reply_markup: {
                    inline_keyboard: [
                        [{ text: "🖥 Открыть Dashboard", web_app: { url: url } }]
                    ]
                }
            };
            bot.sendMessage(chatId, "✨ Нажми на кнопку ниже, чтобы открыть панель управления конкретным чатом:", opts);
        } else if (arg === 'dashboard') {
            const opts = {
                reply_markup: {
                    inline_keyboard: [
                        [{ text: "🖥 Открыть Dashboard", web_app: { url: url } }]
                    ]
                }
            };
            bot.sendMessage(chatId, "✨ Нажми на кнопку ниже, чтобы открыть панель управления:", opts);
        } else {
            bot.sendMessage(chatId, "👋 Привет! Я бот для общения. Добавь меня в группу или жми на кнопки ниже:", {
                reply_markup: {
                    inline_keyboard: [
                        [{ text: "🖥 Мой профиль", web_app: { url: url } }]
                    ]
                }
            });
        }
    });

    // /mybirthday DD.MM или DD.MM.YYYY
    bot.onText(/^\/mybirthday\s+(\d{2}\.\d{2}(?:\.\d{4})?)$/, async (msg, match) => {
        const chatId = msg.chat.id;
        const { userId, user } = getSenderData(msg);
        const birthday = match[1];
        deleteMsg(chatId, msg.message_id);

        const { setBirthday } = require('../database');
        const success = await setBirthday(chatId, userId, birthday);

        if (success) {
            sendTimedMessage(chatId, `✅ <b>${escapeHTML(getUserName(user))}</b>, я запомнила! Твой день рождения <code>${birthday}</code>. Обязательно поздравлю! 🥳`, 15000, { parse_mode: 'HTML' });
        } else {
            sendTimedMessage(chatId, `❌ Ошибка при сохранении даты рождения.`, 10000);
        }
    });

    // /bio <текст>
    bot.onText(/^\/bio\s+(.+)$/, async (msg, match) => {
        const chatId = msg.chat.id;
        const { userId, user } = getSenderData(msg);
        let bio = match[1].trim();
        deleteMsg(chatId, msg.message_id);

        if (bio.length > 100) bio = bio.substring(0, 97) + '...';

        const { setBio } = require('../database');
        const success = await setBio(chatId, userId, bio);

        if (success) {
            sendTimedMessage(chatId, `✅ <b>${escapeHTML(getUserName(user))}</b>, твоё био обновлено! Проверь его в <code>/me</code>.`, 15000, { parse_mode: 'HTML' });
        } else {
            sendTimedMessage(chatId, `❌ Ошибка при сохранении био.`, 10000);
        }
    });

    // /daily (Ежедневный бонус)
    bot.onText(/^\/daily$/, async (msg) => {
        const chatId = msg.chat.id;
        const { userId, user } = getSenderData(msg);
        deleteMsg(chatId, msg.message_id);

        try {
            const result = await claimDailyBonus(chatId, userId);
            if (!result.success) {
                if (result.timeRemaining) {
                    sendTimedMessage(chatId, `⏳ <b>${escapeHTML(getUserName(user))}</b>, твой следующий бонус будет доступен через <code>${result.timeRemaining.hours} ч. ${result.timeRemaining.minutes} мин.</code>`, 30000, { parse_mode: 'HTML' });
                } else {
                    sendTimedMessage(chatId, `❌ Ошибка: ${result.message}`, 10000, { parse_mode: 'HTML' });
                }
                return;
            }

            let msgText = `🎁 <b>ЕЖЕДНЕВНЫЙ БОНУС</b>\n━━━━━━━━━━━━━━━━━━\n` +
                          `👤 <code>${escapeHTML(getUserName(user))}</code> открыл сундук и получил:\n` +
                          `✨ <b>+${result.bonusXp} XP</b>\n`;

            if (result.isRepGained) {
                msgText += `🍪 <b>+1 Печеньку (Репутация)!</b> Ого, повезло!\n`;
            }

            if (result.levelUp) {
                msgText += `🎉 <b>НОВЫЙ УРОВЕНЬ!</b> Теперь ты <b>${result.newLevel}</b> ур.\n`;
            }
            msgText += `━━━━━━━━━━━━━━━━━━\n<i>Возвращайся завтра за новой наградой!</i>`;

            bot.sendMessage(chatId, msgText, { parse_mode: 'HTML' });
        } catch (error) {
            console.error('Ошибка в /daily:', error);
            sendTimedMessage(chatId, `❌ Ошибка при получении бонуса.`, 10000);
        }
    });

    // /linkfilter [on|off] (Управление фильтром ссылок)
    bot.onText(/^\/linkfilter(?:\s+(on|off))?$/, async (msg, match) => {
        const chatId = msg.chat.id;
        const { userId } = getSenderData(msg);
        deleteMsg(chatId, msg.message_id);

        // Проверяем, что пользователь — админ
        const adminCheck = await isAdmin(chatId, userId);
        if (!adminCheck) {
            return sendTimedMessage(chatId, '🚫 <b>Эта команда только для администраторов.</b>', 10000, { parse_mode: 'HTML' });
        }

        const arg = match[1]; // 'on', 'off', или undefined

        if (!arg) {
            // Показываем текущий статус
            const settings = await getChatSettings(chatId);
            const status = settings.link_filter_enabled ? '🔴 Включён (ссылки запрещены для всех, кроме админов)' : '🟢 Выключен (все могут отправлять ссылки)';
            return sendTimedMessage(chatId,
                `🔗 <b>Фильтр Telegram-ссылок</b>\n` +
                `━━━━━━━━━━━━━━━━━━\n` +
                `Статус: ${status}\n\n` +
                `Для изменения:\n` +
                `<code>/linkfilter on</code> — запретить ссылки\n` +
                `<code>/linkfilter off</code> — разрешить ссылки`,
                30000, { parse_mode: 'HTML' });
        }

        const enable = arg === 'on';
        const success = await updateChatSettings(chatId, { link_filter_enabled: enable });

        if (success) {
            const statusMsg = enable
                ? '🔴 <b>Фильтр ссылок включён.</b> Теперь обычные пользователи не могут отправлять t.me ссылки.'
                : '🟢 <b>Фильтр ссылок выключен.</b> Теперь все могут отправлять t.me ссылки.';
            sendTimedMessage(chatId, statusMsg, 20000, { parse_mode: 'HTML' });
        } else {
            sendTimedMessage(chatId, '❌ Ошибка при обновлении настроек.', 10000);
        }
    });

    // <help>в /help добавим упоминание о /linkfilter - обновляем текст /help


    // /me (Профиль)
    bot.onText(/^\/me(?:\s+(.+))?$/, async (msg, match) => {
        const chatId = msg.chat.id;
        const requester = getSenderData(msg);
        let userId = requester.userId;
        let targetUser = requester.user;

        deleteMsg(chatId, msg.message_id);

        let processMsgId = null;
        try {
            const processMsg = await bot.sendMessage(chatId, "⏳ <i>Загрузка профиля...</i>", { parse_mode: 'HTML' });
            processMsgId = processMsg.message_id;
        } catch (e) { }

        if (msg.reply_to_message) {
            const replyInfo = getSenderData(msg.reply_to_message);
            userId = replyInfo.userId; targetUser = replyInfo.user;
        } else if (match[1]) {
            const arg = match[1].trim();
            if (/^\d+$/.test(arg)) {
                userId = parseInt(arg); targetUser = { id: userId, first_name: `User ${userId}` };
            } else if (arg.startsWith('@')) {
                const { data } = await supabase.from('users').select('*').eq('chat_id', chatId).ilike('username', arg.substring(1)).maybeSingle();
                if (data) { userId = data.user_id; targetUser = data; }
                else {
                    if (processMsgId) deleteMsg(chatId, processMsgId);
                    return sendTimedMessage(chatId, `❌ Пользователь ${escapeHTML(arg)} не найден в базе.`);
                }
            }
        }

        const user = await getUser(chatId, userId, targetUser);
        if (!user) {
            if (processMsgId) deleteMsg(chatId, processMsgId);
            return;
        }
        
        if (processMsgId) deleteMsg(chatId, processMsgId);

        const nextXp = getNextLevelXp(user.level);
        const prevLevelXp = user.level === 1 ? 0 : getNextLevelXp(user.level - 1);
        const levelRange = nextXp - prevLevelXp;
        const currentProgress = user.xp - prevLevelXp;
        
        // Рисуем стилизованный прогресс-бар: [██████░░░░]
        const progressPercent = Math.max(0, Math.min(100, (currentProgress / levelRange) * 100));
        const filledBars = Math.floor(progressPercent / 10);
        const emptyBars = 10 - filledBars;
        const progressBar = `[${'█'.repeat(filledBars)}${'░'.repeat(emptyBars)}]`;

        let roleText = userId === ANONYMOUS_ADMIN_ID ? "👻 Анонимный Админ" : (await isAdmin(chatId, userId) ? "🛡 Администратор" : "👤 Пользователь");
        
        // Определение ранга
        const getRank = (level) => {
            if (level >= 50) return "🌌 Легенда";
            if (level >= 30) return "💎 Элита";
            if (level >= 20) return "🔥 Мастер";
            if (level >= 10) return "🌟 Опытный";
            if (level >= 5) return "🌱 Активный";
            return "👶 Новичок";
        };

        const isSelf = userId === requester.userId;
        const headerTitle = isSelf ? "МОЙ ПРОФИЛЬ" : "ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ";
        
        let message = `💠 <b>${headerTitle}</b>\n` +
            `━━━━━━━━━━━━━━━━━━\n` +
            `📝 <b>Имя:</b> <code>${escapeHTML(getUserName(targetUser))}</code>\n` +
            `🏅 <b>Ранг:</b> <i>${getRank(user.level)}</i>\n` +
            `🎖 <b>Роль:</b> <i>${roleText}</i>\n`;

        if (user.birthday) {
            message += `🎂 <b>День рождения:</b> <code>${user.birthday}</code>\n`;
        }

        if (user.bio) {
            message += `💬 <b>О себе:</b> <i>${escapeHTML(user.bio)}</i>\n`;
        }

        message += `━━━━━━━━━━━━━━━━━━\n` +
            `🌟 <b>Уровень:</b> <b>${user.level}</b>\n` +
            `📊 <b>Прогресс:</b> <code>${progressBar}</code> ${Math.floor(progressPercent)}%\n` +
            `✨ <b>Опыт:</b> <code>${user.xp.toLocaleString()} / ${nextXp.toLocaleString()} XP</code>\n` +
            `🍪 <b>Печеньки:</b> <code>${user.reputation.toLocaleString()} шт.</code>\n`;
            
        if (user.warns > 0) {
            message += `⚠️ <b>Предупреждения:</b> <code>${user.warns} / 3</code>\n`;
        }
            
        message += `━━━━━━━━━━━━━━━━━━\n` +
            `<i>До следующего уровня осталось ${nextXp - user.xp} XP</i>`;

        sendTimedMessage(chatId, message, 60000, { parse_mode: 'HTML' });
    });

    // /top (Рейтинг)
    bot.onText(/^\/top$/, async (msg) => {
        const chatId = msg.chat.id;
        deleteMsg(chatId, msg.message_id);

        let processMsgId = null;
        try {
            const processMsg = await bot.sendMessage(chatId, "⏳ <i>Собираю статистику...</i>", { parse_mode: 'HTML' });
            processMsgId = processMsg.message_id;
        } catch (e) { }

        const { data: users } = await supabase.from('users').select('*').eq('chat_id', chatId).order('level', { ascending: false }).order('xp', { ascending: false }).limit(10);
        
        if (processMsgId) deleteMsg(chatId, processMsgId);

        if (!users || users.length === 0) return sendTimedMessage(chatId, '📭 В этом чате пока нет активности.');
        
        let text = '🏆 <b>ТОП-10 АКТИВНЫХ УЧАСТНИКОВ</b>\n━━━━━━━━━━━━━━━━━━\n\n';
        
        const medals = ['🥇', '🥈', '🥉'];
        
        users.forEach((u, i) => {
            const placeIndex = i < 3 ? medals[i] : `<b>${i + 1}.</b>`;
            const warnsMarker = u.warns > 0 ? '⚠️' : '';
            text += `${placeIndex} <code>${escapeHTML(getUserName(u))}</code>\n   └ <b>Ур. ${u.level}</b> | 🍪 ${u.reputation} ${warnsMarker}\n`;
        });
        
        text += '\n━━━━━━━━━━━━━━━━━━\n<i>Чем больше общаетесь, тем выше уровень!</i>';
        sendTimedMessage(chatId, text, 60000, { parse_mode: 'HTML' });
    });

    // /shop
    bot.onText(/^\/shop$/, async (msg) => {
        const chatId = msg.chat.id;
        const helpText = `🏪 <b>МАГАЗИН ПЕЧЕНЕК</b> 🍪\n` +
            `━━━━━━━━━━━━━━━━━━\n\n` +
            `🥇 <b>Купить 1 уровень</b>\n` +
            `└ Стоимость: <code>500 🍪</code>\n` +
            `└ Команда: <code>/buy 1</code>\n\n` +
            `🧹 <b>Снять все предупреждения</b> (варны)\n` +
            `└ Стоимость: <code>200 🍪</code>\n` +
            `└ Команда: <code>/buy 2</code>\n\n` +
            `━━━━━━━━━━━━━━━━━━\n` +
            `<i>Печеньки можно зарабатывать, получая "спасибо" или "+" в ответ на сообщения!</i>`;
        bot.sendMessage(chatId, helpText, { parse_mode: 'HTML' });
    });

    // /buy
    bot.onText(/^\/buy (\d+)$/, async (msg, match) => {
        const chatId = msg.chat.id;
        const { userId, user: senderInfo } = getSenderData(msg);
        const itemId = parseInt(match[1]);
        deleteMsg(chatId, msg.message_id);

        const user = await getUser(chatId, userId, senderInfo);
        if (!user) return;

        if (itemId === 1) { // Уровень
            const cost = 500;
            if (user.reputation < cost) return sendTimedMessage(chatId, `❌ Недостаточно печенек! (Нужно ${cost})`, 15000);
            await updateUser(user.id, { level: user.level + 1, xp: 0, reputation: user.reputation - cost });
            sendTimedMessage(chatId, `✨ ${getUserName(senderInfo)} купил уровень! Теперь ты ${user.level + 1} ур.`, 30000);
        } else if (itemId === 2) { // Варны
            const cost = 200;
            if (user.reputation < cost) return sendTimedMessage(chatId, `❌ Недостаточно печенек!`, 15000);
            await updateUser(user.id, { warns: 0, reputation: user.reputation - cost });
            sendTimedMessage(chatId, `✅ ${getUserName(senderInfo)}, варны сняты!`, 30000);
        }
    });

    // /give
    bot.onText(/^\/give (\d+)$/, async (msg, match) => {
        const chatId = msg.chat.id;
        const { userId: senderId, user: senderInfo } = getSenderData(msg);
        const amount = parseInt(match[1]);
        deleteMsg(chatId, msg.message_id);

        if (amount <= 0 || !msg.reply_to_message) return sendTimedMessage(chatId, `❌ Неверная сумма или нет реплая!`, 15000);
        const { userId: receiverId, user: receiverInfo } = getSenderData(msg.reply_to_message);
        if (senderId === receiverId) return sendTimedMessage(chatId, `❌ Себе нельзя!`, 10000);

        const sender = await getUser(chatId, senderId, senderInfo);
        const receiver = await getUser(chatId, receiverId, receiverInfo);
        if (!sender || !receiver || sender.reputation < amount) return sendTimedMessage(chatId, `❌ Недостаточно 🍪`, 15000);

        await updateUser(sender.id, { reputation: sender.reputation - amount });
        await updateUser(receiver.id, { reputation: receiver.reputation + amount });
        sendTimedMessage(chatId, `🍪 ${getUserName(senderInfo)} передал ${amount} печенек ${getUserName(receiverInfo)}!`, 60000);
    });

    // /kto
    bot.onText(/\/kto (.+)/, async (msg, match) => {
        const chatId = msg.chat.id;
        
        // Проверка общего кулдауна на весь чат (60 секунд)
        const now = Date.now();
        const cooldown = 60000;
        if (!commandCooldowns[chatId]) commandCooldowns[chatId] = {};
        
        if (commandCooldowns[chatId].kto && (now - commandCooldowns[chatId].kto < cooldown)) {
            const remaining = Math.ceil((cooldown - (now - commandCooldowns[chatId].kto)) / 1000);
            deleteMsg(chatId, msg.message_id);
            return sendTimedMessage(chatId, `⏳ Команда /kto на перезарядке! Подождите еще <code>${remaining} сек.</code> для всех.`, 10000, { parse_mode: 'HTML' });
        }

        commandCooldowns[chatId].kto = now;
        deleteMsg(chatId, msg.message_id);
        
        const { data: users } = await supabase.from('users').select('*').eq('chat_id', chatId);
        if (!users || users.length === 0) return;
        
        const randomUser = users[Math.floor(Math.random() * users.length)];
        sendTimedMessage(chatId, `🤔 Я думаю, что <b>${match[1]}</b> — это <b>${escapeHTML(getUserName(randomUser))}</b>!`, 60000, { parse_mode: 'HTML' });
    });

    // /notes (Досье ИИ)
    bot.onText(/^\/notes(?:\s+(.+))?$/, async (msg, match) => {
        const chatId = msg.chat.id;
        const requester = getSenderData(msg);
        let userId = requester.userId;
        let targetUser = requester.user;

        deleteMsg(chatId, msg.message_id);

        // Определяем цель (реплай или упоминание)
        if (msg.reply_to_message) {
            const replyInfo = getSenderData(msg.reply_to_message);
            userId = replyInfo.userId; targetUser = replyInfo.user;
        } else if (match[1]) {
            const arg = match[1].trim();
            if (arg.startsWith('@')) {
                const { data } = await supabase.from('users').select('*').eq('chat_id', chatId).ilike('username', arg.substring(1)).maybeSingle();
                if (data) { userId = data.user_id; targetUser = data; }
                else return sendTimedMessage(chatId, `❌ Пользователь ${escapeHTML(arg)} не найден в базе.`);
            }
        }

        const user = await getUser(chatId, userId, targetUser);
        if (!user) return;

        const userName = getUserName(targetUser);
        const notes = user.ai_notes || 'Пока ничего особенного не приметила... Веди себя интереснее!';
        
        const response = `🕵️‍♀️ <b>ДОСЬЕ ИИ: ${escapeHTML(userName.toUpperCase())}</b>\n` +
                         `━━━━━━━━━━━━━━━━━━\n\n` +
                         `<i>"${escapeHTML(notes)}"</i>\n\n` +
                         `━━━━━━━━━━━━━━━━━━\n` +
                         `<i>Я всё записываю...</i>`;

        sendTimedMessage(chatId, response, 60000, { parse_mode: 'HTML' });
    });



}

module.exports = { registerCommands };
