'use strict';

// Shared pure logic embedded into existing n8n workflows. No network or writes.
const JOIN_URL = 'https://luxurysmartapartments.com/my-bookings/rewards/';
const TEMPLATE_VERSION = 'external-enrollment-v1-20261029';
const DEMO_NAMES = {ar: 'نورة الحربي', en: 'Emma Wilson', fr: 'Nora Martin'};
const QUALIFIED_COPY = {
  ar: ['لتستمتع بمكافآت إقامتك الحالية وإقاماتك القادمة', 'لتستمتع بمكافآت إقاماتك القادمة المؤهلة المحجوزة عبر موقعنا', 'الحجز عبر منصة خارجية لا يمنح نقاط إقامة أو مكافآت بأثر رجعي.'],
  en: ['to enjoy rewards on your current and future stays', 'to enjoy rewards on future eligible stays booked through our website', 'This external-platform booking earns no stay points or retrospective rewards.'],
  fr: ['pour profiter de récompenses sur vos séjours actuels et futurs', 'pour profiter de récompenses sur vos futurs séjours éligibles réservés sur notre site', 'Ce séjour réservé sur une autre plateforme ne génère pas de points ni de droits rétroactifs.'],
};

function cleanName(value) {
  return String(value || '').normalize('NFC').replace(/[\p{Cc}\p{Cf}]/gu, ' ')
    .replace(/[<>*_`~]/g, '').replace(/\s+/g, ' ').trim();
}

function normalizePhone(value) {
  const raw = String(value || '').replace(/\p{Cf}/gu, '')
    .replace(/[٠-٩]/g, c => String(c.charCodeAt(0) - 1632))
    .replace(/[۰-۹]/g, c => String(c.charCodeAt(0) - 1776)).trim();
  let phone = raw.replace(/[\s().-]/g, '');
  const international = /^(?:\+|00)/.test(phone);
  phone = phone.replace(/^\+/, '').replace(/^00/, '');
  if (!international && /^05\d{8}$/.test(phone)) phone = '966' + phone.slice(1);
  else if (!international && /^5\d{8}$/.test(phone)) phone = '966' + phone;
  return /^[1-9]\d{7,14}$/.test(phone) ? phone : null;
}

function render(samples, language, guestName) {
  if (!['ar', 'en', 'fr'].includes(language)) throw new Error('guest_language_missing');
  const sample = samples.find(s => s.language === language);
  if (!sample) throw new Error('approved_asset_missing');
  const name = cleanName(guestName);
  if (!name || name.length > 160) throw new Error('guest_name_missing_or_too_long');
  const [oldCopy, newCopy, limitation] = QUALIFIED_COPY[language];
  if (!sample.caption.includes(DEMO_NAMES[language]) || !sample.caption.includes(oldCopy)) {
    throw new Error('approved_template_changed');
  }
  const caption = sample.caption.replace(DEMO_NAMES[language], () => name)
    .replace(oldCopy, newCopy).trimEnd() + '\n\n' + limitation + '\n\n' + JOIN_URL;
  if (Object.values(DEMO_NAMES).some(demo => demo !== name && caption.includes(demo))) {
    throw new Error('sample_name_leaked');
  }
  // UltraMsg's caption limit is measured conservatively as UTF-16 code units.
  // Never silently truncate approved copy, the guest's name or the join link.
  if (caption.length > 1024) throw new Error('caption_too_long');
  return {language, caption, image_base64: sample.image_base64,
    image_sha256: sample.image_sha256, template_version: TEMPLATE_VERSION};
}

function candidate(snapshot, jobs, config, now) {
  const skip = reason => ({eligible: false, reason});
  const since = Date.parse(config?.activation_at || '');
  if (config?.enabled !== true || !Number.isFinite(since)) return skip('integration_not_enabled');
  const created = Date.parse(snapshot?.booking_created_at || '');
  if (!Number.isFinite(created) || created < since) return skip('historical_booking');
  const clock = Date.parse(now || '');
  if (!Number.isFinite(clock) || clock < created) return skip('event_time_invalid');
  if (!['confirmed', 'new', 'modified', 'accepted', 'reserved'].includes(String(snapshot.canonical_status || snapshot.status || '').toLowerCase())) return skip('booking_not_active');
  const platform = String(snapshot.booking_platform || '').trim();
  const source = String(snapshot.source || '').toLowerCase();
  if (!platform || /^(?:direct(?: website)?|website|hostaway|unknown|other|ical|customical)$/i.test(platform) || Number(snapshot.channel_id) === 2000 || /luxurysmartapartments|direct.website|morning07/.test(source)) return skip('external_source_not_verified');
  if (!/^\d{1,30}$/.test(String(snapshot.reservation_key || ''))) return skip('booking_identity_missing');
  const phone = normalizePhone(snapshot.phone);
  if (!phone) return skip('phone_missing');
  const language = String(snapshot.guest_language || '').toLowerCase().split(/[-_]/)[0];
  if (!['ar', 'en', 'fr'].includes(language)) return skip('guest_language_missing');
  const name = cleanName(snapshot.guest_name);
  if (!name) return skip('guest_name_missing');
  const propertyMessage = (Array.isArray(jobs) ? jobs : []).find(j => String(j.reservation_key) === String(snapshot.reservation_key)
    && String(j.payload?.listing_map_id) === String(snapshot.listing_map_id));
  const zone = snapshot.timezone || snapshot.property_timezone || propertyMessage?.payload?.timezone
    || propertyMessage?.payload?.apartment?.timezone;
  if (!['Asia/Riyadh', 'Africa/Casablanca'].includes(zone)) return skip('property_timezone_unknown');
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {timeZone: zone,
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'})
    .formatToParts(new Date(clock)).map(p => [p.type, p.value]));
  const localDay = parts.year + '-' + parts.month + '-' + parts.day;
  const localTime = parts.hour + ':' + parts.minute;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(snapshot.departure_date || '')) return skip('stay_dates_missing');
  if (snapshot.departure_date < localDay || (snapshot.departure_date === localDay
    && localTime >= (snapshot.check_out_time || '12:00'))) return skip('stay_ended');
  const sameBooking = j => String(j.reservation_key) === String(snapshot.reservation_key);
  const payload = j => j.payload || {};
  const type = j => payload(j).message_type;
  const sameProperty = j => String(payload(j).listing_map_id) === String(snapshot.listing_map_id);
  const sameDates = j => payload(j).arrival_date === snapshot.arrival_date && payload(j).departure_date === snapshot.departure_date;
  const delivered = j => {
    const p = payload(j), recovered = p.recovery_delivered === true || p.recovery_delivered === 'true';
    const recipient = normalizePhone(recovered ? p.recovery_phone || p.recipient_phone || p.phone : p.recipient_phone || p.phone);
    return recipient === phone && (recovered || (j.guest_delivered === true && p.delivery_channel !== 'email'));
  };
  const rows = (Array.isArray(jobs) ? jobs : []).filter(sameBooking).filter(sameProperty);
  if (!rows.some(j => ['status_new', 'status_modified'].includes(type(j)) && sameDates(j) && delivered(j))) return skip('confirmation_not_delivered');
  if (!rows.some(j => type(j) === 'house_rules' && delivered(j))) return skip('house_rules_not_delivered');
  if (rows.some(j => type(j) === 'checkin_guide' && sameDates(j) && !delivered(j) && j.state !== 'cancelled')) return skip('entry_instructions_pending');
  return {eligible: true, reservation_key: String(snapshot.reservation_key), phone,
    guest_name: name, language, guest_email: String(snapshot.guest_email || ''),
    source_platform: platform, booking_created_at: new Date(created).toISOString(),
    event_id: 'loyalty-external-v1:' + snapshot.reservation_key,
    template_version: TEMPLATE_VERSION};
}

function claimDecision(candidate, response) {
  if (candidate?.eligible !== true) return {action: 'skip', reason: candidate?.reason || 'not_eligible'};
  const body = response?.body ?? response;
  if (response?.error || (response?.statusCode && response.statusCode !== 200) || !body || body.code !== 'ok') return {action: 'attention', reason: 'membership_lookup_unavailable'};
  if (['member', 'duplicate', 'blocked', 'phone_missing'].includes(body.decision)) return {action: 'skip', reason: body.decision};
  if (body.decision !== 'eligible' || body.send_allowed !== false || !body.invitation_id || !body.ticket || !body.provider_reference || body.recipient_phone !== candidate.phone) return {action: 'attention', reason: 'membership_claim_unverified'};
  return {action: 'preflight', invitation_id: body.invitation_id, ticket: body.ticket,
    reference_id: body.provider_reference, candidate};
}

function preflight(claim, response, now) {
  const body = response?.body ?? response;
  if (response?.error || (response?.statusCode && response.statusCode !== 200) || !body || body.code !== 'ok') return {action: 'attention', reason: 'preflight_unavailable'};
  if (body.send_allowed !== true || body.decision !== 'eligible') return {action: 'skip', reason: body.decision === 'member' ? 'member' : 'preflight_denied'};
  if (body.invitation_id !== claim.invitation_id || body.provider_reference !== claim.reference_id || body.recipient_phone !== claim.candidate.phone || Date.parse(body.send_expires_at) <= Date.parse(now) || !Number.isFinite(Date.parse(body.send_expires_at))) return {action: 'attention', reason: 'preflight_identity_mismatch'};
  return {...claim, action: 'send'};
}

function sendEvidence(response) {
  const body = response?.body ?? response;
  const id = body?.id == null ? '' : String(body.id);
  const error = Boolean(response?.error || body?.error);
  const status = Number(response?.statusCode || 0);
  const yes = body?.sent === true || body?.sent === 'true';
  if (!error && (!status || status < 400) && id && yes) return {state: 'accepted', provider_id: id, delivered: false, retry_post: false};
  if (!id && status >= 400 && status < 500 && status !== 408) return {state: 'failed', delivered: false, retry_post: false};
  return {state: 'unknown', provider_id: id || null, delivered: false, retry_post: false};
}

function receiptEvidence(response, expected) {
  const body = response?.body ?? response;
  if (response?.error || Number(response?.statusCode || 200) >= 400) return {state: 'unknown', delivered: false, retry_post: false};
  const records = Array.isArray(body) ? body : Array.isArray(body?.messages) ? body.messages : [];
  const rows = records.filter(r => (!expected.provider_id || String(r.id) === String(expected.provider_id))
    && normalizePhone(String(r.to || '').replace(/@c\.us$/, '')) === expected.phone
    && String(r.referenceId || '') === expected.reference_id);
  if (rows.length !== 1) return {state: 'unknown', delivered: false, retry_post: false};
  const row = rows[0], ack = String(row.ack || '').toLowerCase(), status = String(row.status || '').toLowerCase();
  if (['invalid', 'unsent', 'expired'].includes(status)) return {state: 'failed', delivered: false, retry_post: false};
  if (['read', 'played'].includes(ack)) return {state: 'read', provider_id: String(row.id), delivered: true, retry_post: false};
  if (ack === 'device') return {state: 'delivered', provider_id: String(row.id), delivered: true, retry_post: false};
  return {state: 'accepted', provider_id: String(row.id), delivered: false, retry_post: false};
}

module.exports = {JOIN_URL, TEMPLATE_VERSION, cleanName, normalizePhone, render, candidate, claimDecision, preflight, sendEvidence, receiptEvidence};
