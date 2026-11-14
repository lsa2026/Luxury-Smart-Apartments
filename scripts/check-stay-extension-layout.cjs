/* Synthetic file pages only. No guest login, booking, payment, or network calls. */
const { chromium } = require("playwright");
const { execFileSync } = require("node:child_process");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

(async () => {
  const output = process.env.LSA_CONTACT_VISUAL_DIR;
  if (!output) throw new Error("Synthetic output directory required");
  const executablePath = execFileSync("which", ["google-chrome"], { encoding: "utf8" }).trim();
  const browser = await chromium.launch({ executablePath, args: ["--allow-file-access-from-files"] });
  try {
    for (const colorScheme of ["light", "dark"]) {
      for (const width of [390, 820, 1366]) {
        const page = await browser.newPage({ viewport: { width, height: 1000 }, colorScheme });
        await page.route(/^https?:/, route => route.abort());
        for (const name of ["sources", "selection", "guest", "review", "unpaid"]) {
          await page.goto(pathToFileURL(path.join(output, `extension-${name}.html`)).href);
          await page.locator("main.lsa-extension").waitFor();
          const facts = await page.evaluate(() => ({
            width: innerWidth, scroll: document.documentElement.scrollWidth,
            shortButtons: [...document.querySelectorAll("main .lsa-btn")].some(el => el.getBoundingClientRect().height < 43),
          }));
          if (facts.scroll > facts.width + 1 || facts.shortButtons) throw new Error(`${name} ${width}: ${JSON.stringify(facts)}`);
          if (name === "selection" && await page.locator('[name="guest_phone_country"]').count() !== 1) throw new Error("Shared phone country missing");
          if (name === "review" && await page.locator('[name="confirm_independent_booking"]').count() !== 1) throw new Error("Final review missing");
          if (name === "unpaid" && (await page.locator("main.lsa-extension").innerText()).includes("التمديد ساري بعد التحقق")) throw new Error("Unpaid stay claimed active");
          await page.screenshot({ path: path.join(output, `extension-${name}-${width}-${colorScheme}.png`), fullPage: true });
        }
        await page.close();
      }
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
