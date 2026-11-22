/* Static synthetic guest choice pages; no provider or form submissions. */
const {chromium} = require("playwright");
const {execFileSync} = require("node:child_process");
const path = require("node:path");
const {pathToFileURL} = require("node:url");

(async () => {
  const output = process.env.LSA_CONTACT_VISUAL_DIR;
  if (!output) throw new Error("Missing synthetic layout directory");
  const executablePath = execFileSync("which", ["google-chrome"], {encoding: "utf8"}).trim();
  const browser = await chromium.launch({executablePath, args: ["--allow-file-access-from-files"]});
  try {
    for (const width of [390, 820]) {
      for (const language of ["ar", "en", "fr"]) {
        const page = await browser.newPage({viewport: {width, height: 950}, colorScheme: "light"});
        await page.route(/^https?:/, route => route.abort());
        await page.goto(pathToFileURL(path.join(output, `whatsapp-choice-07-${language}.html`)).href);
        // Exercise the real shared shell script, including splash dismissal.
        await page.addScriptTag({path: path.resolve("static/js/site.js")});
        await page.locator('[name="service"]').waitFor({state: "visible"});
        if (await page.locator('[name="service"]').isChecked()) throw new Error("Preselected consent");
        const button = page.locator('button[name="action"][value="save"]');
        await button.scrollIntoViewIfNeeded();
        // Trial performs hit testing without clicking or submitting the form.
        await button.click({trial: true});
        if ((await button.boundingBox()).height < 44) throw new Error("Choice action too small");
        if (await page.locator('[name="marketing"]').count()) throw new Error("Marketing choice leaked");
        if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)) {
          throw new Error(`Choice page overflows: ${language} ${width}`);
        }
        await page.screenshot({path: path.join(output, `whatsapp-choice-${language}-${width}.png`), fullPage: true});
        console.log(`WhatsApp choice ${language} ${width}px: unchecked, reachable, no overflow`);
        await page.close();
      }
    }
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
