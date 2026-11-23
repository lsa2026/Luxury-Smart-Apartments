// Test-only frozen Native39 validator, SHA256 0a47ca8550f4fb8f1a65c79cadd1ecb7c25bf78076a06a9c0d7ba6d16a4b7bd5.
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
