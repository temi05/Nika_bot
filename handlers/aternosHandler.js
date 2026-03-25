let Aternos;
try {
    Aternos = require('aternos-api');
} catch (e) {
    console.warn('⚠️ Библиотека aternos-api не установлена. Выполни: npm install aternos-api');
}

class AternosAPI {
    constructor(session) {
        this.session = session;
        this.client = Aternos && session ? new Aternos.Client(session) : null;
    }

    async getStatus() {
        if (!this.client) return { status: 'OFFLINE', players: '0/0' };
        try {
            const server = await this.client.getServer(); // По умолчанию берет первый сервер
            return {
                status: server.status.toUpperCase(),
                players: `${server.players}/${server.maxPlayers}`,
                ip: server.ip
            };
        } catch (e) {
            console.error('Aternos Status Error:', e.message);
            return { status: 'ERROR' };
        }
    }

    async startServer() {
        if (!this.client) throw new Error('Aternos Client not initialized. Check session cookie.');
        try {
            const server = await this.client.getServer();
            await server.start();
            return true;
        } catch (e) {
            console.error('Aternos Start Error:', e.message);
            throw e;
        }
    }
}

module.exports = AternosAPI;
