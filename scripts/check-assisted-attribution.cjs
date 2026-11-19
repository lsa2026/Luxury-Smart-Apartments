/* Real Chromium + committed scripts on a synthetic local origin. No vendor loads. */
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {execFileSync}=require('node:child_process'),{chromium}=require('playwright');
(async()=>{
 const posts=[],external=[],cases=[];
 const purchase={transaction_id:'synthetic-opaque-order',value:750,currency:'SAR',attribution_version:'aa1',
  attribution_status:'reference_linked',closure_method:'admin_whatsapp',acquisition_kind:'new_booking',
  first_source:'google',first_medium:'cpc',first_campaign_id:'24262302633',last_source:'google',last_medium:'cpc',
  last_campaign_id:'24262298844',campaign_evidence:'tagged_id',origin_property_id:'synthetic-ad-apartment',market:'riyadh'};
 const server=http.createServer((req,res)=>{
  const url=new URL(req.url,'http://localhost');
  if(req.method==='POST'&&url.pathname==='/analytics/attribution/consent/'){
   let data='';req.on('data',b=>{data+=b});req.on('end',()=>{
    posts.push(data);res.setHeader('Content-Type','application/json');
    res.end(JSON.stringify({state:'captured',whatsapp_url:'/contact/whatsapp/?language=en'}));
   });return;
  }
  if(req.method!=='GET'){res.writeHead(405).end();return;}
  if(['/analytics.js','/assisted-attribution.js'].includes(url.pathname)){
   res.setHeader('Content-Type','text/javascript');res.end(fs.readFileSync(path.join('static/js',url.pathname.slice(1))));return;
  }
  if(url.pathname!=='/synthetic/'){res.writeHead(404).end();return;}
  const staff=url.searchParams.get('staff')==='yes';
  res.setHeader('Content-Type','text/html');
  res.end(`<!doctype html><html lang="en"><head><meta charset="utf-8">
   <script>document.cookie='csrftoken=synthetic';window.dataLayer=[];window.choice=false;window.LSAConsent={parseConsent:()=>({analytics:window.choice}),loadPurchaseTracker:()=>false};</script>
   <script src="/analytics.js" defer></script><script src="/assisted-attribution.js" defer></script></head>
   <body data-google-integrations-enabled="true" data-analytics-staff-browser="${staff}"
    data-attribution-public-page="true" data-attribution-page-kind="property_detail"
    data-attribution-property-id="synthetic-ad-apartment" data-attribution-consent-url="/analytics/attribution/consent/"
    data-attribution-whatsapp-original="https://wa.me/966500000000">
    <a href="https://wa.me/966500000000" data-analytics-event="whatsapp_click" data-analytics-lead-source="floating_button">WhatsApp</a>
   </body></html>`);
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const origin=`http://127.0.0.1:${server.address().port}`;
 const browser=await chromium.launch({executablePath:execFileSync('which',['google-chrome'],{encoding:'utf8'}).trim()});
 try{
  for(const width of [390,1366])for(const staff of [false,true]){
   const page=await browser.newPage({viewport:{width,height:900}}),before=posts.length;
   await page.route('**/*',route=>{if(route.request().url().startsWith(origin+'/'))return route.continue();external.push('blocked');return route.abort();});
   await page.goto(`${origin}/synthetic/?staff=${staff?'yes':'no'}&utm_source=google&utm_medium=cpc&utm_id=24262302633&gclid=synthetic-private-click&email=synthetic-private-email`);
   await page.waitForFunction(()=>Boolean(window.LSAAnalytics));
   assert.equal(posts.length,before,'denied state makes no capture request');
   await page.evaluate(()=>{window.choice=true;document.dispatchEvent(new CustomEvent('lsa:consent-updated',{detail:{analytics:true,marketing:false,version:'synthetic'}}));});
   if(staff){
    const result=await page.evaluate(p=>window.LSAAnalytics.pushEvent('purchase',p),purchase);
    assert.equal(result,false);assert.equal(posts.length,before);
   }else{
    await page.waitForFunction(()=>document.querySelector('a').href.includes('/contact/whatsapp/'));
    assert.equal(posts.length,before+1);
    for(const secret of ['synthetic-private-click','synthetic-private-email','gclid=','email='])assert.equal(posts.at(-1).includes(secret),false);
    const href=await page.locator('a').getAttribute('href');
    assert.ok(href.startsWith(origin+'/contact/whatsapp/'));assert.equal(href.includes('LSA-'),false);
    await page.locator('a').evaluate(el=>el.addEventListener('click',event=>event.preventDefault(),{once:true}));
    await page.locator('a').click();
    const clicked=await page.evaluate(()=>window.dataLayer.find(e=>e.event==='whatsapp_click'));
    assert.ok(clicked);assert.equal(JSON.stringify(clicked).includes('link_url'),false);
    await page.evaluate(p=>window.LSAAnalytics.pushEvent('purchase',p),purchase);
    const captured=await page.evaluate(()=>window.dataLayer.find(e=>e.event==='purchase'));
    assert.equal(captured.first_campaign_id,purchase.first_campaign_id);assert.equal(captured.last_campaign_id,purchase.last_campaign_id);
    assert.equal(captured.ecommerce.value,750);assert.equal(captured.ecommerce.first_source,undefined);
    const bad=await page.evaluate(p=>window.LSAAnalytics.pushEvent('purchase',{...p,last_campaign_id:24262302633}),purchase);assert.equal(bad,false);
    await page.evaluate(()=>{window.choice=false;document.dispatchEvent(new CustomEvent('lsa:consent-updated',{detail:{analytics:false,marketing:false,version:'synthetic'}}));});
    await page.waitForFunction(()=>document.querySelector('a').href==='https://wa.me/966500000000');
    await page.waitForFunction(()=>window.dataLayer.some(e=>e.event==='cookie_consent_updated'&&e.analytics===false));
    const last=await page.evaluate(()=>window.dataLayer.filter(e=>e.event).at(-1));
    assert.equal(last.attribution_version,null);assert.equal(last.first_campaign_id,null);
   }
   cases.push({width,staff,passed:true});await page.close();
  }
  assert.equal(external.length,0);
  const result={synthetic:true,cases,externalRequests:0,productionRequests:0,guestMessages:0};
  fs.writeFileSync(path.join(process.env.LSA_CONTACT_VISUAL_DIR,'assisted-attribution-browser-results.json'),JSON.stringify(result,null,2));
  console.log(JSON.stringify(result));
 }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1});
