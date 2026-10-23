const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require.resolve('../static/js/admin-morning-availability.js'), 'utf8');

function element() {
  return {children: [], attributes: {}, listeners: {}, value: '', disabled: false,
    addEventListener(name, fn) { this.listeners[name] = fn; },
    setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) { delete this.attributes[name]; },
    replaceChildren() { this.children = []; },
    append(...items) { this.children.push(...items); },
    querySelectorAll() { return this.children; },
    querySelector() { return this.children[2]; },
    reportValidity() { return true; },
    dispatchEvent() { this.changed = true; },
    focus() { this.focused = true; }};
}
function fixture(fetch) {
  const check = element(), status = element(), results = element();
  const fields = Object.fromEntries(['date', 'start_time', 'end_time', 'guests', 'property', 'guest_email'].map(name => [name, element()]));
  Object.entries({date:'2026-10-25', start_time:'01:00', end_time:'11:00', guests:'2', guest_email:'private@example.test'}).forEach(([name,value]) => fields[name].value = value);
  const form = {dataset:{morningAvailability:'/private/available/'},
    elements:{namedItem:name=>fields[name]},
    querySelector:selector=>({'[data-morning-check]':check, '[data-morning-status]':status, '[data-morning-results]':results}[selector])};
  vm.runInNewContext(source, {document:{querySelector:()=>form, createElement:()=>element()}, fetch,
    URLSearchParams, Intl, Date, Event, Error, TypeError, SyntaxError});
  return {check,status,results,fields};
}
function response(properties) {
  return {ok:true, json:async()=>({properties, checked_at:'2026-10-25T00:00:00Z',unverified_count:0})};
}
const units = [1,2].map(id=>({id:String(id),name:`Apartment ${id}`,time_zone:'Asia/Riyadh'}));

test('availability reads only period fields; selecting a unit never submits or creates a booking', async()=>{
  const requests=[];
  const f=fixture(async(url,options)=>{requests.push({url,options});return response(units);});
  await f.check.listeners.click();
  assert.equal(requests.length,1);
  assert.equal(requests[0].options.cache,'no-store');
  assert.ok(!requests[0].url.includes('private%40'));
  assert.deepEqual([...new URL(requests[0].url,'https://example.test').searchParams.keys()],['date','start_time','end_time','guests']);
  assert.equal(f.results.children.length,2);
  f.results.children[0].listeners.click();
  f.results.children[1].listeners.click();
  assert.equal(f.fields.property.value,'2');
  assert.equal(f.fields.property.changed,true);
  assert.equal(f.results.children[0].attributes['aria-pressed'],'false');
  assert.equal(f.results.children[0].children[2].textContent,'اختيار الشقة');
  assert.equal(f.results.children[1].attributes['aria-pressed'],'true');
  assert.equal(requests.length,1);
});

test('changing a period discards an in-flight response and clears its busy state',async()=>{
  let resolve;
  const f=fixture(()=>new Promise(r=>resolve=r));
  const pending=f.check.listeners.click();
  assert.equal(f.check.disabled,true);
  f.fields.end_time.value='10:00';
  f.fields.end_time.listeners.input();
  resolve(response(units));
  await pending;
  assert.equal(f.results.children.length,0);
  assert.equal(f.results.attributes['aria-busy'],undefined);
  assert.equal(f.check.disabled,false);
});

test('invalid fields never request availability and network failures show Arabic feedback',async()=>{
  let calls=0;
  const f=fixture(async()=>{calls++;throw new TypeError('Failed to fetch');});
  f.fields.guests.reportValidity=()=>false;
  await f.check.listeners.click();
  assert.equal(calls,0);
  f.fields.guests.reportValidity=()=>true;
  await f.check.listeners.click();
  assert.match(f.status.textContent,/تعذر الاتصال/);
  assert.equal(f.check.disabled,false);
  assert.equal(f.results.children.length,0);
});
