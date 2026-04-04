require('dotenv').config();
const { createClient } = require('@supabase/supabase-js');

const supabaseUrl = process.env.SUPABASE_URL;
const supabaseKey = process.env.SUPABASE_KEY;
const supabase = createClient(supabaseUrl, supabaseKey);

async function check() {
    const { data, error } = await supabase
        .from('bot_knowledge')
        .select('*')
        .ilike('fact', '%Among Us%');
    
    if (error) {
        console.error('Error:', error);
        return;
    }
    console.log('Facts found with "Among Us":');
    console.log(JSON.stringify(data, null, 2));
}

check();
