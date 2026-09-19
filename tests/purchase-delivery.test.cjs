const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const code = fs.readFileSync('static/js/analytics.js', 'utf8');
const consentCode = fs.readFileSync('static/js/consent.js', 'utf8');
const storageKey = 'lsa:purchase:v2:test-booking';
const flush = () => new Promise(resolve => setImmediate(resolve));

function run({consent = true, storage = new Map(), results = [204], noPurchase = false,
    storageFails = false, value = '1090.0'} = {}) {
    const handlers = {}, timers = new Map(), scripts = [];
    let nextTimer = 1, acknowledgements = 0;
    const purchase = {dataset: {analyticsValue:value, analyticsTransactionId:'test-booking',
        analyticsCurrency:'SAR', analyticsReceiptToken:'signed-test', analyticsReceiptUrl:'/ack/'}};
    const context = {console, URLSearchParams, AbortController, HTMLDialogElement:class {},
        fetch: (url, options) => {
            const response = results[Math.min(acknowledgements++,results.length-1)];
            if (response === 'hang') return new Promise((resolve,reject)=>
                options.signal.addEventListener('abort',()=>reject(new Error('timeout'))));
            return response === 'reject' ? Promise.reject(new Error('offline'))
                : Promise.resolve({ok:response>=200 && response<300,status:response});
        },
        document: {
            cookie:`lsa_cookie_consent=${encodeURIComponent(JSON.stringify({version:1,analytics:consent,marketing:consent}))}`,
            body:{dataset:{googleIntegrationsEnabled:'true',gtmEnabled:'true',gtmContainerId:'GTM-TEST123',
                cookieConsentEnabled:'true',consentModeEnabled:'true'}},
            head:{append:s=>scripts.push(s)},
            createElement:()=>{const s={dataset:{},remove:()=>scripts.splice(scripts.indexOf(s),1)};return s;},
            querySelectorAll:()=>[],
            querySelector:s=>s==='[data-analytics-purchase-event]' ? (noPurchase?null:purchase)
                : s==="script[data-lsa-google-script='gtm']" ? (scripts[0]||null) : null,
            addEventListener:(name,fn)=>{handlers[name]=fn;},
        },
        window:{dataLayer:[],addEventListener:(name,fn)=>{handlers[name]=fn;},
            setTimeout:(fn,delay)=>{const id=nextTimer++;timers.set(id,{fn,delay});return id;},
            clearTimeout:id=>timers.delete(id),
            sessionStorage:{
                getItem:k=>{if(storageFails)throw new Error('disabled');return storage.get(k);},
                setItem:(k,v)=>{if(storageFails)throw new Error('disabled');storage.set(k,v);},
            },
        },
    };
    vm.createContext(context);
    vm.runInContext(consentCode,context);
    vm.runInContext(code,context);
    return {context,handlers,storage,scripts,timers,
        get acknowledgements(){return acknowledgements;},
        get purchases(){return context.window.dataLayer.filter(e=>e.event==='purchase');},
        process:(id='GTM-TEST123')=>context.window.dataLayer.find(e=>e.event==='purchase')?.eventCallback(id),
        tick:delay=>{for(const [id,t] of [...timers])if(t.delay===delay){timers.delete(id);t.fn();}},
    };
}

test('matching GTM callback is required; enqueue and wrong container never acknowledge',async()=>{
    const r=run();assert.equal(r.acknowledgements,0);assert.equal(r.storage.size,0);
    assert.equal(r.purchases[0].eventTimeout,undefined);
    r.process('GTM-WRONG');assert.equal(r.acknowledgements,0);
    r.process();r.process();await flush();
    assert.equal(r.acknowledgements,1);assert.equal(r.storage.get(storageKey),'acknowledged');
});
test('legacy pushed marker cannot suppress a still-prepared server receipt',()=>{
    assert.equal(run({storage:new Map([['lsa:purchase:test-booking','pushed']])}).purchases.length,1);
});
test('network and HTTP ACK failures retry ACK, never another purchase',async()=>{
    for(const failed of ['reject',500,403]){
        const r=run({results:[failed,204]});r.process();await flush();
        assert.equal(r.storage.get(storageKey),'processed');r.tick(2000);await flush();
        assert.equal(r.acknowledgements,2);assert.equal(r.purchases.length,1);
        assert.equal(r.storage.get(storageKey),'acknowledged');
    }
});
test('reload after interrupted ACK retries only ACK',async()=>{
    const r=run({storage:new Map([[storageKey,'processed']])});await flush();
    assert.equal(r.purchases.length,0);assert.equal(r.acknowledgements,1);
    assert.equal(r.storage.get(storageKey),'acknowledged');
});
test('hanging acknowledgement times out and retries without republishing purchase',async()=>{
    const r=run({results:['hang',204]});r.process();r.tick(8000);await flush();
    assert.equal(r.storage.get(storageKey),'processed');r.tick(2000);await flush();
    assert.equal(r.acknowledgements,2);assert.equal(r.purchases.length,1);
    assert.equal(r.storage.get(storageKey),'acknowledged');
});
test('ACK retry is bounded; online can resume it without duplicate purchase',async()=>{
    const r=run({results:[500,500,500,204]});r.process();await flush();
    r.tick(2000);await flush();r.tick(4000);await flush();
    assert.equal(r.acknowledgements,3);assert.equal(r.timers.size,0);
    r.handlers.online();await flush();assert.equal(r.acknowledgements,4);assert.equal(r.purchases.length,1);
});
test('storage denied still deduplicates repeated callbacks within page',async()=>{
    const r=run({storageFails:true});r.process();r.process();await flush();
    assert.equal(r.acknowledgements,1);assert.equal(r.purchases.length,1);
});
test('denied ordinary page never loads GTM',()=>{
    const r=run({consent:false,noPurchase:true});assert.equal(r.scripts.length,0);assert.equal(r.purchases.length,0);
});
test('verified purchase with denied consent does not grant consent or prematurely ACK',()=>{
    const r=run({consent:false});assert.equal(r.scripts.length,1);assert.equal(r.purchases.length,1);
    assert.equal(r.acknowledgements,0);assert.equal(r.storage.size,0);
    for(const e of r.context.window.dataLayer.filter(e=>e[0]==='consent')){
        for(const name of ['analytics_storage','ad_storage','ad_user_data','ad_personalization'])
            assert.equal(e[2][name],'denied');
    }
    assert.deepEqual(Object.keys(r.purchases[0].ecommerce).sort(),['currency','transaction_id','value']);
});
test('GTM load failure retries loader, not purchase, with bounded attempts',()=>{
    const r=run({consent:false});r.scripts[0].onerror();r.tick(2000);
    assert.equal(r.scripts.length,1);assert.equal(r.purchases.length,1);assert.equal(r.acknowledgements,0);
    assert.equal(r.context.window.dataLayer.filter(e=>e.event==='gtm.js').length,1);
    r.scripts[0].onerror();r.tick(4000);r.scripts[0].onerror();
    assert.equal(r.scripts.length,0);assert.equal(r.timers.size,0);
    r.handlers.online();assert.equal(r.scripts.length,0);
});
test('blocked Google remains retryable on a fresh page load',()=>{
    const storage=new Map();const first=run({consent:false,storage});assert.equal(first.acknowledgements,0);
    const second=run({consent:false,storage});assert.equal(second.purchases.length,1);assert.equal(second.acknowledgements,0);
});
test('invalid purchase on denied page does not load any tracker',()=>{
    const r=run({consent:false,value:'1090,0'});assert.equal(r.scripts.length,0);assert.equal(r.acknowledgements,0);
});
