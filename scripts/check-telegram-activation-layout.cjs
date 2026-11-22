const assert = require("node:assert/strict");
const { chromium } = require("playwright");
const { execFileSync } = require("node:child_process");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
(async () => {
    const output = process.env.LSA_CONTACT_VISUAL_DIR;
    const browser = await chromium.launch({executablePath: execFileSync("which", ["google-chrome"], {encoding: "utf8"}).trim(), args: ["--allow-file-access-from-files"]});
    try {
        for (const fixture of ["ar-issued", "en-issued", "fr-issued", "ar-bound", "ar-expired", "ar-unverified", "ar-admin"]) {
            for (const width of [390, 820]) {
                const page = await browser.newPage({viewport: {width, height: 900}});
                await page.route(/^https?:/, route => route.abort());
                await page.goto(pathToFileURL(path.join(output, `telegram-${fixture}.html`)).href);
                const card = page.locator(".telegram-activation-card");
                assert.ok(await card.isVisible());
                assert.equal(await page.locator("[data-brand-splash]").count(), 0);
                await card.scrollIntoViewIfNeeded();
                assert.ok(await card.evaluate(el => { const r = el.getBoundingClientRect(); return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + Math.min(30, r.height / 2))); }), `${fixture} is covered at ${width}`);
                assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `${fixture} overflows at ${width}`);
                assert.equal(await card.locator("a").count(), fixture.endsWith("issued") ? 1 : 0);
                if (fixture.endsWith("issued")) {
                    const link = card.locator("a");
                    assert.equal(await link.getAttribute("rel"), "noreferrer noopener nofollow");
                    assert.ok((await link.boundingBox()).height >= 44);
                }
                await page.screenshot({path: path.join(output, `telegram-${fixture}-${width}.png`), fullPage: true});
                await page.close();
                console.log(`Telegram synthetic ${fixture} ${width}px passed; no external requests or clicks`);
            }
        }
    } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
