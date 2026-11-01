// Dev-only UI screenshot helper (not an application dependency).
// Usage: npm i puppeteer-core && node scripts/screenshot_ui.mjs
import puppeteer from 'puppeteer-core';
import { mkdirSync } from 'node:fs';

const chrome = process.env.CHROME_PATH ||
  (process.platform === 'win32'
    ? 'C:/Program Files/Google/Chrome/Application/chrome.exe'
    : '/usr/bin/google-chrome');
const root = new URL('..', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
const out = `${root}/docs/ui`;
mkdirSync(out, { recursive: true });

const browser = await puppeteer.launch({
  executablePath: chrome, headless: 'new',
  args: ['--no-sandbox', '--no-proxy-server', '--hide-scrollbars'],
});
const pages = [
  { name: 'index', url: 'http://127.0.0.1:8765/?demo=followup', wait: '#paper-list article' },
  { name: 'chat', url: 'http://127.0.0.1:8765/chat?demo=followup', wait: '#turns .turn' },
];
let failed = false;
for (const pageDef of pages) {
  for (const width of [1440, 1024, 390]) {
    const page = await browser.newPage();
    await page.setViewport({ width, height: 1000, deviceScaleFactor: 1 });
    await page.goto(pageDef.url, { waitUntil: 'networkidle2', timeout: 30000 });
    try {
      await page.waitForSelector(pageDef.wait, { timeout: 20000 });
    } catch {
      console.error(`${pageDef.name}-${width}: selector ${pageDef.wait} not found`);
      failed = true;
    }
    const info = await page.evaluate(() => ({
      viewport: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    if (info.scrollWidth > info.viewport) {
      console.error(`${pageDef.name}-${width}: horizontal overflow ${info.scrollWidth} > ${info.viewport}`);
      failed = true;
    }
    await page.screenshot({ path: `${out}/${pageDef.name}-${width}-full.png`, fullPage: true });
    console.log(`${pageDef.name}-${width}: ${info.viewport}/${info.scrollWidth}`);
    await page.close();
  }
}
await browser.close();
process.exitCode = failed ? 1 : 0;
