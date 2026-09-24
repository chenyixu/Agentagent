// Live browser acceptance against the actual local API, PostgreSQL schema and DeepSeek runtime.
// It forwards every appointment request to the service. By default it deliberately drops
// only the confirmation HTTP response after the upstream service has committed it.

import fs from 'node:fs';
import path from 'node:path';

const PW_ROOT = process.env.PW_ROOT ??
  '/Users/chenyx/.workbuddy/binaries/node/workspace/node_modules/playwright/index.mjs';
const { chromium } = await import(PW_ROOT);
const FRONTEND = process.env.FRONTEND_URL ?? 'http://127.0.0.1:5173';
const API_ORIGIN = process.env.API_ORIGIN ?? 'http://127.0.0.1:8010';
const USER_ID = process.env.BROWSER_USER_ID ?? 'customer-1';
const REQUEST_TEXT = process.env.BOOKING_REQUEST ?? '我想约肩颈舒缓，明天下午15点';
const RESULT_PATH = process.env.BROWSER_RESULT_PATH;
if (!RESULT_PATH) throw new Error('BROWSER_RESULT_PATH is required');

const result = {
  kind: 'live_browser_booking_acceptance',
  frontend_origin: FRONTEND,
  api_origin: API_ORIGIN,
  username: USER_ID,
  request_text: REQUEST_TEXT,
  actual_api_requests: [],
  confirmation_post_count: 0,
  confirmation_response_deliberately_dropped_after_upstream_commit: true,
  confirmation_upstream_status: null,
  confirmation_idempotency_key: null,
  task_id: null,
  turns: [],
  page_errors: [],
  failed_requests: [],
  passed: false,
};

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addInitScript(({ apiOrigin, userId }) => {
  localStorage.setItem('server_url', apiOrigin);
  localStorage.setItem('username', userId);
  localStorage.setItem('auth_mode', 'development');
}, { apiOrigin: API_ORIGIN, userId: USER_ID });

let page;
try {
  page = await context.newPage();
  page.on('pageerror', (error) => result.page_errors.push(error.message));
  page.on('requestfailed', (request) => {
    const url = request.url();
    if (url.startsWith(API_ORIGIN) && !url.includes('/booking/v1/confirmations')) {
      result.failed_requests.push({ method: request.method(), path: new URL(url).pathname, failure: request.failure()?.errorText });
    }
  });
  page.on('response', (response) => {
    const url = response.url();
    if (url.startsWith(API_ORIGIN)) {
      result.actual_api_requests.push({
        method: response.request().method(),
        path: new URL(url).pathname,
        status: response.status(),
      });
    }
  });

  await context.route(`${API_ORIGIN}/booking/v1/confirmations`, async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    result.confirmation_post_count += 1;
    const payload = route.request().postDataJSON();
    result.confirmation_idempotency_key = payload.idempotency_key;
    const upstream = await route.fetch();
    result.confirmation_upstream_status = upstream.status();
    const body = await upstream.json();
    result.upstream_confirmation = {
      operation_id: body.operation_id,
      appointment_id: body.appointment_id,
      status: body.status,
    };
    // Simulate the client losing the response after the real service commits the write.
    await route.abort('failed');
  });

  await page.goto(`${FRONTEND}/appointment`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: '智能预约' }).waitFor({ timeout: 20_000 });
  await page.getByLabel('预约需求').fill(REQUEST_TEXT);
  const firstTurnResponse = page.waitForResponse((response) =>
    response.url() === `${API_ORIGIN}/booking/v1/messages` && response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '提交需求' }).click();
  let firstTurn = await (await firstTurnResponse).json();
  result.task_id = firstTurn.task_id;
  result.turns.push({ state: firstTurn.task_state, version: firstTurn.task_version, has_reply: !!firstTurn.reply_text });

  if (firstTurn.task_state === 'PROPOSED') {
    if (!firstTurn.reply_text || !/可约|方案|候选|找到/.test(firstTurn.reply_text)) {
      throw new Error('PROPOSED task did not return a user-facing candidate summary');
    }
    await page.getByLabel('预约需求').fill('第一个可以，请先为我保留并展示最终确认方案');
    const selectionResponse = page.waitForResponse((response) =>
      response.url() === `${API_ORIGIN}/booking/v1/messages` && response.request().method() === 'POST',
    );
    await page.getByRole('button', { name: '提交需求' }).click();
    const selection = await (await selectionResponse).json();
    result.turns.push({ state: selection.task_state, version: selection.task_version, has_pending_confirmation: !!selection.pending_confirmation });
    firstTurn = selection;
  }

  if (firstTurn.task_state !== 'WAITING_CONFIRMATION') {
    throw new Error(`expected WAITING_CONFIRMATION before user action, got ${firstTurn.task_state}`);
  }
  await page.locator('[data-slot="card-title"]').filter({ hasText: '请核对预约方案' }).waitFor({ timeout: 30_000 });
  const beforeConfirmStorage = await page.evaluate(() => JSON.stringify({
    local: Object.entries(localStorage),
    session: Object.entries(sessionStorage),
  }));
  if (beforeConfirmStorage.includes(firstTurn.pending_confirmation?.confirmation_token ?? '\u0000')) {
    throw new Error('confirmation credential was persisted in browser storage');
  }
  if (result.confirmation_post_count !== 0) throw new Error('write occurred before explicit user confirmation');

  await page.getByRole('button', { name: '确认预约' }).click();
  await page.locator('[data-slot="card-title"]').filter({ hasText: '预约已确认' }).waitFor({ timeout: 45_000 });
  if (result.confirmation_upstream_status !== 200) {
    throw new Error(`live confirmation endpoint returned ${result.confirmation_upstream_status}`);
  }
  if (!result.upstream_confirmation?.appointment_id || !result.confirmation_idempotency_key) {
    throw new Error('confirmation response or idempotency key is missing');
  }

  // The app must reconcile the committed operation by the same key without a second POST.
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.locator('[data-slot="card-title"]').filter({ hasText: '预约已确认' }).waitFor({ timeout: 30_000 });
  await page.getByText(result.upstream_confirmation.appointment_id, { exact: true }).waitFor();
  if (result.confirmation_post_count !== 1) throw new Error('duplicate confirmation POST detected');
  if (result.page_errors.length) throw new Error(`browser page errors: ${result.page_errors.join('; ')}`);
  if (result.failed_requests.length) throw new Error(`unexpected live API failures: ${JSON.stringify(result.failed_requests)}`);

  const storageAfterCommit = await page.evaluate(() => JSON.stringify({
    local: Object.entries(localStorage),
    session: Object.entries(sessionStorage),
  }));
  if (storageAfterCommit.includes(firstTurn.pending_confirmation?.confirmation_token ?? '\u0000')) {
    throw new Error('confirmation credential was persisted after commit');
  }
  result.passed = true;
  result.checks = [
    'browser requests reached the live FastAPI service',
    'real DeepSeek runtime produced a user-visible appointment candidate',
    'confirmation required explicit user click',
    'confirmation credential stayed out of browser storage',
    'real confirmation committed before its response was deliberately dropped',
    'operation lookup and page reload recovered the committed appointment',
    'same idempotency attempt did not issue a duplicate confirmation POST',
  ];
} catch (error) {
  result.error = error instanceof Error ? error.message : String(error);
} finally {
  await context.close();
  await browser.close();
  fs.mkdirSync(path.dirname(RESULT_PATH), { recursive: true });
  fs.writeFileSync(RESULT_PATH, `${JSON.stringify(result, null, 2)}\n`, { flag: 'wx' });
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}

if (!result.passed) process.exitCode = 1;
