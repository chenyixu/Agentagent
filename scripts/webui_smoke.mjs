// Browser smoke test for the self-contained Agentagent repo:
// repo-local frontend (vite/5173)  ->  webapp/ App service (8010).
//
// Run with the managed node runtime (playwright lives in its workspace):
//   NODE_PATH=~/.workbuddy/binaries/node/workspace/node_modules \
//     ~/.workbuddy/binaries/node/versions/22.22.2-3/bin/node scripts/webui_smoke.mjs
//
// Uses the system Chrome (channel: 'chrome') so no Chromium download is needed.

import { mkdirSync } from 'node:fs';

// ESM ignores NODE_PATH, so resolve playwright from the managed node workspace
// explicitly (playwright is intentionally not a dependency of this repo).
const PW_ROOT =
  process.env.PW_ROOT ??
  `${process.env.HOME}/.workbuddy/binaries/node/workspace/node_modules/playwright/index.mjs`;
const { chromium } = await import(PW_ROOT);

const FRONTEND = process.env.FRONTEND_URL ?? 'http://localhost:5173';
const BACKEND = process.env.BACKEND_URL ?? 'http://127.0.0.1:8010';
const LOGIN = process.env.LOGIN ?? 'customer-1';
const OUT = process.env.SHOT_DIR ?? 'docs/screenshots';

mkdirSync(OUT, { recursive: true });

const log = (...a) => console.log('[webcheck]', ...a);

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
const page = await context.newPage();

const consoleErrors = [];
page.on('console', (m) => {
  if (m.type() === 'error') consoleErrors.push(m.text());
});
page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message}`));

try {
  log('goto', FRONTEND);
  await page.goto(FRONTEND, { waitUntil: 'networkidle' });

  // ---- setup page: server address + login name -------------------------
  const urlInput = page.locator('input').first();
  await urlInput.waitFor({ state: 'visible', timeout: 30_000 });

  const inputs = page.locator('input');
  const n = await inputs.count();
  log('setup inputs:', n);

  await inputs.nth(0).fill(BACKEND);
  await inputs.nth(1).fill(LOGIN);

  await page.screenshot({ path: `${OUT}/webui-login.png` });

  const submit = page.getByRole('button').last();
  await submit.click();

  // ---- chat page -------------------------------------------------------
  await page.waitForSelector('textarea, [contenteditable="true"]', { timeout: 60_000 });
  log('chat page reached');
  await page.waitForTimeout(1500);

  const agents = await page.locator('text=门店预约助理').count();
  log('agent card visible:', agents > 0);

  const composer = page.locator('textarea').first();
  await composer.fill('肩颈多少钱？明天下午有空档吗？');
  await composer.press('Enter');

  log('message sent, waiting for answer...');
  // Tool card + streamed answer; be generous, a real LLM round-trip is slow.
  await page.waitForTimeout(20_000);

  for (let i = 0; i < 12; i += 1) {
    const text = await page.locator('body').innerText();
    if (/[\d,]+\s*元/.test(text) && /只读/.test(text)) break;
    await page.waitForTimeout(5000);
  }

  const body = await page.locator('body').innerText();
  const hitQuote = /[\d,]+\s*元/.test(body);
  const hitBoundary = /只读/.test(body);
  const hitToolCard = /工具/.test(body);

  await page.screenshot({ path: `${OUT}/webui-chat-tool-call.png`, fullPage: true });

  console.log('\n=== RESULT ===');
  console.log('quote in answer      :', hitQuote);
  console.log('read-only boundary   :', hitBoundary);
  console.log('tool card present    :', hitToolCard);
  console.log('console error count  :', consoleErrors.length);
  const interesting = consoleErrors.filter(
    (e) => !/knowledge_bases|handoff|refund-proposals|503|404/.test(e),
  );
  console.log('unexpected console errors:');
  for (const e of interesting.slice(0, 10)) console.log('   -', e.slice(0, 200));

  console.log('\n--- answer excerpt ---');
  console.log(body.replace(/\n{2,}/g, '\n').slice(-1800));

  const ok = hitQuote && hitBoundary && hitToolCard;
  console.log('\nVERDICT:', ok ? 'PASS' : 'FAIL');
  process.exitCode = ok ? 0 : 1;
} finally {
  await browser.close();
}
