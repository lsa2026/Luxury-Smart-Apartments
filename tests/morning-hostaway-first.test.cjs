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

test('unpaid delivery progress queues the fourth guest message through the protected website', () => {
  const branch=workflow.connections['L06 Verified Paid Morning Booking?'].main;
  assert.equal(branch[0][0].node,'L07 Paid Morning Loyalty Invitation');
  assert.equal(branch[1][0].node,'P06 Guest Delivery Confirmed?');
  const guard=workflow.nodes.find(n=>n.name==='P06 Guest Delivery Confirmed?');
  assert.match(guard.parameters.conditions.conditions[0].leftValue,/delivered === true/);
  assert.match(guard.parameters.conditions.conditions[0].leftValue,/message_type === 'checkin_guide'/);
  const expression=guard.parameters.conditions.conditions[0].leftValue.slice(3,-2).trim();
  const decide=new Function('$json','$','return '+expression);
  const output=kind=>({item:{json:{message_type:kind}},first:()=>({json:{message_type:'group_notice'}})});
  assert.equal(decide({delivered:true},()=>output('checkin_guide')),true);
  assert.equal(decide({delivered:true},()=>output('house_rules')),false);
  assert.equal(decide({delivered:false},()=>output('checkin_guide')),false);
  const path=workflow.connections[guard.name].main;
  assert.equal(path[0][0].node,'P07 Queue Payment Link After Three Messages');
  assert.equal(path[1][0].node,'D00 Claim Next Morning07 Delivery');
  const callback=workflow.nodes.find(n=>n.name===path[0][0].node);
  assert.equal(callback.parameters.url,'https://luxurysmartapartments.com/integrations/morning07/payment-link/');
  assert.equal(callback.parameters.method,'POST');
  assert.match(callback.parameters.jsonBody,/core_delivered/);
  assert.match(callback.parameters.headerParameters.parameters[0].value,/delivery_token/);
  assert.equal(workflow.connections[callback.name].main[0][0].node,'D00 Claim Next Morning07 Delivery');
});
