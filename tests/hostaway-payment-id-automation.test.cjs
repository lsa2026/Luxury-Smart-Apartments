const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = readFileSync(resolve(__dirname, '../docs/hostaway-id-audit/pathway03-phone-verification.js'), 'utf8');
const large = '957144035275968076';
function verify(key, id, previous) {
  const state = previous || {reservation_key:key, phone:'19995550123', errors:[]};
  const context = {
    $json:{statusCode:200, body:{status:'success', result:{id, phone:'+19995550123'}}},
    $:()=>({item:{json:{state}}, first:()=>({json:{reservation_key:key, phone:state.phone}})}),
  };
  return vm.runInNewContext('(function(){'+source+'})()', context, {timeout:1000})[0].json.state;
}
test('pathway03 accepts legacy and string IDs and retains large text exactly', () => {
  for(const [key,id] of [['123',123],['123','123'],[large,large]]) {
    const state = verify(key,id);
    assert.equal(state.hostaway_phone_saved,true);
    assert.equal(state.reservation_key,key);
  }
});
test('pathway03 fails closed for another exact text identity', () => {
  const state = verify(large,'957144035275968077');
  assert.equal(state.hostaway_phone_saved,false);
  assert.equal(state.errors.length,1);
});
test('repeated read-back does not duplicate errors or change booking identity', () => {
  const once = verify(large,'957144035275968077');
  const twice = verify(large,'957144035275968077',once);
  assert.equal(twice.errors.length,1);
  const recovered = verify(large,large,twice);
  assert.equal(recovered.errors.length,0);
  assert.equal(recovered.reservation_key,large);
});
