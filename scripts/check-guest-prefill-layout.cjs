/* Synthetic form and fake responses only; no guest, authentication or provider IO. */
const {chromium} = require('playwright');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const data = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
(async () => {
    const output = process.env.LSA_CONTACT_VISUAL_DIR;
    if (!output) throw Error('Synthetic output required');
    const executablePath = process.env.LSA_TEST_CHROME || execFileSync('which', ['google-chrome'], {encoding:'utf8'}).trim();
    const address = data(fs.readFileSync('static/js/checkout-address.js', 'utf8'));
    const module = fs.readFileSync('static/js/admin-guest-prefill.js', 'utf8').replace('"./checkout-address.js"', JSON.stringify(address));
    const browser = await chromium.launch({executablePath, args:['--allow-file-access-from-files']});
    const results = [];
    try {
        for (const name of ['guest-prefill', 'extension-selection', 'extension-guest'])
        for (const colorScheme of ['light', 'dark']) for (const width of [390, 820, 1366]) {
            const page = await browser.newPage({viewport:{width, height:1000}, colorScheme});
            await page.route(/^https?:/, route => route.abort());
            await page.goto(pathToFileURL(path.join(output, `${name}.html`)).href);
            await page.evaluate(() => {
                window.fetch = async () => ({ok:true, json:async () => ({status:'found', candidates:[{
                    source_label:'حجز سابق تجريبي', saved_at:'2026-11-01', address_kind:'billing',
                    address_available:true, address_complete:true, fields:{
                        guest_name:'ضيف تجريبي', guest_first_name:'ضيف', guest_last_name:'تجريبي',
                        guest_email:'synthetic@example.invalid', guest_phone:'+16502530000', guest_phone_country:'US', guest_language:'ar',
                        billing_street1:'King Road 10', billing_city:'Riyadh', billing_state:'Riyadh', billing_country:'SA', billing_postcode:'12345',
                    },
                }]})});
            });
            await page.addScriptTag({type:'module', content:module});
            await page.locator('[name="guest_email"]').fill('synthetic@example.invalid');
            const priceField = page.locator('[name="final_total_price"]');
            const price = await priceField.count() ? await priceField.inputValue() : null;
            await page.locator('[data-load-saved-guest]').click();
            await page.locator('[data-saved-guest-choices]').waitFor({state:'visible'});
            for (const selector of ['[data-load-saved-guest]', '[data-saved-guest-choice]', '[data-apply-saved-guest]', '[data-apply-saved-address]']) {
                const bounds = await page.locator(selector).boundingBox();
                if (!bounds || bounds.height < 43 || bounds.x < -1 || bounds.x+bounds.width > width+1) throw Error(`Unusable ${selector} at ${width}`);
            }
            if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth+1)) throw Error(`Overflow at ${width}`);
            await page.locator('[data-saved-guest-choices]').screenshot({path:path.join(output, `guest-prefill-${name}-${colorScheme}-${width}.png`)});
            await page.locator('[data-apply-saved-guest]').click();
            if (await page.locator('[name="guest_first_name"]').inputValue() !== 'ضيف' ||
                await page.locator('[name="billing_street1"]').inputValue() !== 'King Road 10' ||
                price !== null && await priceField.inputValue() !== price) throw Error('Restoration changed wrong fields');
            results.push({name, width, colorScheme, restored:true, price_unchanged:price===null?null:true, overflow:false});
            await page.close();
        }
    } finally {await browser.close();}
    fs.writeFileSync(path.join(output,'guest-prefill-browser-results.json'), JSON.stringify({synthetic_only:true, results},null,2));
    console.log(`Saved guest recovery: ${results.length} responsive browser cases passed`);
})().catch(error => {console.error(error); process.exit(1);});
