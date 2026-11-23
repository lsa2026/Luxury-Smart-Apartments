// Test-only frozen Native39 validator, SHA256 9c1421ea4b529026d397a833b9d5673116ef909a0631f9026a7fbe0ea0b10b4d.
// Packet/code/image/phone are transient stdin only; output is limited to checks/hashes.
import {pcPlan, pcValidatePacket} from './tg39-phone-protocol.fixture.mjs';
try {
  let raw='';
  for await (const chunk of process.stdin) {
    raw+=chunk;
    if(raw.length>80000) throw Error('INPUT_BOUND');
  }
  const input=JSON.parse(raw),p=input.packet;
  if(input.dispatch) {
    const plan=pcPlan(p,input.now);
    process.stdout.write(JSON.stringify({
      valid:true,scope_fields:Object.keys(plan.scope).sort(),
      methods:plan.parts.map(x=>x.method),
      code_only_in_text:!plan.parts[0].html.includes(input.synthetic_code)&&plan.parts[1].html.includes(input.synthetic_code),
      nonce_not_in_scope_or_parts:!JSON.stringify(plan).includes(p.content_nonce),
      part_hashes:plan.parts.map(x=>x.content_sha256),
    }));
  } else {
    const scope=pcValidatePacket(p,false,input.now);
    process.stdout.write(JSON.stringify({valid:true,scope_fields:Object.keys(scope).sort()}));
  }
} catch {
  process.stdout.write(JSON.stringify({valid:false,error:'NATIVE_CONTRACT_REJECTED'}));
  process.exitCode=1;
}
