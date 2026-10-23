/* Synthetic execution of the actual changed Code nodes. No network or credentials. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const ops=JSON.parse(fs.readFileSync(process.argv[2]||'docs/automation/morning-workflow-patch.json','utf8'));
const get=(i,name)=>{const op=ops[i].find(o=>o.nodeName===name||o.node?.name===name);return op.parameters?.jsCode||op.node?.parameters.jsCode;};
function run(code,input,refs={}){
 const items=(Array.isArray(input)?input:[input]).map(json=>({json}));
 const context={Date,Intl,encodeURIComponent,decodeURIComponent,unescape,
  $json:items[0].json,$input:{all:()=>items,first:()=>items[0]},$execution:{id:'synthetic'},
  $:name=>{if(!(name in refs))throw Error('Node was not executed: '+name);const json=refs[name];
   return {item:{json},first:()=>({json}),all:()=>[{json}],isExecuted:true};}};
 return JSON.parse(JSON.stringify(vm.runInNewContext('(function(){'+code+'})()',context)));
}
for(const group of Object.values(ops))for(const op of group){const js=op.parameters?.jsCode||op.node?.parameters?.jsCode;if(js)new vm.Script('(function(){'+js+'})()');}
const g={source:'morningstay',morning_stay_id:'00000000-0000-4000-8000-000000000001',
 source_event_id:'00000000-0000-4000-8000-000000000002',reservation_key:'morningstay:00000000-0000-4000-8000-000000000001',
 source_revision:1,state_fingerprint:'a'.repeat(64),payment_verified:true,status:'paid',listing_map_id:97001,
 listing_name:'Synthetic Apartment',timezone:'Asia/Riyadh',arrival_date:'2027-01-01',departure_date:'2027-01-01',
 starts_at:'2027-01-01T01:00:00+03:00',ends_at:'2027-01-01T11:00:00+03:00',
 check_in_time:'01:00',check_out_time:'11:00',source_updated_at:'2026-12-31T18:00:00Z',
 guest_name:'Synthetic Guest',guest_language:'ar',phone:'+16502530000',guest_email:'synthetic@example.test',
 event_type:'new',event_revision:1,__test_now:'2027-01-01T07:30:00Z',
 config:{production_enabled:true,test_mode:false}};
const normal=get('1','M03 Normalize Verified Morning Stay');
assert.equal(run(normal,g)[0].json.nights,0);
assert.throws(()=>run(normal,{...g,payment_verified:false}));
assert.throws(()=>run(normal,{...g,status:'review'}));
assert.throws(()=>run(normal,{...g,check_out_time:'12:00'}));
const templates=['status_new','status_modified','status_cancelled','house_rules','checkin_guide','reminder_checkout'].map(template_name=>({template_name,content_ar:'تعليمات {{guest_name}}',content_en:'Rules {{guest_name}}',content_fr:'Règles {{guest_name}}'}));
const rendered=run(get('1','10 Select Language + Render Messages'),templates,{
 '07 Canonical Claimed Reservation':g,'08 Get Apartment Info + Timezone':{timezone:'Asia/Riyadh'},
})[0].json;
assert.match(rendered.event_message_whatsapp,/11:00/);
assert.doesNotMatch(rendered.event_message_whatsapp,/ليال|غد|12:00|15:00/);
const plan=run(get('1','11 Plan Immediate Messages and Future Reminders'),rendered)[0].json;
assert.equal(plan.calendar_plan.immediate_checkout_reminder,false);
assert.equal(plan.calendar_plan.checkout_in_confirmation,true);
assert.equal(plan.calendar_plan.checkout_reminder_at,'2027-01-01T07:00:00.000Z');
const messages=run(get('1','19 Prepare Ordered Guest Messages'),plan);
assert.deepEqual(messages.map(m=>m.json.message_type),['status_new','house_rules','checkin_guide']);
assert(messages.every(m=>m.json.safety_passed));
const repeat=run(get('1','19 Prepare Ordered Guest Messages'),plan);
assert.deepEqual(repeat.map(m=>m.json.message_key),messages.map(m=>m.json.message_key));
const decorated=run(get('1','19A Decorate Immediate Delivery Jobs'),messages.map(m=>m.json));
assert.equal(decorated.filter(m=>m.json.should_notify_work_group).length,1);
const modification=run(get('1','19 Prepare Ordered Guest Messages'),{...plan,event_type:'modified',source_revision:2,event_revision:2,phone_changed:false,calendar_plan:{...plan.calendar_plan,immediate_checkin_reminder:false}});
assert.deepEqual(modification.map(m=>m.json.message_type),['status_modified']);
const changedPhone=run(get('1','19 Prepare Ordered Guest Messages'),{...plan,event_type:'modified',source_revision:2,phone:'16502530001',phone_changed:true});
assert.equal(changedPhone.length,3);
assert.notEqual(changedPhone[1].json.message_key,messages[1].json.message_key);
const cancelled=run(get('1','19 Prepare Ordered Guest Messages'),{...plan,event_type:'cancelled',source_revision:3});
assert.deepEqual(cancelled.map(m=>m.json.message_type),['status_cancelled']);
const reminder={...g,phone:'16502530000',action:'due',message_type:'reminder_checkout',reminder_id:'synthetic-reminder',message_key:'synthetic-checkout'};
const batch={reminders:[reminder],apartments:[{listing_map_id:97001,timezone:'Asia/Riyadh'}]};
const reminderResult=run(get('2','38 Render Reminder Items'),templates,{'36 Collect Apartment Lookup':batch})[0].json;
assert.equal(reminderResult.messages.length,1);
assert.match(reminderResult.messages[0].message_body,/11:00/);
assert.doesNotMatch(reminderResult.messages[0].message_body,/غد|12:00/);
reminderResult.messages[0].management_url='https://luxurysmartapartments.com/admin/operations/morning-stays/'+g.morning_stay_id+'/';
const morningDecorated=run(get('2','38A Decorate Scheduled Delivery Jobs'),reminderResult)[0].json;
assert.equal(morningDecorated.messages.length,1);
assert.match(morningDecorated.messages[0].group_failure_body,/admin\/operations\/morning-stays/);
assert.doesNotMatch(morningDecorated.messages[0].group_failure_body,/form\/2364e051|undefined/);
const dispatch=ops['2'].find(o=>o.nodeName==='4D0 Reminders - Next Delivery Action').parameters;
for(const morning of [true,false]){
 const context={$execution:{id:'synthetic'},$:name=>({isExecuted:name.startsWith('M20')?morning:!morning,
 first:()=>({json:{selected_timezones:['Asia/Riyadh']}})})};
 const evaluate=s=>JSON.parse(JSON.stringify(vm.runInNewContext(s.slice(3,-2).trim(),context)));
 assert.match(evaluate(dispatch.url),morning?/\/lsa_morning_next$/:/\/lsa_reminders_next$/);
 const args=evaluate(dispatch.jsonBody);
 assert.equal(Object.hasOwn(args,'p_timezones'),!morning);
}
const normalPlanCode=get('1','11 Plan Immediate Messages and Future Reminders');
const overnight={...g,source:'Hostaway',arrival_date:'2027-01-02',departure_date:'2027-01-05',check_in_time:'15:00',check_out_time:'12:00'};
const refs={'08 Get Apartment Info + Timezone':{timezone:'Asia/Riyadh'}};
const overnightPlan=run(normalPlanCode,overnight,refs)[0].json.calendar_plan;
assert.equal(overnightPlan.check_in_at,'2027-01-02T12:00:00.000Z');
assert.equal(overnightPlan.check_out_at,'2027-01-05T09:00:00.000Z');
assert.equal(overnightPlan.checkin_reminder_at,'2027-01-01T18:00:00.000Z');
assert.equal(overnightPlan.immediate_checkin_reminder,false);
console.log('PASS: changed Code syntax, valid/invalid events, stable duplicate keys, exact morning times, one team notice, changes, cancellation, checkout rendering, unchanged overnight planning.');
