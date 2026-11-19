'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const harness = fs.readFileSync('tests/prepurchase.test.cjs', 'utf8');
// Reuse the established DOM harness, not a substitute analytics implementation.
const {run} = new Function('require', harness.slice(0, harness.indexOf('\ntest(')) + '\nreturn {run};')(require);
const source = fs.readFileSync('static/js/assisted-attribution.js', 'utf8');
const purchase = {transaction_id:'synthetic-opaque-order', value:750, currency:'SAR',
    attribution_version:'aa1', attribution_status:'reference_linked', closure_method:'admin_whatsapp',
    acquisition_kind:'new_booking', first_source:'google', first_medium:'cpc', first_campaign_id:'24262302633',
    last_source:'google', last_medium:'cpc', last_campaign_id:'24262298844', campaign_evidence:'tagged_id',
    origin_property_id:'synthetic-ad-apartment', market:'riyadh'};

test('actual analytics sender preserves controlled first/last tuple and opaque standard order',()=>{
    const d=run();
    assert.notEqual(d.window.LSAAnalytics.pushEvent('purchase',purchase),false);
    const event=d.events('purchase')[0];
    for(const [key,value] of Object.entries(purchase))assert.equal(event[key],value);
    assert.equal(event.ecommerce.transaction_id,purchase.transaction_id);
    assert.equal(event.ecommerce.value,750);
    assert.equal(event.ecommerce.first_campaign_id,undefined);
    if(process.env.LSA_ASSISTED_FIXTURES)fs.writeFileSync(process.env.LSA_ASSISTED_FIXTURES,JSON.stringify({synthetic:true,event},null,2));
});
test('staff browser cannot emit guest purchase even with analytics consent',()=>{
    const d=run();d.document.body.dataset.analyticsStaffBrowser='true';
    assert.equal(d.window.LSAAnalytics.pushEvent('purchase',purchase),false);
    assert.equal(d.events('purchase').length,0);
});
test('existing anonymous purchase exception carries no assisted source fields',()=>{
    const d=run({consent:false});d.window.LSAConsent.loadPurchaseTracker=()=>true;
    assert.notEqual(d.window.LSAAnalytics.pushEvent('purchase',purchase),false);
    const event=d.events('purchase')[0];assert.equal(event.ecommerce.value,750);
    assert.equal(event.first_source,null);assert.equal(event.first_campaign_id,null);
    assert.equal(event.attribution_version,null);
});
test('subsequent events atomically clear assisted source, origin and campaign fields',()=>{
    const d=run();d.window.LSAAnalytics.pushEvent('purchase',purchase);
    d.window.LSAAnalytics.pushEvent('whatsapp_click',{lead_source:'floating_button'});
    const event=d.events('whatsapp_click')[0];
    for(const key of Object.keys(purchase).filter(k=>!['transaction_id','value','currency'].includes(k)))assert.equal(event[key],null);
});
test('raw sources, numeric/unknown campaign ids and unsupported classifications are rejected',()=>{
    const d=run();for(const bad of [{first_source:'a-private-name'},{last_medium:'private@email.invalid'},
        {first_campaign_id:'966500000000'},{last_campaign_id:24262302633},{closure_method:'staff-session'},
        {attribution_status:'google_verified'},{attribution_version:'aa2'}]){
        assert.equal(d.window.LSAAnalytics.pushEvent('purchase',{...purchase,...bad}),false);
    }
    assert.equal(d.events('purchase').length,0);
});
test('full purchase schema stays within25 fields; unrelated pp1 schema stays frozen',()=>{
    const d=run();const full={...purchase,items:[{item_id:'synthetic-booked-other'}],tax:0,coupon:'synthetic',
        property_id:'synthetic-booked-other',property_name:'Synthetic apartment',page_language:'en',
        payment_type:'card',payment_brand:'visa',checkout_kind:'payment_link'};
    const clean=d.window.LSAAnalytics.sanitize('purchase',full);
    assert.equal(Object.keys(clean).length,25);
    assert.equal(d.window.LSAAnalytics.sanitize('availability_view',{measurement_version:'pp1',first_source:'google'}),null);
});

function landing({staff=false,resultUrl='/contact/whatsapp/?language=en'}={}){
    let analytics=false;const handlers=new Map(),posts=[],link={href:'https://wa.me/966500000000'};
    const body={dataset:{analyticsStaffBrowser:String(staff),attributionPublicPage:'true',attributionPageKind:'property_detail',
        attributionPropertyId:'synthetic-ad-apartment',attributionConsentUrl:'/analytics/attribution/consent/',
        attributionWhatsappOriginal:link.href}};
    const document={body,cookie:'csrftoken=synthetic-csrf',querySelectorAll:()=>[link],querySelector:()=>null,
        addEventListener:(name,fn)=>handlers.set(name,fn)};
    const window={location:{origin:'https://test.invalid',search:'?utm_source=google&utm_medium=cpc&utm_id=24262302633&gclid=private-click&email=private-email'},
        LSAConsent:{parseConsent:()=>({analytics})},
        fetch:async(url,options)=>{posts.push({url,options});return{ok:true,json:async()=>({state:analytics?'captured':'cleared',whatsapp_url:resultUrl})};}};
    vm.runInNewContext(source,{window,document,URL,URLSearchParams});
    return {link,posts,async choice(value){analytics=value;await handlers.get('lsa:consent-updated')?.();}};
}
test('late consent posts bounded campaign parameters but never raw click id, URL or guest data',async()=>{
    const d=landing();assert.equal(d.posts.length,0);await d.choice(true);
    const body=d.posts[0].options.body.toString();
    assert.ok(body.includes('utm_id=24262302633'));assert.ok(body.includes('click_id_seen=1'));
    for(const bad of ['private-click','private-email','gclid=','email=','http'])assert.equal(body.includes(bad),false);
    assert.equal(d.posts[0].options.headers['X-CSRFToken'],'synthetic-csrf');
    assert.equal(d.link.href,'https://test.invalid/contact/whatsapp/?language=en');
    assert.equal(d.link.href.includes('LSA-'),false); // Enhanced outbound click cannot see the reference.
});
test('withdrawal restores ordinary link and clears context without replaying landing params',async()=>{
    const d=landing();await d.choice(true);await d.choice(false);
    assert.equal(d.link.href,'https://wa.me/966500000000');
    assert.equal(d.posts[1].options.body.has('utm_id'),false);
});
test('staff makes no capture request and an unexpected external redirect is ignored',async()=>{
    const staff=landing({staff:true});await staff.choice(true);assert.equal(staff.posts.length,0);
    const d=landing({resultUrl:'https://untrusted.invalid/contact/whatsapp/'});await d.choice(true);
    assert.equal(d.link.href,'https://wa.me/966500000000');
});
