const crypto = require('crypto');

const QDRANT_URL = process.env.QDRANT_URL ? process.env.QDRANT_URL.trim().replace(/\/$/, '') : '';
const QDRANT_API_KEY = process.env.QDRANT_API_KEY ? process.env.QDRANT_API_KEY.trim() : '';
const QDRANT_COLLECTION = process.env.QDRANT_COLLECTION ? process.env.QDRANT_COLLECTION.trim() : 'Nika';
const QDRANT_DISTANCE = process.env.QDRANT_DISTANCE ? process.env.QDRANT_DISTANCE.trim() : 'Cosine';
const QDRANT_VECTOR_SIZE = Number(process.env.QDRANT_VECTOR_SIZE || 1536);

let collectionReady = false;
let payloadIndexesReady = false;

function qdrantEnabled() {
    return Boolean(QDRANT_URL);
}

function buildKnowledgeId(chatId, fingerprint) {
    const base = `${chatId}::${fingerprint}`;
    const hex = crypto.createHash('sha256').update(base).digest('hex');
    // Build a deterministic UUIDv5-like string from hash (valid version/variant bits)
    const hex32 = hex.slice(0, 32);
    const timeHiAndVersion = (parseInt(hex32.slice(12, 16), 16) & 0x0fff) | 0x5000;
    const clockSeqHiAndReserved = (parseInt(hex32.slice(16, 20), 16) & 0x3fff) | 0x8000;
    const pad4 = (value) => value.toString(16).padStart(4, '0');
    return [
        hex32.slice(0, 8),
        hex32.slice(8, 12),
        pad4(timeHiAndVersion),
        pad4(clockSeqHiAndReserved),
        hex32.slice(20, 32)
    ].join('-');
}

async function qdrantFetch(path, options = {}) {
    const url = `${QDRANT_URL}${path}`;
    const headers = {
        'Content-Type': 'application/json'
    };
    if (QDRANT_API_KEY) headers['api-key'] = QDRANT_API_KEY;

    const res = await fetch(url, {
        method: options.method || 'POST',
        headers,
        body: options.body ? JSON.stringify(options.body) : undefined
    });

    if (!res.ok) {
        const text = await res.text().catch(() => '');
        const err = new Error(`Qdrant ${res.status}: ${text || res.statusText}`);
        err.status = res.status;
        throw err;
    }

    return res.json();
}

async function ensureCollection() {
    if (!qdrantEnabled() || collectionReady) return;
    try {
        await qdrantFetch(`/collections/${QDRANT_COLLECTION}`, { method: 'GET' });
        collectionReady = true;
        await ensurePayloadIndexes();
        return;
    } catch (e) {
        if (e.status !== 404) throw e;
    }

    await qdrantFetch(`/collections/${QDRANT_COLLECTION}`, {
        method: 'PUT',
        body: {
            vectors: {
                size: QDRANT_VECTOR_SIZE,
                distance: QDRANT_DISTANCE
            }
        }
    });
    collectionReady = true;
    await ensurePayloadIndexes();
}

async function ensurePayloadIndexes() {
    if (!qdrantEnabled() || payloadIndexesReady) return;
    const indexes = [
        { field_name: 'chat_id', field_schema: 'integer' },
        { field_name: 'status', field_schema: 'keyword' },
        { field_name: 'confidence', field_schema: 'float' },
        { field_name: 'last_seen_at', field_schema: 'datetime' }
    ];

    for (const index of indexes) {
        try {
            await qdrantFetch(`/collections/${QDRANT_COLLECTION}/index`, {
                method: 'PUT',
                body: index
            });
        } catch (error) {
            // Ignore if already exists; surface other errors
            if (String(error.message || '').includes('already exists')) continue;
            if (error.status === 409) continue;
            throw error;
        }
    }

    payloadIndexesReady = true;
}

async function upsertPoint(points) {
    if (!qdrantEnabled()) return null;
    await ensureCollection();
    return qdrantFetch(`/collections/${QDRANT_COLLECTION}/points?wait=true`, {
        body: { points }
    });
}

async function getPoint(id, withVector = false) {
    if (!qdrantEnabled()) return null;
    await ensureCollection();
    try {
        const res = await qdrantFetch(`/collections/${QDRANT_COLLECTION}/points/get`, {
            body: { ids: [id], with_payload: true, with_vector: withVector }
        });
        return res?.result?.[0] || null;
    } catch (error) {
        if (error.status === 404) return null;
        throw error;
    }
}

async function deletePoint(id) {
    if (!qdrantEnabled()) return null;
    await ensureCollection();
    return qdrantFetch(`/collections/${QDRANT_COLLECTION}/points/delete?wait=true`, {
        body: { points: [id] }
    });
}

async function searchPoints(vector, limit = 5, filter = null) {
    if (!qdrantEnabled()) return [];
    await ensureCollection();
    const res = await qdrantFetch(`/collections/${QDRANT_COLLECTION}/points/search`, {
        body: {
            vector,
            limit,
            with_payload: true,
            filter: filter || undefined
        }
    });
    return res?.result || [];
}

async function scrollPoints(filter = null, limit = 50, offset = null, withVector = false) {
    if (!qdrantEnabled()) return { points: [], nextPage: null };
    await ensureCollection();
    const res = await qdrantFetch(`/collections/${QDRANT_COLLECTION}/points/scroll`, {
        body: {
            filter: filter || undefined,
            limit,
            offset,
            with_payload: true,
            with_vector: withVector
        }
    });
    return {
        points: res?.result?.points || [],
        nextPage: res?.result?.next_page_offset || null
    };
}

module.exports = {
    qdrantEnabled,
    buildKnowledgeId,
    ensureCollection,
    ensurePayloadIndexes,
    upsertPoint,
    getPoint,
    deletePoint,
    searchPoints,
    scrollPoints
};
