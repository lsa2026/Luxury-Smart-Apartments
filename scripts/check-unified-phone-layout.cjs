/* Disposable synthetic form only; all network requests blocked. */
const assert = require("node:assert/strict");
const { chromium } = require("playwright");
const { execFileSync } = require("node:child_process");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

(async () => {
    const output = process.env.LSA_CONTACT_VISUAL_DIR;
    if (!output) throw Error("Synthetic layout directory required");
    const executablePath = execFileSync("which", ["google-chrome"], {encoding: "utf8"}).trim();
    const browser = await chromium.launch({executablePath, args: ["--allow-file-access-from-files"]});
    try {
        for (const width of [390, 820, 1366]) {
            const page = await browser.newPage({viewport: {width, height: 900}});
            await page.route(/^https?:/, route => route.abort());
            await page.addInitScript(() => {
                localStorage.setItem("lsa-phone-country-v1", "GB");
                window.syntheticLookups = 0;
                window.fetch = async (url, options) => {
                    if (options.method !== "POST" || options.credentials !== "same-origin") throw Error("Unsafe lookup");
                    if (url.includes("guest-classification")) {
                        if (url.includes("example") || url.includes("966")) throw Error("Contacts leaked in URL");
                        window.syntheticLookups++;
                        return {ok: true, json: async () => ({status: "complete", classification: {label: "ضيف عائد", basis: "تطابق الجوال الدولي في هوستاوي"}})};
                    }
                    const raw = options.body.get("phone").replace(/[\s()-]/g, "");
                    const foreign = options.body.get("country") === "GB" || raw.startsWith("+44") || raw.startsWith("0044");
                    const phone = foreign ? "+442079460958" : "+966501234567";
                    return {ok: true, json: async () => ({phone, country: foreign ? "GB" : "SA"})};
                };
            });
            await page.goto(pathToFileURL(path.join(output, "unified-phone.html")).href);
            const form = page.locator("form[data-morning-validation]");
            const country = form.locator('[name="billing_country"]');
            const phone = form.locator('[name="guest_phone"]');
            const phoneCountry = form.locator('[name="guest_phone_country"]');
            await page.waitForFunction(() => !!document.querySelector("form[data-morning-validation]")?.validateGuestPhone);
            assert.equal(await phoneCountry.inputValue(), "SA", "Saudi default must ignore an old preference");
            assert.equal(await country.inputValue(), "SA");
            assert.equal(await country.evaluate(el => el.checkValidity()), true);
            assert.notEqual(await country.evaluate(el => getComputedStyle(el).color), "rgb(161, 33, 33)", "Valid country must not look erroneous");
            await country.evaluate(el => { el.add(new Option("", "")); el.value = ""; el.dispatchEvent(new Event("change", {bubbles: true})); });
            assert.equal(await country.evaluate(el => el.checkValidity()), false);
            await country.selectOption("SA");
            assert.equal(await country.evaluate(el => el.checkValidity()), true, "Correcting country clears custom validity");
            for (const value of ["0501234567", "00966501234567"]) {
                await phone.fill(value);
                await page.waitForFunction(() => document.querySelector('[name="guest_phone"]').value === "+966501234567");
            }
            await phone.fill("00442079460958");
            await page.waitForFunction(() => document.querySelector('[name="guest_phone_country"]').value === "GB");
            assert.equal(await phone.inputValue(), "+442079460958");
            await phone.fill("02079460958");
            await phone.blur();
            await page.waitForFunction(() => document.querySelector('[name="guest_phone"]').value === "+442079460958");
            await page.evaluate(() => {
                window.syntheticSubmits = 0;
                document.querySelector("form[data-morning-validation]").addEventListener("submit", event => {
                    if (!event.defaultPrevented) window.syntheticSubmits++;
                    event.preventDefault();
                });
            });
            await form.locator("[data-check-guest]").click();
            await page.waitForFunction(() => document.querySelector("[data-guest-check-result]").textContent.includes("ضيف عائد"));
            assert.equal(await page.evaluate(() => window.syntheticSubmits), 0, "Check button must never create a booking");
            assert.equal(await page.evaluate(() => window.syntheticLookups), 1);
            await form.locator('button[type="submit"]').click();
            assert.equal(await page.evaluate(() => window.syntheticSubmits), 1, "Full valid form can submit after country correction");
            await page.screenshot({path: path.join(output, `unified-phone-${width}.png`), fullPage: true});
            const overflow = await page.evaluate(() => [...document.querySelectorAll("body *")].map(el => {
                const r = el.getBoundingClientRect();
                return {tag: el.tagName, id: el.id, class: el.className, name: el.name, x: Math.round(r.x), width: Math.round(r.width), right: Math.round(r.right)};
            }).filter(r => r.width && (r.right > innerWidth + 1 || r.x < -1)).slice(0, 25));
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `Overflow at ${width}: ${JSON.stringify(overflow)}`);
            await page.close();
            console.log(`Unified booking phone ${width}px: valid country recovery, automatic correction, private guest check and native submit passed`);
        }
    } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
