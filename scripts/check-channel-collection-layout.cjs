/* Synthetic admin pages only. No provider/guest/site network requests. */
const {chromium} = require('playwright');
const {execFileSync} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const {pathToFileURL} = require('node:url');
(async () => {
  const dir = process.env.LSA_CONTACT_VISUAL_DIR;
  if (!dir) throw Error('Missing synthetic fixture directory');
  const browser = await chromium.launch({executablePath:execFileSync('which',['google-chrome'],{encoding:'utf8'}).trim(), args:['--allow-file-access-from-files']});
  const results=[];
  try {
    for(const width of [390,820,1366]) {
      for(const name of ['channel-list','channel-draft','channel-pending','channel-succeeded']) {
        const page=await browser.newPage({viewport:{width,height:950},colorScheme:'light'});
        await page.route(/^https?:/, route=>route.abort());
        await page.goto(pathToFileURL(path.join(dir,name+'.html')).href);
        await page.locator('main.lsa-operations-screen').waitFor();
        if(await page.locator('[name="action"][value="start"]').count()) throw Error('Unapproved debit action exposed');
        if(await page.locator('input[name="card_number"],input[name="cvc"],.paymentWidgets').count()) throw Error('Card entry leaked into admin preparation');
        if(name==='channel-list') {
          await page.evaluate(()=>{
            window.fetch=async (_url,options)=>{
              const data=JSON.parse(options.body);
              return {ok:true,json:async()=>data.input?{suggestions:[{place_id:'synthetic-place',label:'10 Test Road, Riyadh'}]}:{fields:{billing_street1:'10 Test Road',billing_city:'Riyadh',billing_state:'Riyadh',billing_country:'SA',billing_postcode:'12345'},next_ticket:'synthetic-ticket'}};
            };
          });
          await page.addScriptTag({path:path.resolve('static/js/checkout-address.js'),type:'module'});
          await page.addScriptTag({path:path.resolve('static/js/morning-validation.js')});
          await page.locator('[data-address-search]').fill('RRXA1234');
          await page.getByRole('option',{name:'10 Test Road, Riyadh'}).click();
          if(await page.locator('[name="billing_state"]').inputValue()!=='Riyadh') throw Error('Shared address mapping failed');
          if(await page.locator('[name="terms_accepted"]').count()) throw Error('Manager must not accept guest terms');
          await page.locator('[name="amount"]').fill('670.241');
          if(await page.locator('[name="amount"]').evaluate(el=>el.validity.valid)) throw Error('Excess amount precision accepted');
          await page.locator('[name="amount"]').fill('670.24');
        }
        if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1)) throw Error(name+' overflow '+width);
        await page.screenshot({path:path.join(dir,name+'-'+width+'.png'),fullPage:true});
        results.push({page:name,width,passed:true});
        await page.close();
      }
    }
  } finally {await browser.close();}
  fs.writeFileSync(path.join(dir,'channel-collection-browser-results.json'),JSON.stringify({synthetic:true,results,realNetworkRequests:0,cardTests:0,charges:0},null,2));
  console.log(JSON.stringify({synthetic:true,passed:results.length,realNetworkRequests:0,charges:0}));
})().catch(error=>{console.error(error);process.exit(1);});
