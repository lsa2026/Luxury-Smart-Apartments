'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const c = require('./controller.cjs');
const now = '2026-10-29T10:30:00Z';
const candidate = {eligible: true, reservation_key: '99000007', phone: '999000000097', language: 'en'};
const packet = {language: 'en', caption: 'Synthetic only', image_base64: 'synthetic', image_sha256: 'fake-hash'};
const claim = {ok: true, decision: 'eligible', member: false, invitation_id: 'test-invitation', ticket: 'test-ticket', reference_id: 'test-ref', phone: candidate.phone};
const ready = () => c.receive(c.receive(c.start(candidate, packet, now), claim, now),
  {...claim, allowed: true, expires_at: '2026-10-29T10:31:00Z'}, now);
test('normal complete path sends once and distinguishes accepted/read', () => {
  let s = ready(); assert.equal(s.action.kind, 'send');
  s = c.receive(s, {body: {sent: true, id: 77}, statusCode: 200}, now);
  assert.equal(s.action.kind, 'record'); assert.equal(s.evidence.delivered, false);
  s = c.receive(s, {ok: true}, now); assert.equal(s.action.kind, 'wait');
  s = c.receive(s, {}, now); assert.equal(s.action.kind, 'receipt');
  s = c.receive(s, {messages: [{id: 77, referenceId: 'test-ref', to: candidate.phone + '@c.us', status: 'sent', ack: 'read'}]}, now);
  assert.equal(s.evidence.delivered, true); s = c.receive(s, {ok: true}, now);
  assert.equal(s.action.kind, 'done'); assert.equal(s.posts, 1);
});
test('uncertain POST is reconciled by exact reference without a second POST', () => {
  let s = c.receive(ready(), {error: 'timeout'}, now);
  assert.equal(s.evidence.state, 'unknown'); s = c.receive(s, {ok: true}, now);
  s = c.receive(s, {}, now);
  s = c.receive(s, {messages: [{id: 88, referenceId: 'test-ref', to: candidate.phone + '@c.us', status: 'sent', ack: 'device'}]}, now);
  assert.equal(s.provider_id, '88'); s = c.receive(s, {ok: true}, now);
  assert.equal(s.action.kind, 'done'); assert.equal(s.posts, 1);
});
test('invitation send failure reaches attention without touching essential messages', () => {
  let s = c.receive(ready(), {statusCode: 400, body: {error: 'invalid'}}, now);
  s = c.receive(s, {ok: true}, now); assert.equal(s.action.kind, 'attention'); assert.equal(s.posts, 1);
});
test('recording failure never retries accepted provider POST', () => {
  let s = c.receive(ready(), {body: {sent: true, id: 77}}, now);
  s = c.receive(s, {error: 'network'}, now); assert.equal(s.action.kind, 'attention'); assert.equal(s.posts, 1);
});
test('timeout during receipt produces unresolved attention, not delivered', () => {
  let s = c.receive(ready(), {error: 'timeout'}, now);
  s = c.receive(s, {ok: true}, now); s = c.receive(s, {}, now);
  s = c.receive(s, {messages: []}, '2026-10-29T10:36:00Z');
  assert.equal(s.evidence.delivered, false); assert.equal(s.action.kind, 'record');
  s = c.receive(s, {ok: true}, '2026-10-29T10:36:00Z'); assert.equal(s.action.kind, 'attention'); assert.equal(s.posts, 1);
});
test('duplicate and member finish without any POST', () => {
  for (const decision of ['member', 'duplicate']) {
    const s = c.receive(c.start(candidate, packet, now), {ok: true, decision}, now);
    assert.equal(s.action.kind, 'done'); assert.equal(s.posts, 0);
  }
});
test('mismatched artwork is rejected before a claim', () => assert.throws(() => c.start(candidate, {...packet, language: 'fr'}, now), /artwork_language_mismatch/));
