const POLZA_API_KEY = 'pza_Ut5ahRtIFZSzj_jKezwdRvQMMebqZ1BI';

async function listModels() {
    try {
        const response = await fetch('https://polza.ai/api/v1/models', {
            headers: { 'Authorization': `Bearer ${POLZA_API_KEY}` }
        });
        const data = await response.json();
        console.log('--- AVAILABLE MODELS ---');
        data.data.forEach(m => {
            if (m.id.toLowerCase().includes('gemini')) {
                console.log(`ID: ${m.id}`);
            }
        });
        console.log('------------------------');
    } catch (e) {
        console.error('Error listing models:', e.message);
    }
}

listModels();
