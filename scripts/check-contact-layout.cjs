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
      for (const name of ["detail", "list"]) {
        await page.goto(pathToFileURL(path.join(output, `${name}.html`)).href);
        await page.locator("main.lsa-contact").waitFor();
        const layout = await page.evaluate(() => ({
          width: window.innerWidth,
          scrollWidth: document.documentElement.scrollWidth,
          smallTargets: [...document.querySelectorAll(".lsa-contact__button")]
            .filter(el => el.getBoundingClientRect().height < 44).length,
        }));
        if (layout.scrollWidth > layout.width + 1 || layout.smallTargets) {
          throw new Error(`${name} ${width}: invalid layout ${JSON.stringify(layout)}`);
        }
        if (name === "detail") {
          if (await page.locator('[rel="next"]').count() !== 1) throw new Error("Next message missing");
          if (await page.locator('[rel="prev"]').count() !== 1) throw new Error("Previous message missing");
          await page.locator("#id_body").fill("نص تجريبي للمعاينة فقط\nSynthetic preview only");
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
