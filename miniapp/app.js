const tg = window.Telegram.WebApp;

// Инициализация
tg.expand(); // Расширяем на весь экран
const user = tg.initDataUnsafe?.user;

// DOM Elements
const navItems = document.querySelectorAll('.nav-item');
const tabContents = document.querySelectorAll('.tab-content');
const toastContainer = document.getElementById('toast-container');
const lbContainer = document.getElementById('leaderboard-container');
const lbSwitchBtns = document.querySelectorAll('.switch-btn');
const badWordsList = document.getElementById('bad-words-list');
const wordInput = document.getElementById('blacklist-input');
const btnAddWord = document.getElementById('btn-add-word');
const navSettings = document.getElementById('nav-settings');

let currentChatId = null;
let viewAsUserId = null; // Если зашли от лица канала
let isAdminUser = false;

// Tabs Logic
navItems.forEach(item => {
    item.addEventListener('click', (e) => {
        e.preventDefault();
        const targetId = item.getAttribute('data-tab');
        
        // Remove active class
        navItems.forEach(nav => nav.classList.remove('active'));
        tabContents.forEach(tab => tab.classList.remove('active'));
        
        // Add active class
        item.classList.add('active');
        document.getElementById(targetId).classList.add('active');
        
        // Telegram haptic feedback
        tg.HapticFeedback.selectionChanged();
    });
});

// Toast Notification System
function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    
    let icon = 'fa-info-circle';
    if (type === 'success') icon = 'fa-check-circle text-green';
    if (type === 'error') icon = 'fa-exclamation-circle text-red';
    
    toast.innerHTML = `<i class="fas ${icon}"></i> <span>${message}</span>`;
    toastContainer.appendChild(toast);
    
    // Telegram haptic
    if(type === 'success') tg.HapticFeedback.notificationOccurred('success');
    else if(type === 'error') tg.HapticFeedback.notificationOccurred('error');
    else tg.HapticFeedback.impactOccurred('light');

    setTimeout(() => {
        toast.remove();
    }, 3000);
}

// Загрузка профиля пользователя
async function loadUserProfile() {
    // Определяем какой ID использовать (свой или канала)
    const effectiveUserId = viewAsUserId || user?.id;

    if (effectiveUserId) {
        // Если смотрим чужой профиль (канал), подставим базовое имя из URL или потом из БД
        if (viewAsUserId) {
             document.getElementById('user-name').textContent = "Загрузка профиля...";
        } else if (user) {
             document.getElementById('user-name').textContent = user.first_name + (user.last_name ? ` ${user.last_name}` : '');
             document.getElementById('user-id').textContent = `@${user.username || user.id}`;
             if (user.photo_url) document.getElementById('user-avatar').src = user.photo_url;
        }
        
        try {
            const res = await fetch(`/api/profile?user_id=${effectiveUserId}${currentChatId ? `&chat_id=${currentChatId}` : ''}`, {
                headers: { 'x-tg-init-data': tg.initData }
            });
            if (res.ok) {
                const data = await res.json();
                
                // Обновляем имя и фото (особенно важно для каналов)
                document.getElementById('user-name').textContent = (data.user_id < 0 ? '📢 ' : '') + (data.first_name || 'Инкогнито');
                document.getElementById('user-id').textContent = data.username ? `@${data.username}` : `ID: ${data.user_id}`;
                if (data.photo_url) document.getElementById('user-avatar').src = data.photo_url;

                document.getElementById('user-level').textContent = data.level || 1;
                document.getElementById('user-rep').textContent = data.reputation || 0;
                document.getElementById('user-warns').textContent = data.warns || 0;
                
                const nextXp = 50 * data.level * data.level + 50 * data.level;
                const xpPercent = Math.min((data.xp / nextXp) * 100, 100);
                document.getElementById('xp-text').textContent = `${data.xp} / ${nextXp} XP`;
                document.getElementById('xp-bar').style.width = `${xpPercent}%`;

                // Проверка на админа 
                if (data.is_admin) {
                    isAdminUser = true;
                    navSettings.style.display = 'flex';
                    loadBadWords();
                }
            } else { showToast('Не удалось загрузить статистику', 'error'); }
        } catch (e) {
            console.error(e);
        }
    } else {
        document.getElementById('user-name').textContent = 'Локальный Тест';
        document.getElementById('user-id').textContent = 'Запущено не в TG';
        document.getElementById('user-level').textContent = 14;
        document.getElementById('user-rep').textContent = 152;
        document.getElementById('user-warns').textContent = 0;
        document.getElementById('xp-text').textContent = '350 / 800 XP';
        document.getElementById('xp-bar').style.width = '45%';
    }
}

// Загрузка лидерборда
async function loadLeaderboard(type = 'level') {
    lbContainer.innerHTML = '<div class="loading-spinner"><i class="fas fa-spinner fa-spin"></i> Загрузка...</div>';
    
    // Подсветка кнопок
    lbSwitchBtns.forEach(btn => {
        if (btn.getAttribute('data-target') === `top-${type === 'reputation' ? 'rep' : 'level'}`) btn.classList.add('active');
        else btn.classList.remove('active');
    });

    if (!currentChatId) {
        lbContainer.innerHTML = '<div style="text-align:center;color:var(--hint-color);margin-top:20px;">Запустите приложение из группы</div>';
        return;
    }

    try {
        const res = await fetch(`/api/leaderboard?chat_id=${currentChatId}&type=${type}`, {
            headers: { 'x-tg-init-data': tg.initData }
        });
        if (res.ok) {
            const data = await res.json();
            lbContainer.innerHTML = '';
            if (data.length === 0) {
                lbContainer.innerHTML = '<div style="text-align:center;color:var(--hint-color);margin-top:20px;">Нет данных</div>';
                return;
            }
            
            data.forEach((user, index) => {
                let rankClass = '';
                if (index === 0) rankClass = 'gold';
                else if (index === 1) rankClass = 'silver';
                else if (index === 2) rankClass = 'bronze';
                
                const valText = type === 'level' ? `${user.level} ур.` : `${user.reputation} 🍪`;
                
                const avatar = user.photo_url || `https://ui-avatars.com/api/?name=${encodeURIComponent(user.first_name)}&background=random&color=fff`;
                const isChannel = user.user_id < 0;
                const nameDisplay = isChannel ? `<i class="fas fa-bullhorn" style="font-size: 0.8em; opacity: 0.7;"></i> ${user.first_name}` : user.first_name;

                lbContainer.innerHTML += `
                    <div class="lb-item">
                        <div class="lb-rank ${rankClass}">${index + 1}</div>
                        <img class="lb-avatar" src="${avatar}" alt="${user.first_name}">
                        <div class="lb-info">
                            <div class="lb-name">${nameDisplay}</div>
                            <div class="lb-val"><i class="fas fa-${type === 'level' ? 'star text-yellow' : 'heart text-red'}"></i> ${valText}</div>
                        </div>
                    </div>
                `;
            });
        }
    } catch (e) {
        lbContainer.innerHTML = '<div style="text-align:center;color:var(--accent-red);margin-top:20px;">Ошибка загрузки</div>';
    }
}

lbSwitchBtns.forEach(btn => {
    btn.addEventListener('click', () => {
        const isRep = btn.getAttribute('data-target') === 'top-rep';
        loadLeaderboard(isRep ? 'reputation' : 'level');
    });
});

// Aternos Logic
const startBtn = document.getElementById('btn-start-server');
const dot = document.getElementById('status-dot');
const statusTxt = document.getElementById('server-status');

async function updateAternosStatus() {
    try {
        const res = await fetch(`/api/aternos/status`, {
            headers: { 'x-tg-init-data': tg.initData }
        });
        if (res.ok) {
            const data = await res.json();
            statusTxt.textContent = data.status === 'ONLINE' ? 'В сети' : (data.status === 'STARTING' ? 'Запускается...' : 'Оффлайн');
            dot.className = `point ${data.status === 'ONLINE' ? 'green' : (data.status === 'STARTING' ? 'orange' : 'red')}`;
            document.getElementById('server-players').textContent = data.players || '0 / 0';
            if (data.ip) document.getElementById('server-ip').textContent = data.ip;
            
            if (data.status === 'STARTING') {
                startBtn.disabled = true;
                startBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Запуск...';
            } else if (data.status === 'ONLINE') {
                startBtn.disabled = true;
                startBtn.innerHTML = '<i class="fas fa-check"></i> Онлайн';
            } else {
                startBtn.disabled = false;
                startBtn.innerHTML = '<i class="fas fa-power-off"></i> Запустить сервер';
            }
        }
    } catch (e) { console.error(e); }
}

startBtn.addEventListener('click', async () => {
    dot.className = 'point orange';
    statusTxt.textContent = 'Отправка...';
    startBtn.disabled = true;
    startBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Обработка...';
    
    try {
        const res = await fetch(`/api/aternos/start`, {
            method: 'POST',
            headers: { 'x-tg-init-data': tg.initData }
        });
        const data = await res.json();
        if (data.success) {
            showToast('Сервер запускается!', 'success');
            setTimeout(updateAternosStatus, 2000);
        } else {
            showToast('Ошибка запуска', 'error');
            updateAternosStatus();
        }
    } catch (e) { 
        showToast('Ошибка сети', 'error');
        updateAternosStatus();
    }
});

// Обновляем статус каждые 30 секунд при открытой вкладке
setInterval(() => {
    if (document.getElementById('tab-aternos').classList.contains('active')) {
        updateAternosStatus();
    }
}, 30000);

// Логика Админ-панели (Плохие слова)
async function loadBadWords() {
    if (!currentChatId || !isAdminUser) return;
    try {
        const res = await fetch(`/api/badwords?chat_id=${currentChatId}`, { headers: { 'x-tg-init-data': tg.initData } });
        if (res.ok) {
            const data = await res.json();
            renderBadWords(data);
        }
    } catch (e) { console.error('Error loading bad words', e); }
}

function renderBadWords(words) {
    badWordsList.innerHTML = '';
    words.forEach(word => {
        const li = document.createElement('li');
        li.className = 'word-chip';
        li.innerHTML = `<span>${word}</span> <i class="fas fa-times" onclick="removeBadWord('${word}')"></i>`;
        badWordsList.appendChild(li);
    });
}

async function addBadWord() {
    const word = wordInput.value.trim().toLowerCase();
    if (!word || !currentChatId) return;
    
    try {
        const res = await fetch(`/api/badwords`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'x-tg-init-data': tg.initData },
            body: JSON.stringify({ chat_id: currentChatId, word, action: 'add' })
        });
        if (res.ok) {
            wordInput.value = '';
            loadBadWords();
            showToast('Слово добавлено', 'success');
        } else { showToast('Ошибка добавления', 'error'); }
    } catch (e) { console.error(e); }
}

window.removeBadWord = async function(word) {
    if (!currentChatId) return;
    try {
        const res = await fetch(`/api/badwords`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'x-tg-init-data': tg.initData },
            body: JSON.stringify({ chat_id: currentChatId, word, action: 'remove' })
        });
        if (res.ok) {
            loadBadWords();
            showToast('Слово удалено', 'success');
        }
    } catch (e) { console.error(e); }
}

btnAddWord.addEventListener('click', addBadWord);

// Инициализация при старте
document.addEventListener('DOMContentLoaded', () => {
    // Получаем chat_id из URL (он передается туда из WebApp) или из initData
    const urlParams = new URLSearchParams(window.location.search);
    currentChatId = urlParams.get('chat_id') || (tg.initDataUnsafe?.chat?.id) || null;
    viewAsUserId = urlParams.get('as_user') || null;

    // Если открыто из группы, сразу идем на вкладку рейтинга
    if (currentChatId) {
        navItems.forEach(n => n.classList.remove('active'));
        tabContents.forEach(t => t.classList.remove('active'));
        
        document.querySelector('[data-tab="tab-leaderboard"]').classList.add('active');
        document.getElementById('tab-leaderboard').classList.add('active');
        loadLeaderboard('level');
    }

    loadUserProfile();
    tg.ready();
});
