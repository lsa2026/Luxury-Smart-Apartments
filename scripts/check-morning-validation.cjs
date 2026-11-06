/* File-only synthetic billing forms. Network and form submissions are blocked. */
const { chromium } = require('playwright');
const { execFileSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');
const path = require('node:path');
(async () => {
    const output = process.env.LSA_CONTACT_VISUAL_DIR;
    if (!output) throw new Error('Synthetic layout directory required');
    const browser = await chromium.launch({executablePath: execFileSync('which', ['google-chrome'], {encoding:'utf8'}).trim(), args:['--allow-file-access-from-files']});
    try {
        for (const width of [390, 820, 1366]) {
            const page = await browser.newPage({viewport:{width,height:1000}, colorScheme:'light'});
            await page.route(/^https?:/, route => route.abort());
            for (const name of ['morning', 'morning-create', 'morning-guest']) {
                await page.goto(pathToFileURL(path.join(output,`${name}.html`)).href);
                const postcode = page.locator('[name="billing_postcode"]');
                await postcode.waitFor();
                await page.waitForFunction(() => [...document.querySelectorAll('form')].some(form => form.dataset.morningValidationReady === 'true'));
                await page.locator('[name="billing_country"]').selectOption('SA');
                await postcode.fill('WrongRoad');
                await postcode.blur();
                const error = page.locator('#id_billing_postcode_error');
                if (!(await error.innerText()).includes('5 أرقام')) throw new Error(`${name}: missing specific Saudi postcode error`);
                if (await postcode.getAttribute('aria-invalid') !== 'true') throw new Error('Invalid field not exposed to accessibility');
                const rule = page.locator('#id_billing_postcode_rule');
                if (!(await rule.isVisible()) || !(await rule.innerText()).includes('5 أرقام')) throw new Error('Persistent rule missing');
                if (await rule.evaluate(el=>Number(getComputedStyle(el).fontWeight)) < 600) throw new Error('Rule not bold');
                await page.screenshot({path:path.join(output,`${name}-validation-error-${width}.png`), fullPage:true});
                await postcode.fill('13221');
                if ((await error.innerText()).trim()) throw new Error('Corrected postcode error did not clear');
                if (!(await rule.isVisible())) throw new Error('Rule disappeared after input');
                await page.locator('[name="billing_country"]').selectOption('GB');
                await postcode.fill('SW1A 1AA');
                if ((await error.innerText()).trim()) throw new Error('Valid international postcode rejected');
                const layout = await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));
                if (layout.scroll > layout.width + 1) throw new Error(`${name} overflows at ${width}`);
                console.log(`${name} ${width}px: specific error, correction, persistent bold hint, country change, no overflow`);
            }
            await page.close();
        }
    } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
