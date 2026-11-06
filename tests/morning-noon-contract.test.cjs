const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const workflow = JSON.parse(fs.readFileSync('docs/automation07/workflow.json', 'utf8'));
const code = workflow.nodes.find(n => n.name === 'M03 Validate Verified Morning Booking').parameters.jsCode;
const validate = new Function('$json', code);
const body = {source:'morning07',payment_verified:true,status:'paid',morning_stay_id:'07000000-0000-4000-8000-000000000001',source_event_id:'07000000-0000-4000-8000-000000000002',source_revision:1,listing_map_id:123,phone:'+16502530000',state_fingerprint:'a'.repeat(64),delivery_token:'x'.repeat(60),arrival_date:'2032-01-01',departure_date:'2032-01-01',timezone:'Asia/Riyadh',check_in_time:'01:00',check_out_time:'12:00',starts_at:'2032-01-01T01:00:00+03:00',ends_at:'2032-01-01T12:00:00+03:00'};
// JSON export uses escaped regex for imported workflow serialization. Normalize only the known regex escape.
const run = new Function('$json', code.replace('/^\\\\+?', '/^\\+?').replace('\\\\d{7,14}', '\\d{7,14}'));
test('verified morning checkout includes noon in both local time zones', () => {
 for (const timezone of ['Asia/Riyadh','Africa/Casablanca']) assert.equal(run({statusCode:200,body:{...body,timezone}})[0].json.valid,true);
});
test('after-noon, unverified, and overnight events remain rejected', () => {
 for (const change of [{check_out_time:'12:01'}, {payment_verified:false}, {check_in_time:'00:00'}, {departure_date:'2032-01-02'}]) assert.equal(run({statusCode:200,body:{...body,...change}})[0].json.valid,false);
});
