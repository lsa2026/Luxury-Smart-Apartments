'use strict';
const policy = require('./policy.cjs');
const copy = value => JSON.parse(JSON.stringify(value));

// The caller supplies credential-backed HTTP adapters. These pure functions never send.
// A preflight authorization MUST durably mark posting before returning allowed=true.
function start(candidate, packet, now) {
  if (!candidate?.eligible) return {action: {kind: 'done'}, reason: candidate?.reason};
  if (candidate.language !== packet.language) throw Error('artwork_language_mismatch');
  return {candidate, packet, stage: 'claim', started_at: now, posts: 0,
    action: {kind: 'claim', request: {reservation_key: candidate.reservation_key, phone: candidate.phone,
      email: candidate.guest_email || '', language: candidate.language, guest_name: candidate.guest_name,
      source_platform: candidate.source_platform, event_id: candidate.event_id}}};
}
function next(s, kind, request) {return {...s, stage: kind, action: {kind, request}};}
function attention(s, reason) {return {...s, stage: 'attention', reason, action: {kind: 'attention'}};}
function receive(state, response, now) {
  const s = copy(state), kind = s.action?.kind;
  if (!kind) return attention(s, 'invitation_action_missing');
  if (kind === 'claim') {
    const b = response?.body ?? response;
    if (!response?.error && Number(response?.statusCode || 200) === 200 && b?.code === 'ok'
      && b.decision === 'duplicate' && ['accepted', 'unknown'].includes(b.status)
      && b.invitation_id && b.provider_reference) {
      s.claim = {invitation_id: b.invitation_id, reference_id: b.provider_reference};
      s.reason = 'previous_invitation_requires_reconciliation';
      return next(s, 'status', {invitation_id: b.invitation_id});
    }
    const decision = policy.claimDecision(s.candidate, response);
    if (decision.action === 'skip') return {...s, stage: 'done', reason: decision.reason, action: {kind: 'done'}};
    if (decision.action !== 'preflight') return attention(s, decision.reason);
    s.claim = decision;
    return next(s, 'preflight', {invitation_id: decision.invitation_id, ticket: decision.ticket, phone: s.candidate.phone});
  }
  if (kind === 'preflight') {
    const decision = policy.preflight(s.claim, response, now);
    if (decision.action === 'skip') return {...s, stage: 'done', reason: decision.reason, action: {kind: 'done'}};
    if (decision.action !== 'send') {s.reason = decision.reason; return next(s, 'status', {invitation_id: s.claim.invitation_id});}
    if (s.posts !== 0) return attention(s, 'duplicate_post_prevented');
    s.posts = 1;
    s.send_expires_at = (response?.body ?? response).send_expires_at;
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
    return next(s, 'record', {invitation_id: s.claim.invitation_id,
      provider_reference: s.claim.reference_id, provider_id: s.provider_id || '', status: evidence.state});
  }
  if (kind === 'record') {
    const body = response?.body ?? response;
    if (response?.error || Number(response?.statusCode || 200) >= 400 || body?.code !== 'ok'
      || body.invitation_id !== s.claim.invitation_id || body.provider_reference !== s.claim.reference_id) {
      s.reason = 'result_recording_unconfirmed_do_not_repeat_post';
      return next(s, 'status', {invitation_id: s.claim.invitation_id});
    }
    if (s.finished) return s.evidence?.delivered ? {...s, stage: 'done', action: {kind: 'done'}} : attention(s, s.reason || 'invitation_failed');
    return next(s, 'wait', {seconds: 15});
  }
  if (kind === 'wait') return next(s, 'receipt', {id: s.provider_id || '', referenceId: s.claim.reference_id,
    status: 'all', limit: 100, page: 1, sort: 'desc'});
  if (kind === 'status') {
    const b = response?.body ?? response;
    if (!response?.error && Number(response?.statusCode || 200) === 200 && b?.code === 'ok'
      && b.invitation_id === s.claim.invitation_id && b.provider_reference === s.claim.reference_id
      && ['delivered', 'read'].includes(b.status) && b.provider_id) {
      return {...s, evidence: {state: b.status, delivered: true, provider_id: b.provider_id}, stage: 'done', action: {kind: 'done'}};
    }
    return attention(s, s.reason || 'shared_invitation_status_unresolved_do_not_send');
  }
  return attention(s, 'unexpected_invitation_response');
}
module.exports = {start, receive};
