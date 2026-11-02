'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const policy = require('./policy.cjs');
const controller = require('./controller.cjs');
const now = '2026-11-02T10:30:00Z';
const packet = {language:'en',caption:'Synthetic fixture',image_base64:'synthetic'};
for (const [raw,canonical] of [['48500100200','+48500100200'],['+96566557745','+96566557745'],['0542800325','+966542800325'],['٠٥٤٢٨٠٠٣٢٥','+966542800325']]) {
 test('E.164 at website boundary; provider stays digits: '+raw, () => {
  const phone=policy.normalizePhone(raw);
  const candidate={eligible:true,reservation_key:'99000009',phone,language:'en'};
  let state=controller.start(candidate,packet,now);
  assert.equal(state.action.request.phone,canonical);
  const identity={code:'ok',decision:'eligible',send_allowed:false,invitation_id:'synthetic',ticket:'synthetic-ticket',provider_reference:'synthetic-reference',recipient_phone:canonical};
  state=controller.receive(state,identity,now);
  assert.equal(state.action.kind,'preflight');
  assert.equal(state.action.request.phone,canonical);
  state=controller.receive(state,{...identity,send_allowed:true,send_expires_at:'2026-11-02T10:31:00Z'},now);
  assert.equal(state.action.kind,'send');
  assert.equal(state.action.request.to,phone);
  assert.equal(state.posts,1);
 });
}
test('wrong international recipient still fails closed', () => {
 const candidate={eligible:true,phone:'48500100200',language:'en'};
 const claim=policy.claimDecision(candidate,{code:'ok',decision:'eligible',send_allowed:false,invitation_id:'synthetic',ticket:'ticket',provider_reference:'ref',recipient_phone:'+48500100201'});
 assert.equal(claim.action,'attention');
});
test('missing/invalid phone never becomes a plus-only API number', () => {
 for(const value of ['',null,'not a number'])assert.throws(()=>policy.internationalPhone(value),/candidate_phone_invalid/);
});
