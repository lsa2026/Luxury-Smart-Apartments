'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const c = require('./controller.cjs');
const now = '2026-10-29T10:30:00Z';
const candidate = {eligible: true, reservation_key: '99000007', phone: '999000000097', language: 'en'};
const packet = {language: 'en', caption: 'Synthetic only', image_base64: 'synthetic', image_sha256: 'fake-hash'};
const claim = {code: 'ok', decision: 'eligible', send_allowed: false, invitation_id: 'test-invitation', ticket: 'test-ticket', provider_reference: 'test-ref', recipient_phone: candidate.phone};
const recorded = {code: 'ok', invitation_id: claim.invitation_id, provider_reference: claim.provider_reference};
const ready = () => c.receive(c.receive(c.start(candidate, packet, now), claim, now),
  {...claim, send_allowed: true, send_expires_at: '2026-10-29T10:31:00Z'}, now);
test('normal complete path sends once and distinguishes accepted/read', () => {
  let s = ready(); assert.equal(s.action.kind, 'send');
  s = c.receive(s, {body: {sent: true, id: 77}, statusCode: 200}, now);
  assert.equal(s.action.kind, 'record'); assert.equal(s.evidence.delivered, false);
  s = c.receive(s, recorded, now); assert.equal(s.action.kind, 'wait');
  s = c.receive(s, {}, now); assert.equal(s.action.kind, 'receipt');
  s = c.receive(s, {messages: [{id: 77, referenceId: 'test-ref', to: candidate.phone + '@c.us', status: 'sent', ack: 'read'}]}, now);
  assert.equal(s.evidence.delivered, true); s = c.receive(s, recorded, now);
  assert.equal(s.action.kind, 'done'); assert.equal(s.posts, 1);
});
test('uncertain POST is reconciled by exact reference without a second POST', () => {
  let s = c.receive(ready(), {error: 'timeout'}, now);
  assert.equal(s.evidence.state, 'unknown'); s = c.receive(s, recorded, now);
  s = c.receive(s, {}, now);
  s = c.receive(s, {messages: [{id: 88, referenceId: 'test-ref', to: candidate.phone + '@c.us', status: 'sent', ack: 'device'}]}, now);
  assert.equal(s.provider_id, '88'); s = c.receive(s, recorded, now);
  assert.equal(s.action.kind, 'done'); assert.equal(s.posts, 1);
});
test('invitation send failure reaches attention without touching essential messages', () => {
  let s = c.receive(ready(), {statusCode: 400, body: {error: 'invalid'}}, now);
  s = c.receive(s, recorded, now); assert.equal(s.action.kind, 'attention'); assert.equal(s.posts, 1);
});
test('recording failure never retries accepted provider POST', () => {
  let s = c.receive(ready(), {body: {sent: true, id: 77}}, now);
  s = c.receive(s, {error: 'network'}, now); assert.equal(s.action.kind, 'status'); s = c.receive(s, {code:'ok', invitation_id:claim.invitation_id, provider_reference:claim.provider_reference, status:'unknown'}, now); assert.equal(s.action.kind, 'attention'); assert.equal(s.posts, 1);
});
test('timeout during receipt produces unresolved attention, not delivered', () => {
  let s = c.receive(ready(), {error: 'timeout'}, now);
  s = c.receive(s, recorded, now); s = c.receive(s, {}, now);
  s = c.receive(s, {messages: []}, '2026-10-29T10:36:00Z');
  assert.equal(s.evidence.delivered, false); assert.equal(s.action.kind, 'record');
  s = c.receive(s, recorded, '2026-10-29T10:36:00Z'); assert.equal(s.action.kind, 'attention'); assert.equal(s.posts, 1);
});
test('duplicate and member finish without any POST', () => {
  for (const decision of ['member', 'duplicate']) {
    const s = c.receive(c.start(candidate, packet, now), {code: 'ok', decision}, now);
    assert.equal(s.action.kind, 'done'); assert.equal(s.posts, 0);
  }
});
test('mismatched artwork is rejected before a claim', () => assert.throws(() => c.start(candidate, {...packet, language: 'fr'}, now), /artwork_language_mismatch/));
