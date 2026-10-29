'use strict';
const policy = require('./policy.cjs');
const copy = value => JSON.parse(JSON.stringify(value));

// The caller supplies credential-backed HTTP adapters. These pure functions never send.
// A preflight authorization MUST durably mark posting before returning allowed=true.
function start(candidate, packet, now) {
  if (!candidate?.eligible) return {action: {kind: 'done'}, reason: candidate?.reason};
  if (candidate.language !== packet.language) throw Error('artwork_language_mismatch');
  return {candidate, packet, stage: 'claim', started_at: now, posts: 0,
    action: {kind: 'claim', request: {...candidate, image_sha256: packet.image_sha256}}};
}
function next(s, kind, request) {return {...s, stage: kind, action: {kind, request}};}
function attention(s, reason) {return {...s, stage: 'attention', reason, action: {kind: 'attention'}};}
function receive(state, response, now) {
  const s = copy(state), kind = s.action?.kind;
  if (!kind) return attention(s, 'invitation_action_missing');
  if (kind === 'claim') {
    const decision = policy.claimDecision(s.candidate, response);
    if (decision.action === 'skip') return {...s, stage: 'done', reason: decision.reason, action: {kind: 'done'}};
    if (decision.action !== 'preflight') return attention(s, decision.reason);
    s.claim = decision;
    return next(s, 'preflight', {invitation_id: decision.invitation_id, ticket: decision.ticket,
      reference_id: decision.reference_id, phone: s.candidate.phone});
  }
  if (kind === 'preflight') {
    const decision = policy.preflight(s.claim, response, now);
    if (decision.action === 'skip') return {...s, stage: 'done', reason: decision.reason, action: {kind: 'done'}};
    if (decision.action !== 'send') return attention(s, decision.reason);
    if (s.posts !== 0) return attention(s, 'duplicate_post_prevented');
    s.posts = 1;
    s.deadline = new Date(Date.parse(now) + 300000).toISOString();
    return next(s, 'send', {to: s.candidate.phone, image: s.packet.image_base64,
      caption: s.packet.caption, referenceId: s.claim.reference_id});
  }
  if (kind === 'send' || kind === 'receipt') {
    const evidence = kind === 'send' ? policy.sendEvidence(response) : policy.receiptEvidence(response,
      {provider_id: s.provider_id, reference_id: s.claim.reference_id, phone: s.candidate.phone});
    if (evidence.provider_id) s.provider_id = evidence.provider_id;
    s.evidence = evidence;
    const timedOut = Date.parse(now) >= Date.parse(s.deadline);
    s.finished = evidence.delivered || evidence.state === 'failed' || timedOut;
    if (timedOut && !evidence.delivered) s.reason = 'delivery_unconfirmed_after_5_minutes';
    return next(s, 'record', {invitation_id: s.claim.invitation_id, ticket: s.claim.ticket,
      reference_id: s.claim.reference_id, provider_id: s.provider_id || null,
      phone: s.candidate.phone, state: evidence.state, delivered: evidence.delivered,
      image_sha256: s.packet.image_sha256, reason: s.reason || '', needs_attention: Boolean(s.reason || evidence.state === 'failed')});
  }
  if (kind === 'record') {
    const body = response?.body ?? response;
    if (response?.error || Number(response?.statusCode || 200) >= 400 || body?.ok !== true) return attention(s, 'result_recording_unconfirmed_do_not_repeat_post');
    if (s.finished) return s.evidence?.delivered ? {...s, stage: 'done', action: {kind: 'done'}} : attention(s, s.reason || 'invitation_failed');
    return next(s, 'wait', {seconds: 15});
  }
  if (kind === 'wait') return next(s, 'receipt', {id: s.provider_id || '', referenceId: s.claim.reference_id,
    status: 'all', limit: 100, page: 1, sort: 'desc'});
  return attention(s, 'unexpected_invitation_response');
}
module.exports = {start, receive};
