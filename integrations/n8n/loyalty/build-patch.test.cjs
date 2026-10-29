'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {build, names} = require('./build-patch.cjs');
const {samples} = require('../../../apps/loyalty/invitation_assets/enrollment-owner-review-v3.json');
const credential = {id: 'synthetic-credential-id', name: 'Synthetic fixture only'};
const options = {workflowId: 'synthetic-workflow', hooks: ['Existing Core Finished'], tablePrefix: 'lsa_',
  supabaseCredential: credential, websiteCredential: credential, providerCredential: credential,
  telegramCredential: credential, alertChatId: 'synthetic-chat', enabled: false,
  api: {claim: 'https://luxurysmartapartments.com/__contract_fixture__/claim',
    preflight: 'https://luxurysmartapartments.com/__contract_fixture__/preflight',
    record: 'https://luxurysmartapartments.com/__contract_fixture__/record', status: 'https://luxurysmartapartments.com/__contract_fixture__/status'},
  providerBase: 'https://api.ultramsg.com/instance999999'};
test('additive patch has no schedule or new workflow, and begins with a closed gate', () => {
  const patch = build(options, samples);
  assert.ok(patch.operations.every(o => ['addNode', 'setNodeSettings', 'addConnection'].includes(o.type)));
  assert.ok(patch.nodes.every(n => !/Trigger|schedule|executeWorkflow/.test(n.type)));
  assert.ok(patch.nodes.find(n => n.name === names.keys).parameters.jsCode.includes('"enabled":false'));
  for (const node of patch.nodes.filter(n => n.type === 'n8n-nodes-base.code')) assert.doesNotThrow(() => new Function(node.parameters.jsCode));
  assert.ok(patch.nodes.every(n => n.type !== 'n8n-nodes-base.gmail'));
  assert.ok(patch.operations.length <= 100);
});
test('activation requires real dependency bindings and an explicit timestamp', () => {
  assert.throws(() => build({...options, enabled: true}, samples), /activation_boundary_required/);
  assert.throws(() => build({...options, websiteCredential: undefined}, samples), /verified_credential_bindings_required/);
  assert.throws(() => build({...options, api: undefined}, samples), /verified_website_contract_required/);
});
test('every action is wired and every target is defined or an existing hook', () => {
  const patch = build(options, samples), known = new Set(patch.nodes.map(n => n.name));
  assert.equal(known.size, patch.nodes.length);
  for (const edge of patch.connections) assert.ok(known.has(edge.target));
  assert.equal(patch.connections.filter(e => e.source === names.route).length, 9);
  assert.equal(patch.nodes.find(n => n.name === names.receive).parameters.mode, 'runOnceForEachItem');
});
test('compiled n8n preparation keeps candidate pairing after skipping an older booking', () => {
  const clock = Date.now(), created = new Date(clock - 60000).toISOString();
  const patch = build({...options, enabled: true, activation_at: new Date(clock - 120000).toISOString()}, samples);
  const script = patch.nodes.find(n => n.name === names.prepare).parameters.jsCode;
  const base = {reservation_key: '99000007', booking_created_at: created, canonical_status: 'confirmed',
    booking_platform: 'Booking.com', channel_id: 2018, phone: '+999000000097', guest_name: 'Synthetic Guest',
    guest_language: 'fr', listing_map_id: 315814, timezone: 'Asia/Riyadh', arrival_date: '2099-01-01', departure_date: '2099-01-02'};
  const snapshots = [{...base, reservation_key: '99000006', booking_created_at: '2000-01-01T00:00:00Z'}, base];
  const inputs = snapshots.map(b => ({json: {statusCode: 200, body: ['status_new', 'house_rules'].map(type => ({reservation_key: b.reservation_key,
    state: 'complete', guest_delivered: true, payload: {...b, message_type: type, delivery_channel: 'whatsapp'}}))}}));
  const execute = new Function('$input', '$', '$runIndex', script);
  const read = name => ({all: () => snapshots.map(snapshot => ({json: {statusCode: 200, body: [{snapshot}]}}))});
  const result = execute({all: () => inputs}, read, 0);
  assert.equal(result.length, 1); assert.equal(result[0].pairedItem.item, 1);
  assert.equal(result[0].json.candidate.reservation_key, base.reservation_key);
  assert.equal(result[0].json.packet.language, 'fr');
  assert.equal(result[0].json.action.kind, 'claim');
});

test('credential-free staging disables every dependency and cannot be enabled', () => {
  const patch=build({...options, staged:true, websiteCredential:undefined},samples);
  assert.equal(patch.enabled,false);
  assert.equal(patch.nodes.filter(n=>!n.disabled).length,1);
  assert.equal(patch.nodes.find(n=>!n.disabled).name,names.keys);
  for(const name of [names.claim,names.preflight,names.record,names.status]){
    const node=patch.nodes.find(n=>n.name===name);
    assert.equal(node.disabled,true);assert.equal(node.parameters.authentication,'none');
    assert.equal(node.credentials,undefined);
  }
  assert.throws(()=>build({...options,staged:true,enabled:true},samples),/staged_integration_must_remain_closed/);
  assert.throws(()=>build({...options,staged:true,activation_at:'2099-01-01T00:00:00Z'},samples),/staged_integration_must_remain_closed/);
});

test('each later hook reads its own snapshot run instead of an earlier booking', () => {
  const clock=Date.now();
  const patch=build({...options,enabled:true,activation_at:new Date(clock-120000).toISOString()},samples);
  const script=patch.nodes.find(n=>n.name===names.prepare).parameters.jsCode;
  const snapshot={reservation_key:'99000008',booking_created_at:new Date(clock-60000).toISOString(),canonical_status:'confirmed',booking_platform:'Booking.com',channel_id:2018,phone:'999000000097',guest_name:'Synthetic Guest',guest_language:'en',listing_map_id:315814,timezone:'Asia/Riyadh',arrival_date:'2099-01-01',departure_date:'2099-01-02'};
  const jobs=['status_new','house_rules'].map(message_type=>({reservation_key:snapshot.reservation_key,state:'complete',guest_delivered:true,payload:{...snapshot,message_type,delivery_channel:'whatsapp'}}));
  let reads=0;
  const result=new Function('$input','$','$runIndex',script)({all:()=>[{json:{statusCode:200,body:jobs}}]},()=>({all:(port,run)=>{reads++;assert.equal(run,1);return [{json:{statusCode:200,body:[{snapshot}]}}]}}),1);
  assert.equal(reads,1);assert.equal(result[0].json.candidate.reservation_key,'99000008');
});
test('closed compiled n8n entry never reads or invokes downstream services', () => {
  const patch = build(options, samples), script = patch.nodes.find(n => n.name === names.keys).parameters.jsCode;
  const result = new Function('$input', '$', script)({all: () => {throw Error('should not read input');}}, () => {throw Error('should not read nodes');});
  assert.deepEqual(result, []);
});

