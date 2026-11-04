import 'dotenv/config';
import fs from 'node:fs';
import {createClient} from '@supabase/supabase-js';
const db=createClient(process.env.SUPABASE_URL,process.env.SUPABASE_SERVICE_ROLE_KEY);
const check=r=>{if(r.error)throw Error(r.error.message);return r;};
const rows=[];
for(let start=0;;start+=1000){const r=check(await db.from('places').select('content_id,title,is_active,needs_ai_summary').eq('content_type_id','38').order('content_id').range(start,start+999));rows.push(...r.data);if(r.data.length<1000)break;}
fs.mkdirSync('artifacts/shopping-exclusion',{recursive:true});
fs.writeFileSync('artifacts/shopping-exclusion/places.json',JSON.stringify(rows,null,2));
console.log(JSON.stringify({total:rows.length,active:rows.filter(x=>x.is_active).length,flagged:rows.filter(x=>x.needs_ai_summary).length,examples:rows.filter(x=>/코엑스/.test(x.title))}));
if(process.argv.includes('--apply')){
let changed=0;
for(let i=0;i<rows.length;i+=100){const ids=rows.slice(i,i+100).map(x=>x.content_id);const before=check(await db.from('ai_review_jobs').select('*').in('content_id',ids)).data;fs.writeFileSync(`artifacts/shopping-exclusion/jobs-${i}.json`,JSON.stringify(before,null,2));check(await db.from('places').update({needs_ai_summary:false}).eq('content_type_id','38').eq('needs_ai_summary',true).in('content_id',ids));check(await db.from('ai_review_jobs').update({attempt_count:2,last_error_code:'excluded_shopping_category',lease_token:null,leased_until:null}).in('content_id',ids));changed+=before.length;if(i%1000===0)console.log('processed',i+ids.length);}
const remaining=check(await db.from('places').select('content_id',{count:'exact',head:true}).eq('content_type_id','38').eq('needs_ai_summary',true));
console.log(JSON.stringify({jobsExcluded:changed,remainingFlagged:remaining.count}));
}
