const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('static/js/booking-history.js', 'utf8');
const api = import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
class Element extends EventTarget {
    constructor() { super(); this.attrs={}; this.textContent=''; }
    setAttribute(k,v) { this.attrs[k]=v; }
}
function setup(running='true') {
    const panel=new Element(), form=new Element(), submit=new Element(), status=new Element();
    const nodes={'[data-history-form]':form,'[data-history-submit]':submit,'[data-history-status]':status};
    panel.querySelector=s=>nodes[s];
    panel.dataset={running,statusUrl:'/my-bookings/restore/status/',waiting:'still running'};
    const page={hidden:false}, lifecycle=new EventTarget(), timers=new Map(); let next=0, reloaded=0;
    return {panel,form,submit,status,page,lifecycle,timers,
        schedule:fn=>{timers.set(++next,fn);return next;},cancel:id=>timers.delete(id),
        reload:()=>reloaded++, reloaded:()=>reloaded,
        flush:async()=>{const pending=[...timers.values()];timers.clear();for(const fn of pending) await fn();},
    };
}
test('inactive recovery never fetches; native form has no interception or extra POST',async()=>{
    const {watchBookingHistory}=await api; const env=setup('false'); let calls=0;
    watchBookingHistory(env.panel,{...env,fetcher:async()=>{calls++;}});
    assert.equal(env.timers.size,0);
    const event=new Event('submit',{cancelable:true}); env.form.dispatchEvent(event);
    assert.equal(event.defaultPrevented,false); assert.equal(env.submit.disabled,true);
    assert.equal(calls,0);
    assert.doesNotMatch(source,/method:\s*['"]POST|requestSubmit\(|\.submit\(|localStorage|dataLayer|console\.log/);
});
test('poll reads status only with no cache and reloads once on completion',async()=>{
    const {watchBookingHistory}=await api;const env=setup();let calls=0;
    watchBookingHistory(env.panel,{...env,fetcher:async(url,opts)=>{
        assert.equal(url,'/my-bookings/restore/status/');
        assert.equal(opts.cache,'no-store');assert.equal(opts.credentials,'same-origin');
        assert.equal(opts.method,undefined);assert.equal(opts.body,undefined);calls++;
        return {ok:true,json:async()=>({running:calls===1,message:calls===1?'working':'done'})};
    }});
    await env.flush();assert.equal(env.status.textContent,'working');assert.equal(env.reloaded(),0);
    await env.flush();assert.equal(env.status.textContent,'done');assert.equal(env.reloaded(),1);
    await env.flush();assert.equal(calls,2);
});
test('hidden tabs make no requests and navigation stops in-flight responses',async()=>{
    const {watchBookingHistory}=await api;const env=setup();let calls=0,release;
    watchBookingHistory(env.panel,{...env,fetcher:()=>{calls++;return new Promise(resolve=>{release=resolve;});}});
    env.page.hidden=true;await env.flush();assert.equal(calls,0);
    env.page.hidden=false;const pending=env.flush();assert.equal(calls,1);
    env.lifecycle.dispatchEvent(new Event('pagehide'));
    release({ok:true,json:async()=>({running:false,message:'done'})});await pending;
    assert.equal(env.reloaded(),0);assert.equal(env.timers.size,0);
});
test('temporary status failures do not start recovery and reads are bounded',async()=>{
    const {watchBookingHistory}=await api;const env=setup();let calls=0;
    watchBookingHistory(env.panel,{...env,fetcher:async()=>{calls++;throw new Error('offline');}});
    for(let i=0;i<125;i++) await env.flush();
    assert.equal(calls,120);assert.equal(env.status.textContent,'still running');assert.equal(env.reloaded(),0);
});
test('expired session and browser back cache refresh state without performing an import',async()=>{
    const {watchBookingHistory}=await api;const env=setup();
    watchBookingHistory(env.panel,{...env,fetcher:async()=>({redirected:true})});
    await env.flush();assert.equal(env.reloaded(),1);
    const restored=new Event('pageshow');restored.persisted=true;env.lifecycle.dispatchEvent(restored);
    assert.equal(env.reloaded(),2);
});
