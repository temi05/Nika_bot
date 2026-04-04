const { supabase } = require('./database');

async function findFact(chatId, query) {
    const { data, error } = await supabase
        .from('bot_knowledge')
        .select('*')
        .eq('chat_id', chatId)
        .ilike('fact', `%${query}%`);
    
    if (error) {
        console.error('Error:', error);
        return;
    }

    console.log(`Results for "${query}":`, data);
}

// Replace with actual chatId from logs if known, or search globally if allowed
// For now, I'll just check if "Among Us" exists ANYWHERE to see if it's a real fact.
findFact(-1002349071063, 'Among Us'); // Example ID from a typical group, but I need to find the real one.
// Let's just search all chats for "Among Us" to see if it's a common hallucination/saved fact.
async function searchAll(query) {
    const { data } = await supabase.from('bot_knowledge').select('*').ilike('fact', `%${query}%`);
    console.log('Global search results:', data);
}

searchAll('Among Us');
