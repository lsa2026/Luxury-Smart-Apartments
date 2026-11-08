const test = require('node:test');
const assert = require('node:assert/strict');
const workflow = require('../docs/automation07/workflow.json');
const { render } = require('../scripts/automation07/render.cjs');
const admission = new Function('$json', workflow.nodes.find(n => n.name === 'M03 Validate Verified Morning Booking').parameters.jsCode);
function booking() {
  return { source:'morning07', contract_version:2, booking_verified:true, hostaway_reservation_id:999981,
    status:'confirmed', payment_status:'unpaid', payment_verified:false,
    morning_stay_id:'07000000-0000-4000-8000-000000000011', source_event_id:'07000000-0000-4000-8000-000000000012',
    source_revision:1, listing_map_id:123, phone:'+16465550123', guest_name:'Synthetic Guest',
    guest_email:'synthetic@example.test', guest_language:'en', listing_name:'Synthetic apartment',
    state_fingerprint:'a'.repeat(64), delivery_token:'synthetic-capability-'.repeat(4),
    arrival_date:'2032-01-01', departure_date:'2032-01-01', timezone:'Asia/Riyadh',
    check_in_time:'01:00', check_out_time:'11:00', starts_at:'2032-01-01T01:00:00+03:00',
    ends_at:'2032-01-01T11:00:00+03:00', management_url:'https://example.test/admin' };
}
test('actual Morning07 admission accepts manager-confirmed Hostaway before collection', () => {
  assert.equal(admission({statusCode:200,body:booking()})[0].json.valid,true);
  for (const change of [{booking_verified:false},{hostaway_reservation_id:0},{payment_verified:true},
    {status:'paid'},{check_out_time:'12:01'},{timezone:'Africa/Casablanca'}]) {
    assert.equal(admission({statusCode:200,body:{...booking(),...change}})[0].json.valid,false);
  }
});
test('unpaid confirmation stays truthful and uses the exact morning checkout', () => {
  const input={booking:booking(),previous:{},apartment:{},now:'2031-12-31T22:00:00Z',
    templates:[{template_name:'house_rules',content_en:'No smoking'},
      {template_name:'checkin_guide',content_en:'Contact support after ID verification'}]};
  const jobs=render(input).jobs;
  assert.equal(jobs.filter(x=>x.kind==='status_new').length,1);
  assert.match(jobs.find(x=>x.kind==='status_new').body,/Payment: unpaid/);
  assert.equal(jobs.find(x=>x.kind==='reminder_checkout').due_at,'2032-01-01T07:00:00.000Z');
  assert.deepEqual(render(input).jobs.map(x=>x.job_key),jobs.map(x=>x.job_key));
  const cancelled=render({...input,booking:{...booking(),status:'cancelled'},previous:booking()}).jobs;
  assert.deepEqual(cancelled.map(x=>x.kind),['status_cancelled','group_notice']);
});
