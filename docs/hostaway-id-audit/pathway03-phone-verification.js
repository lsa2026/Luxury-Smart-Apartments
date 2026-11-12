// Read-only snapshot of node 103D, workflow sNwA2n4FfnAMqFDW.
// Active version 9238ff39-db57-4497-b307-69be839298a9, reviewed 2026-11-12.
// Isolated fixture only: no triggers, credentials, connections or production writes.
const previous=$('103 Prepare Recovery Work').item.json;
const input=$('101 Validate Booking and New Phone').first().json;
const key=String(previous.state?.reservation_key||input.reservation_key||'');
const phone=String(previous.state?.phone||input.phone||'').replace(/\D/g,'');
let body=$json.body??$json;
if(typeof body==='string'){try{body=JSON.parse(body);}catch{body={};}}
const code=Number($json.statusCode||200);
const saved=!$json.error&&code>=200&&code<300&&body.status==='success'&&String(body.result?.id||'')===key&&String(body.result?.phone||'').replace(/\D/g,'')===phone;
const at=new Date().toISOString();
const reason='???? ????? ??? ??? ???????? ?? ??????? ????? '+key;
if(previous.state){
 const state=JSON.parse(JSON.stringify(previous.state));
 state.hostaway_phone_saved=saved;
 state.hostaway_phone_checked_at=at;
 state.errors=(state.errors||[]).filter(x=>!String(x).startsWith('???? ????? ??? ??? ???????? ?? ???????'));
 if(!saved)state.errors.push(reason);
 return [{json:{...previous,state},pairedItem:{item:0}}];
}
if(!saved)return [{json:{...previous,route:'error',error:reason,input:{reservation_key:key,phone}},pairedItem:{item:0}}];
return [{json:{...previous,result:{...(previous.result||{}),hostaway_phone_saved:true,hostaway_phone_checked_at:at}},pairedItem:{item:0}}];
