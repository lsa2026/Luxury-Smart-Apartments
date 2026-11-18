/* Actual Chromium, isolated synthetic markup + production scripts. No external requests. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

(async () => {
    const output = process.env.LSA_CONTACT_VISUAL_DIR;
    assert.ok(output);
    const contexts = {measurement_version: 'pp1', attempt_id: 'a'.repeat(32), parent_attempt_id: 'b'.repeat(32),
        request_kind: 'property_quote', entry_page: 'availability_results', journey_entry: 'home',
        page_kind: 'quote', stay_kind: 'overnight', result_status: 'available', display_status: 'available',
        result_count: 1, checked_count: 1, error_count: 0, error_category: 'none'};
    const server = http.createServer((req, res) => {
        const url = new URL(req.url, 'http://localhost');
        if (req.method !== 'GET') { res.writeHead(405).end(); return; }
        if (['/analytics.js', '/prepurchase.js'].includes(url.pathname)) {
            res.setHeader('Content-Type', 'text/javascript');
            res.end(fs.readFileSync(path.join('static/js', url.pathname.slice(1)))); return;
        }
        if (url.pathname !== '/synthetic/') { res.writeHead(404).end(); return; }
        const currency = url.searchParams.get('currency') === 'EUR' ? 'EUR' : 'SAR';
        const value = currency === 'EUR' ? '100' : '500';
        const language = ['ar', 'en', 'fr'].includes(url.searchParams.get('lang')) ? url.searchParams.get('lang') : 'en';
        res.setHeader('Content-Type', 'text/html');
        res.end(`<!doctype html><html lang="${language}"><head><meta charset="utf-8">
          <style>body{margin:24px;font:20px sans-serif}p,strong{display:block;padding:15px}.spacer{height:1500px}</style>
          <script>window.dataLayer=[];window.syntheticConsent=false;window.LSAConsent={parseConsent:()=>({analytics:window.syntheticConsent})};</script>
          <script src="/analytics.js" defer></script><script src="/prepurchase.js" defer></script></head>
          <body data-google-integrations-enabled="true" data-prepurchase-page-kind="quote">
          <script type="application/json" id="lsa-prepurchase-context">${JSON.stringify(contexts)}</script>
          <p data-pp-result>Available / متاحة / Disponible</p><div class="spacer"></div>
          <strong data-pp-price data-pp-property-id="synthetic-apartment" data-pp-property-name="Synthetic Apartment"
            data-pp-price-context="quote_total" data-pp-currency="${currency}" data-pp-value="${value}"
            data-pp-source-currency="SAR" data-pp-source-value="500">${currency} ${value}</strong>
          </body></html>`);
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const origin = `http://127.0.0.1:${server.address().port}`;
    const executablePath = execFileSync('which', ['google-chrome'], {encoding: 'utf8'}).trim();
    const browser = await chromium.launch({executablePath});
    const cases = [];
    const external = [];
    try {
        for (const language of ['ar', 'en', 'fr']) for (const width of [390, 820, 1366]) {
            const page = await browser.newPage({viewport: {width, height: 800}});
            await page.route('**/*', route => {
                if (route.request().url().startsWith(origin + '/')) return route.continue();
                external.push('blocked'); return route.abort();
            });
            const eventCount = name => page.evaluate(event => window.dataLayer.filter(e => e.event === event).length, name);
            await page.goto(`${origin}/synthetic/?lang=${language}`);
            await page.waitForFunction(() => Boolean(window.LSAAnalytics));
            await page.waitForTimeout(1100);
            assert.equal(await eventCount('availability_result_view'), 0);
            assert.equal(await eventCount('price_view'), 0);
            await page.evaluate(() => { window.syntheticConsent = true; document.dispatchEvent(new CustomEvent('lsa:consent-updated', {detail: {analytics: true, marketing: false, version: 'synthetic'}})); });
            await page.waitForFunction(() => window.dataLayer.some(e => e.event === 'availability_result_view'));
            assert.equal(await eventCount('price_view'), 0, 'offscreen price is not seen');
            await page.locator('[data-pp-price]').scrollIntoViewIfNeeded();
            await page.waitForTimeout(400);
            assert.equal(await eventCount('price_view'), 0, 'dwell is not immediate');
            await page.waitForFunction(() => window.dataLayer.some(e => e.event === 'price_view'));
            assert.equal(await eventCount('price_view'), 1);
            await page.reload();
            await page.evaluate(() => { window.syntheticConsent = true; document.dispatchEvent(new CustomEvent('lsa:consent-updated', {detail: {analytics: true, marketing: false, version: 'synthetic'}})); });
            await page.locator('[data-pp-price]').scrollIntoViewIfNeeded();
            await page.waitForTimeout(1200);
            assert.equal(await eventCount('price_view'), 0, 'refresh does not duplicate');
            await page.goto(`${origin}/synthetic/?lang=${language}&currency=EUR`);
            await page.evaluate(() => { window.syntheticConsent = true; document.dispatchEvent(new CustomEvent('lsa:consent-updated', {detail: {analytics: true, marketing: false, version: 'synthetic'}})); });
            await page.locator('[data-pp-price]').scrollIntoViewIfNeeded();
            await page.waitForFunction(() => window.dataLayer.some(e => e.event === 'price_view'));
            const event = await page.evaluate(() => window.dataLayer.find(e => e.event === 'price_view'));
            assert.equal(event.currency, 'EUR'); assert.equal(event.value, 100);
            assert.equal(event.source_currency, 'SAR'); assert.equal(event.source_value, 500);
            assert.equal(event.parent_attempt_id, contexts.parent_attempt_id);
            cases.push({language, width, deniedSuppressed: true, offscreenSuppressed: true,
                continuousDwell: true, refreshDeduplicated: true, displayedCurrencyCorrect: true});
            await page.close();
        }
        assert.equal(external.length, 0);
        fs.writeFileSync(path.join(output, 'prepurchase-browser-results.json'), JSON.stringify({synthetic: true, cases,
            productionRequests: 0, externalRequests: 0, purchases: 0}, null, 2));
        console.log(JSON.stringify({synthetic: true, passed: cases.length, externalRequests: external.length}));
    } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; });
