/* Synthetic file-only UI checks: no login, real records or outgoing requests. */
const { chromium } = require("playwright");
const { execFileSync } = require("node:child_process");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

(async () => {
  const output = process.env.LSA_CONTACT_VISUAL_DIR;
  if (!output) throw new Error("Set LSA_CONTACT_VISUAL_DIR to the synthetic rendered pages");
  const executablePath = execFileSync("which", ["google-chrome"], { encoding: "utf8" }).trim();
  const browser = await chromium.launch({ executablePath, args: ["--allow-file-access-from-files"] });
  try {
    for (const width of [390, 820, 1366]) {
      const page = await browser.newPage({ viewport: { width, height: 900 }, colorScheme: "light" });
      await page.route(/^https?:/, route => route.abort());
      await page.addInitScript(() => {
        // Every address response is synthetic; no network request is made.
        window.fetch = async (_url, options) => {
          const body = JSON.parse(options.body);
          return { ok: true, status: 200, json: async () => body.input
            ? { suggestions: [{ place_id: "synthetic-place", label: "10 Test Road, Riyadh" }] }
            : { fields: { billing_street1: "10 Test Road", billing_city: "Riyadh", billing_state: "Riyadh", billing_country: "SA", billing_postcode: "12345" }, next_ticket: "synthetic-only" } };
        };
      });
      for (const name of ["detail", "list", "morning", "morning-create", "morning-guest"]) {
        await page.goto(pathToFileURL(path.join(output, `${name}.html`)).href);
        await page.locator(name.startsWith("morning") ? "[data-checkout-address]" : "main.lsa-contact").waitFor();
        if (name.startsWith("morning")) {
          const search = page.locator("[data-address-search]");
          await search.fill("Test road");
          await page.locator("[data-address-options] button").click();
          await page.waitForFunction(() => document.querySelector('[name="billing_city"]').value === "Riyadh");
          const street = await page.locator('[name="billing_street1"]').inputValue();
          if (street !== "10 Test Road") throw new Error("Shared address mapping failed");
          if (name === "morning-guest") {
            const state = page.locator('[name="billing_state"]');
            await state.fill("الرياض");
            if (await state.getAttribute("aria-invalid") !== "true") throw new Error("Morning immediate validation missing");
            if (!(await page.locator("#id_billing_state_error").innerText()).includes("Riyadh")) throw new Error("Morning correction hint missing");
            const before = page.url();
            await page.locator('[name="terms_accepted"]').check();
            await page.locator('[name="house_rules_accepted"]').check();
            await page.locator('form[data-morning-validation] button[type="submit"]').click();
            if (page.url() !== before) throw new Error("Invalid morning form submitted");
            await state.fill("Riyadh");
            if (await state.getAttribute("aria-invalid") !== "false") throw new Error("Corrected morning field remains blocked");
          }
        }
        const layout = await page.evaluate(() => ({
          width: window.innerWidth,
          scrollWidth: document.documentElement.scrollWidth,
          smallTargets: [...document.querySelectorAll(".lsa-contact__button, .morning-payment-send .lsa-btn")]
            .filter(el => el.getBoundingClientRect().height < 44).length,
        }));
        if (layout.scrollWidth > layout.width + 1 || layout.smallTargets) {
          throw new Error(`${name} ${width}: invalid layout ${JSON.stringify(layout)}`);
        }
        if (name === "detail") {
          if (await page.locator('[rel="next"]').count() !== 1) throw new Error("Next message missing");
          if (await page.locator('[rel="prev"]').count() !== 1) throw new Error("Previous message missing");
          await page.locator("#id_body").fill("مسودة تجريبية للمراجعة فقط");
        }
        if (name === "morning") {
          await page.locator(".morning-payment-send summary").click();
          const width = await page.evaluate(() => document.documentElement.scrollWidth);
          if (width > page.viewportSize().width + 1) throw new Error("Morning preview overflows");
        }
        await page.screenshot({ path: path.join(output, `${name}-${width}.png`), fullPage: true });
        console.log(`${name} ${width}px: no overflow, accessible buttons, synthetic content only`);
      }
      await page.close();
    }
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
