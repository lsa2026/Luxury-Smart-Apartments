/* Isolated synthetic payment pages: all external requests are blocked. */
const {chromium} = require("playwright");
const {execFileSync} = require("node:child_process");
const path = require("node:path");
const {pathToFileURL} = require("node:url");

(async () => {
  const output = process.env.LSA_CONTACT_VISUAL_DIR;
  if (!output) throw new Error("Set the synthetic layout directory");
  const executablePath = execFileSync("which", ["google-chrome"], {encoding: "utf8"}).trim();
  const browser = await chromium.launch({executablePath, args: ["--allow-file-access-from-files"]});
  try {
    for (const width of [390, 820, 1366]) {
      for (const name of ["revised-payment-owner", "payment-receipt-ar", "payment-receipt-en", "payment-receipt-fr"]) {
        const page = await browser.newPage({viewport: {width, height: 950}, colorScheme: "light"});
        await page.route(/^https?:/, route => route.abort());
        await page.goto(pathToFileURL(path.join(output, `${name}.html`)).href);
        if (name === "revised-payment-owner") {
          await page.evaluate(() => {
            window.fetch = async (_url, options) => {
              const data = JSON.parse(options.body);
              return {ok: true, json: async () => data.input
                ? {suggestions: [{place_id: "synthetic-place", label: "10 Test Road, Riyadh"}]}
                : {fields: {billing_street1: "10 Test Road", billing_city: "Riyadh", billing_state: "Riyadh", billing_country: "SA", billing_postcode: "12345"}, next_ticket: "synthetic-ticket"}};
            };
          });
          await page.addScriptTag({path: path.resolve("static/js/checkout-address.js"), type: "module"});
          await page.locator("[data-checkout-address]").waitFor({state: "visible"});
          await page.locator("[data-address-search]").fill("RRXA1234");
          await page.getByRole("option", {name: "10 Test Road, Riyadh"}).click();
          if (await page.locator('[name="billing_state"]').inputValue() !== "Riyadh") throw new Error("Address not filled");
          if (await page.locator('[name="billing_country"]').inputValue() !== "SA") throw new Error("Country not filled");
          if (await page.locator('[name="terms_accepted"]').count()) throw new Error("Owner must not accept guest terms");
        } else {
          await page.locator(".payment-outcome").waitFor();
          const logo = await page.locator(".payment-outcome__brand img").evaluate(image => ({
            loaded: image.complete && image.naturalWidth > 0,
            ratio: image.getBoundingClientRect().width / image.getBoundingClientRect().height,
          }));
          if (!logo.loaded || Math.abs(logo.ratio - 1920 / 450) > .02) throw new Error("Approved logo geometry changed");
          if (await page.locator('button[type="submit"], form').count()) throw new Error("Paid receipt must not offer another payment");
          const font = await page.locator(".payment-outcome").evaluate(node => getComputedStyle(node).fontFamily);
          if (!font.includes(name.endsWith("-ar") ? "PaymentCairo" : "PaymentInter")) throw new Error("Local payment font missing");
          for (const link of await page.locator(".payment-outcome__actions a").all()) {
            if ((await link.boundingBox()).height < 44) throw new Error("Receipt action too small");
          }
        }
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
        if (overflow) throw new Error(`${name} overflows at ${width}`);
        await page.screenshot({path: path.join(output, `${name}-${width}.png`), fullPage: true});
        console.log(`${name} ${width}px passed`);
        await page.close();
      }
    }
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
