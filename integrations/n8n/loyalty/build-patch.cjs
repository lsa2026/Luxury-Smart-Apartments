'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const ROOT = __dirname;
const policy = fs.readFileSync(path.join(ROOT, 'policy.cjs'), 'utf8');
const controller = fs.readFileSync(path.join(ROOT, 'controller.cjs'), 'utf8').replace("const policy = require('./policy.cjs');", '');
const embedded = source => '(function(){const module={exports:{}};' + source + ';return module.exports;})()';
const runtime = 'const policy=' + embedded(policy) + ';const controller=' + embedded(controller) + ';\n';
const N = {
  keys: 'L00 External Loyalty Booking Keys', snapshot: 'L01 Current Loyalty Booking',
  jobs: 'L02 Core Delivery Evidence', prepare: 'L03 Prepare Optional Loyalty Invitation',
  route: 'L06 Route Optional Loyalty Action', claim: 'L07 Claim Shared Loyalty Invitation',
  status: 'L05 Read Shared Loyalty State',
  preflight: 'L08 Recheck Loyalty Membership', send: 'L09 Send Approved Loyalty Image',
  receipt: 'L10 Read Exact Loyalty Receipt', record: 'L11 Record Shared Loyalty Result',
  wait: 'L12 Wait for Loyalty Receipt', receive: 'L13 Interpret Optional Loyalty Result',
  attention: 'L14 Loyalty Attention Text', notify: 'L15 Notify Optional Loyalty Failure',
  done: 'L16 Optional Loyalty Finished',
};

/** Build additive operations for an EXISTING workflow; never creates/publishes one.
 * api paths/credential must be supplied from the final verified 0107 contract.
 * Set enabled=false for isolated validation. Enabling requires an activation_at.
 */
function build(options, samples) {
  const {workflowId, hooks, tablePrefix, supabaseCredential, websiteCredential,
    providerCredential, telegramCredential, alertChatId, api, providerBase, activation_at, enabled} = options;
  if (!workflowId || !Array.isArray(hooks) || !hooks.length) throw Error('workflow_hooks_required');
  if (!['lsa_', 'lsa06_'].includes(tablePrefix)) throw Error('unsupported_existing_journey');
  if (![supabaseCredential, websiteCredential, providerCredential, telegramCredential].every(c => c?.id && c?.name)) throw Error('verified_credential_bindings_required');
  const websiteAuth = websiteCredential.type || 'httpTemplatedCustomAuth';
  if (!['httpTemplatedCustomAuth', 'httpHeaderAuth'].includes(websiteAuth)) throw Error('unsupported_website_auth_binding');
  if (!['claim', 'preflight', 'record', 'status'].every(k => typeof api?.[k] === 'string' && api[k].startsWith('https://luxurysmartapartments.com/'))) throw Error('verified_website_contract_required');
  if (!/^https:\/\/api\.ultramsg\.com\/instance\d+$/.test(providerBase || '')) throw Error('provider_base_required');
  if (enabled === true && !Number.isFinite(Date.parse(activation_at))) throw Error('activation_boundary_required');
  const config = {enabled: enabled === true, activation_at: activation_at || null};
  const nodes = [], connections = [];
  const id = name => {const h = crypto.createHash('sha256').update(workflowId + ':' + name).digest('hex');return h.slice(0,8)+'-'+h.slice(8,12)+'-4'+h.slice(13,16)+'-a'+h.slice(17,20)+'-'+h.slice(20,32);};
  const add = (name, type, typeVersion, parameters, credentials) => {
    const node = {id: id(name), name, type, typeVersion, parameters, position: [2400 + (nodes.length % 5) * 240, 2600 + Math.floor(nodes.length / 5) * 180]};
    if (credentials) node.credentials = credentials;
    nodes.push(node);return node;
  };
  const code = (name, jsCode, mode = 'runOnceForAllItems') => add(name, 'n8n-nodes-base.code', 2, {mode, jsCode});
  const io = (name, parameters, credentials) => add(name, 'n8n-nodes-base.httpRequest', 4.4,
    {...parameters, options: {response: {response: {fullResponse: true, neverError: true, responseFormat: 'json'}}, timeout: 10000}}, credentials);
  const db = name => 'https://rsqgwepnowpnpealrzbn.supabase.co/rest/v1/' + name;
  const dbCredentials = {supabaseApi: supabaseCredential};
  code(N.keys, `const config=${JSON.stringify(config)};if(config.enabled!==true)return [];
const keys=new Set();
for(const item of $input.all()){const j=item.json;if(j.overall_success===true&&j.state?.reservation_key)keys.add(String(j.state.reservation_key));}
try{for(const r of $('2R0 Collect Booking Report Scope').all(0,0)){for(const c of r.json.contexts||[])if(c.reservation_key)keys.add(String(c.reservation_key));}}catch{}
return [...keys].filter(k=>/^\\d{1,30}$/.test(k)).map(k=>({json:{reservation_key:k}}));`);
  io(N.snapshot, {url: db(tablePrefix + 'v3_reservations'), authentication: 'predefinedCredentialType', nodeCredentialType: 'supabaseApi',
    sendQuery: true, queryParameters: {parameters: [{name: 'select', value: 'reservation_key,snapshot'}, {name: 'reservation_key', value: '={{ "eq."+$json.reservation_key }}'}, {name: 'limit', value: '1'}]}}, dbCredentials);
  // Full-response arrays are not unrolled; preserve a single paired response per key.
  io(N.jobs, {url: db(tablePrefix + 'v32_delivery_jobs'), authentication: 'predefinedCredentialType', nodeCredentialType: 'supabaseApi',
    sendQuery: true, queryParameters: {parameters: [{name: 'select', value: 'reservation_key,payload,state,guest_delivered,next_at'},
      {name: 'reservation_key', value: `={{ "eq."+$('${N.keys}').item.json.reservation_key }}`}]}}, dbCredentials);
  code(N.prepare, runtime + `const config=${JSON.stringify(config)},samples=${JSON.stringify(samples)};const out=[];
for(const [i,item] of $input.all().entries()){const j=item.json;try{const snap=$('${N.snapshot}').all(0,0)[i].json;
if(snap.error||snap.statusCode!==200||j.error||j.statusCode!==200||!Array.isArray(snap.body)||snap.body.length!==1||!Array.isArray(j.body))throw Error('authoritative_loyalty_context_unavailable');
const c=policy.candidate(snap.body[0].snapshot,j.body,config,new Date().toISOString());if(!c.eligible)continue;
const packet=policy.render(samples,c.language,c.guest_name);out.push({json:controller.start(c,packet,new Date().toISOString()),pairedItem:{item:i}});
}catch(e){out.push({json:{action:{kind:'attention'},reason:String(e.message).slice(0,300)},pairedItem:{item:i}});}}
return out;`);
  add(N.route, 'n8n-nodes-base.switch', 3.4, {mode: 'expression', numberOutputs: 9,
    output: "={{ ['claim','preflight','send','receipt','record','status','wait','attention','done'].includes($json.action?.kind) ? ['claim','preflight','send','receipt','record','status','wait','attention','done'].indexOf($json.action.kind) : 7 }}"});
  const apiNode = (name, url) => io(name, {method: 'POST', url, authentication: 'genericCredentialType', genericAuthType: websiteAuth, sendBody: true,
    specifyBody: 'json', jsonBody: '={{ $json.action.request }}'}, {[websiteAuth]: {id: websiteCredential.id, name: websiteCredential.name}});
  apiNode(N.claim, api.claim);apiNode(N.preflight, api.preflight);apiNode(N.record, api.record);apiNode(N.status, api.status);
  io(N.send, {method: 'POST', url: `={{ Date.now() < Date.parse($json.send_expires_at) ? '${providerBase}/messages/image' : '' }}`, authentication: 'genericCredentialType', genericAuthType: 'httpTemplatedCustomAuth',
    sendBody: true, contentType: 'form-urlencoded', bodyParameters: {parameters: ['to', 'image', 'caption', 'referenceId'].map(k => ({name: k, value: '={{ $json.action.request.' + k + ' }}'}))}}, {httpTemplatedCustomAuth: providerCredential});
  io(N.receipt, {url: providerBase + '/messages', authentication: 'genericCredentialType', genericAuthType: 'httpTemplatedCustomAuth', sendQuery: true,
    queryParameters: {parameters: ['id', 'referenceId', 'status', 'limit', 'page', 'sort'].map(k => ({name: k, value: '={{ $json.action.request.' + k + ' }}'}))}}, {httpTemplatedCustomAuth: providerCredential});
  add(N.wait, 'n8n-nodes-base.wait', 1.1, {resume: 'timeInterval', amount: 15, unit: 'seconds'});
  code(N.receive, runtime + `let s;try{s=$('${N.route}').item.json;return {json:controller.receive(s,$json,new Date().toISOString())};}catch(e){return {json:{...(s||{}),action:{kind:'attention'},reason:String(e.message).slice(0,300)}};}`, 'runOnceForEachItem');
  code(N.attention, `return $input.all().map((i,n)=>({json:{text:['⚠️ تعثرت دعوة الولاء الاختيارية؛ الرسائل الأساسية لا تتوقف.', 'الحجز: '+(i.json.candidate?.reservation_key||'غير محدد'),'السبب: '+(i.json.reason||'نتيجة الدعوة تحتاج مراجعة'),'التنفيذ: '+String($execution.id)].join('\\n')},pairedItem:{item:n}}));`);
  add(N.notify, 'n8n-nodes-base.telegram', 1.2, {resource: 'message', operation: 'sendMessage', chatId: alertChatId, text: '={{ $json.text }}', additionalFields: {appendAttribution: false}}, {telegramApi: telegramCredential});
  code(N.done, "return $input.all().map((i,n)=>({json:{invitation_status:i.json.evidence?.state||'skipped',reason:i.json.reason||'',reservation_key:i.json.candidate?.reservation_key||''},pairedItem:{item:n}}));");
  const edge = (source, target, sourceIndex = 0) => connections.push({type: 'addConnection', source, target, sourceIndex, targetIndex: 0, connectionType: 'main'});
  for (const hook of hooks) edge(hook, N.keys);
  edge(N.keys, N.snapshot);edge(N.snapshot, N.jobs);edge(N.jobs, N.prepare);edge(N.prepare, N.route);
  [N.claim, N.preflight, N.send, N.receipt, N.record, N.status, N.wait, N.attention, N.done].forEach((name, i) => edge(N.route, name, i));
  for (const name of [N.claim, N.preflight, N.send, N.receipt, N.record, N.status, N.wait]) edge(name, N.receive);
  edge(N.receive, N.route);edge(N.attention, N.notify);
  const operations = nodes.flatMap(node => [{type: 'addNode', node},
    {type: 'setNodeSettings', nodeName: node.name, settings: {onError: 'continueRegularOutput'}}]).concat(connections);
  return {operations, nodes, connections, enabled: config.enabled, workflowId};
}
module.exports = {build, names: N};
