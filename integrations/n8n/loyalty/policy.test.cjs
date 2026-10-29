'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const p = require('./policy.cjs');
const {samples} = require('../../../apps/loyalty/invitation_assets/enrollment-owner-review-v3.json');
const config = {enabled: true, activation_at: '2026-10-29T10:00:00Z'};
const now = '2026-10-29T10:30:00Z';
const booking = {reservation_key: '99000007', booking_created_at: '2026-10-29T10:01:00Z',
  canonical_status: 'confirmed', booking_platform: 'Booking.com', channel_id: 2018,
  phone: '+999000000097', guest_name: 'Synthetic Guest', guest_language: 'en',
  listing_map_id: 315814, timezone: 'Asia/Riyadh', arrival_date: '2026-11-01', departure_date: '2026-11-02'};
const job = type => ({reservation_key: booking.reservation_key, state: 'complete', guest_delivered: true,
  payload: {...booking, message_type: type, delivery_channel: 'whatsapp'}});
const jobs = [job('status_new'), job('house_rules')];
const candidate = () => p.candidate(booking, jobs, config, now);
const claimBody = () => ({code: 'ok', decision: 'eligible', send_allowed: false, invitation_id: 'synthetic-id',
  ticket: 'synthetic-ticket', provider_reference: 'synthetic-reference', recipient_phone: '999000000097'});

for (const language of ['ar', 'en', 'fr']) test('approved ' + language + ' image and qualified caption', () => {
  const sample = samples.find(x => x.language === language);
  assert.equal(crypto.createHash('sha256').update(Buffer.from(sample.image_base64, 'base64')).digest('hex'), sample.image_sha256);
  const result = p.render(samples, language, 'Synthetic Guest');
  assert.ok(result.caption.includes('Synthetic Guest'));
  assert.ok(result.caption.endsWith(p.JOIN_URL));
  assert.ok(result.caption.length <= 1024);
  assert.equal(result.image_base64, sample.image_base64);
  for (const demo of ['نورة الحربي', 'Emma Wilson', 'Nora Martin']) assert.ok(!result.caption.includes(demo));
  assert.ok(!/current and future stays|séjours actuels et futurs|مكافآت إقامتك الحالية/.test(result.caption));
  assert.ok(/سما|Sama/.test(result.caption));
});
test('new external reservation after completed core messages is eligible', () => assert.equal(candidate().eligible, true));
test('no activation or historical campaign', () => {
  assert.equal(p.candidate(booking, jobs, {...config, enabled: false}, now).reason, 'integration_not_enabled');
  assert.equal(p.candidate({...booking, booking_created_at: '2026-10-28T00:00:00Z'}, jobs, config, now).reason, 'historical_booking');
});
test('website and ambiguous source never enter external campaign', () => {
  for (const change of [{booking_platform: 'Direct Website'}, {channel_id: 2000}, {source: 'LuxurySmartApartments'}, {booking_platform: 'iCal'}]) assert.equal(p.candidate({...booking, ...change}, jobs, config, now).eligible, false);
});
test('late correction cannot invite an ended stay', () => {
  assert.equal(p.candidate({...booking, departure_date: '2026-10-29', check_out_time: '12:00'}, jobs, config, now).reason, 'stay_ended');
});
test('property timezone comes from its own delivery context, never a global Saudi default', () => {
  const b = {...booking, timezone: undefined};
  const mapped = jobs.map(j => ({...j, payload: {...j.payload, timezone: undefined, apartment: {timezone: 'Africa/Casablanca'}}}));
  assert.equal(p.candidate(b, mapped, config, now).eligible, true);
  assert.equal(p.candidate(b, jobs.map(j => ({...j, payload: {...j.payload, timezone: undefined}})), config, now).reason, 'property_timezone_unknown');
});
test('missing or malformed phone does not call membership/send', () => {
  for (const phone of ['', 'not-a-number', '0542']) assert.equal(p.candidate({...booking, phone}, jobs, config, now).reason, 'phone_missing');
  assert.equal(p.normalizePhone('٠٥٤٢٨٠٠٣٢٥'), '966542800325');
  assert.equal(p.normalizePhone('+965 6655 7745'), '96566557745');
});
test('email acceptance never counts as core WhatsApp delivery', () => {
  const emails = jobs.map(j => ({...j, guest_delivered: false, email_accepted: true, payload: {...j.payload, delivery_channel: 'email'}}));
  assert.equal(p.candidate(booking, emails, config, now).reason, 'confirmation_not_delivered');
});
test('delivery to an obsolete number does not unlock an invitation to a corrected number', () => {
  assert.equal(p.candidate({...booking, phone: '999000000098'}, jobs, config, now).reason, 'confirmation_not_delivered');
});
test('core failure blocks invitation only; corrections unlock after actual delivery', () => {
  const pending = [jobs[0], {...jobs[1], guest_delivered: false}];
  assert.equal(p.candidate(booking, pending, config, now).reason, 'house_rules_not_delivered');
  pending[1].payload.recovery_delivered = true;
  assert.equal(p.candidate(booking, pending, config, now).eligible, true);
});
test('due entry instructions finish before invitation', () => {
  assert.equal(p.candidate(booking, [...jobs, {...job('checkin_guide'), state: 'attention', guest_delivered: false}], config, now).reason, 'entry_instructions_pending');
});
test('existing membership and duplicate are explicit skip decisions', () => {
  for (const decision of ['member', 'duplicate']) assert.equal(p.claimDecision(candidate(), {code: 'ok', decision}).action, 'skip');
});
test('unknown membership or mismatched target cannot be treated as non-member', () => {
  assert.equal(p.claimDecision(candidate(), {statusCode: 503}).action, 'attention');
  assert.equal(p.claimDecision(candidate(), {...claimBody(), send_allowed: undefined}).action, 'attention');
  assert.equal(p.claimDecision(candidate(), {...claimBody(), recipient_phone: '999000000098'}).action, 'attention');
});
test('member joining between claim and preflight prevents sending', () => {
  const claim = p.claimDecision(candidate(), claimBody());
  assert.equal(p.preflight(claim, {code: 'ok', send_allowed: false, decision: 'member'}, now).action, 'skip');
});
test('expired or wrong preflight ticket cannot be sent', () => {
  const claim = p.claimDecision(candidate(), claimBody());
  const result = {...claimBody(), send_allowed: true, send_expires_at: '2026-10-29T11:00:00Z'};
  assert.equal(p.preflight(claim, result, now).action, 'send');
  assert.equal(p.preflight(claim, {...result, send_expires_at: '2026-10-29T09:00:00Z'}, now).action, 'attention');
});
test('provider accepted is not delivered; timeout never repeats POST', () => {
  assert.deepEqual(p.sendEvidence({statusCode: 200, body: {sent: true, id: 77}}), {state: 'accepted', provider_id: '77', delivered: false, retry_post: false});
  assert.equal(p.sendEvidence({error: 'timeout'}).state, 'unknown');
  assert.equal(p.sendEvidence({error: 'timeout'}).retry_post, false);
  assert.equal(p.sendEvidence({statusCode: 400, body: {error: 'invalid'}}).state, 'failed');
});
test('delivery evidence requires exact id, recipient and reference; no numeric ACK inference', () => {
  const expected = {provider_id: '77', phone: '999000000097', reference_id: 'synthetic-reference'};
  const row = {id: 77, to: '999000000097@c.us', referenceId: expected.reference_id, status: 'sent', ack: 'read'};
  assert.equal(p.receiptEvidence({messages: [row]}, expected).state, 'read');
  assert.equal(p.receiptEvidence({messages: [{...row, ack: 2}]}, expected).delivered, false);
  assert.equal(p.receiptEvidence({messages: [{...row, to: '999000000098@c.us'}]}, expected).delivered, false);
  assert.equal(p.receiptEvidence({statusCode: 502, body: {messages: [row]}}, expected).delivered, false);
});
test('long name or unsupported language does not silently truncate or choose artwork', () => {
  assert.throws(() => p.render(samples, 'fr', 'X'.repeat(151)), /guest_name_missing_or_too_long/);
  const overlong=samples.map(s=>s.language==='fr'?{...s,caption:s.caption+' X'.repeat(600)}:s);
  assert.throws(() => p.render(overlong, 'fr', 'Synthetic Guest'), /caption_too_long/);
  assert.throws(() => p.render(samples, 'de', 'Synthetic Guest'), /guest_language_missing/);
});
test('shared claim contract handles webhook repeat and corrected phone without another invitation', () => {
  // Test double ONLY. The live site must implement this durable uniqueness atomically.
  const reservationClaims = new Map(), phoneClaims = new Map();
  const claim = c => {
    if (reservationClaims.has(c.reservation_key) || phoneClaims.has(c.phone)) return {code: 'ok', decision: 'duplicate'};
    reservationClaims.set(c.reservation_key, 'unknown'); phoneClaims.set(c.phone, 'unknown'); return claimBody();
  };
  const c = candidate();
  assert.equal(p.claimDecision(c, claim(c)).action, 'preflight');
  assert.equal(p.claimDecision(c, claim(c)).action, 'skip');
  assert.equal(p.claimDecision({...c, phone: '999000000098'}, claim({...c, phone: '999000000098'})).action, 'skip');
  assert.equal(p.claimDecision({...c, reservation_key: '99000008'}, claim({...c, reservation_key: '99000008'})).action, 'skip');
});

