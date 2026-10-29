'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const contract = require('./website-contract-v1.json');
const controller = require('./controller.cjs');
const now = '2026-10-29T10:30:00Z';
const candidate = {eligible: true, reservation_key: '99000007', phone: '999000000097',
  guest_email: 'synthetic@example.invalid', guest_name: 'Synthetic Guest', language: 'en',
  source_platform: 'Booking.com', event_id: 'loyalty-external-v1:99000007'};
const packet = {language: 'en', caption: 'Synthetic only', image_base64: 'synthetic', image_sha256: 'test-hash'};
const example = action => structuredClone(contract.paths['/integrations/loyalty/invitations/' + action + '/'].post.responses['200'].content['application/json'].example);
const claim = {...example('claim'), recipient_phone: candidate.phone, language: 'en'};
const preflight = {...example('preflight'), recipient_phone: candidate.phone, language: 'en', send_expires_at: '2026-10-29T10:31:00Z'};
function shape(body, key) {
  const schema = contract.components.schemas[key];
  for (const k of schema.required) assert.ok(Object.hasOwn(body, k), key + ' missing ' + k);
  for (const [k,v] of Object.entries(body)) {
    assert.ok(Object.hasOwn(schema.properties,k), key + ' extra property ' + k);
    assert.equal(typeof v,'string', key + '.' + k);
    if(schema.properties[k].enum)assert.ok(schema.properties[k].enum.includes(v));
    if(schema.properties[k].maxLength)assert.ok(v.length<=schema.properties[k].maxLength);
  }
}
test('claim and preflight wire requests match the actual strict 0107 JSON contract', () => {
  let s = controller.start(candidate, packet, now);shape(s.action.request,'Claim');
  s = controller.receive(s, claim, now);assert.equal(s.action.kind,'preflight');shape(s.action.request,'Preflight');
  s = controller.receive(s, preflight, now);assert.equal(s.action.kind,'send');
  assert.equal(s.action.request.referenceId, claim.provider_reference);
  assert.equal(s.send_expires_at, preflight.send_expires_at);
});
test('record wire request uses status/provider_reference and no undocumented properties', () => {
  let s = controller.receive(controller.start(candidate, packet, now),claim,now);
  s = controller.receive(s,preflight,now);s=controller.receive(s,{statusCode:200,body:{sent:true,id:77}},now);
  shape(s.action.request,'Record');assert.equal(s.action.request.status,'accepted');
  assert.equal(s.action.request.provider_reference,claim.provider_reference);
});
test('ambiguous preflight reads status and cannot issue a provider POST', () => {
  let s=controller.receive(controller.start(candidate,packet,now),claim,now);
  s=controller.receive(s,{error:'timeout'},now);assert.equal(s.action.kind,'status');assert.equal(s.posts,0);
  assert.deepEqual(Object.keys(s.action.request),['invitation_id']);
  s=controller.receive(s,{code:'ok',invitation_id:claim.invitation_id,provider_reference:claim.provider_reference,status:'unknown'},now);
  assert.equal(s.action.kind,'attention');assert.equal(s.posts,0);
});
test('accepted duplicate is reconciled through status, not another preflight or POST', () => {
  let s=controller.receive(controller.start(candidate,packet,now),{...claim,decision:'duplicate',status:'accepted',ticket:undefined},now);
  assert.equal(s.action.kind,'status');assert.equal(s.posts,0);
  s=controller.receive(s,{...claim,status:'read',provider_id:'77'},now);
  assert.equal(s.action.kind,'done');assert.equal(s.evidence.state,'read');assert.equal(s.posts,0);
});
test('expired real preflight cannot authorize provider POST', () => {
  let s=controller.receive(controller.start(candidate,packet,now),claim,now);
  s=controller.receive(s,{...preflight,send_expires_at:'2026-10-29T10:29:00Z'},now);
  assert.equal(s.action.kind,'status');assert.equal(s.posts,0);
});
