// Deterministic browser acceptance for the appointment page.
// The browser talks to a Playwright-mocked API, so this verifies UI behavior
// and recovery contracts without mutating the local test database or calling a model.
//
// Run the Vite server in another terminal, then:
//   PW_ROOT=/Users/chenyx/.workbuddy/binaries/node/workspace/node_modules/playwright/index.mjs \
//     /Users/chenyx/.workbuddy/binaries/node/versions/22.22.2-3/bin/node scripts/appointment_ui_smoke.mjs

const PW_ROOT =
  process.env.PW_ROOT ??
  '/Users/chenyx/.workbuddy/binaries/node/workspace/node_modules/playwright/index.mjs';
const { chromium } = await import(PW_ROOT);

const FRONTEND = process.env.FRONTEND_URL ?? 'http://127.0.0.1:5173';
const API_ORIGIN = 'http://api.test';
const TASK_ID = 'task-ui-appointment-001';
const CONFIRMATION_TOKEN = 'CONFIRM.ui-only-secret';
const APPOINTMENT_ID = 'appt-ui-001';

let committed = false;
let confirmationPosts = 0;
let operationReads = 0;
let confirmationBody = null;

const credential = {
  proposal_id: 'proposal-ui-001',
  proposal_version: 3,
  confirmation_token: CONFIRMATION_TOKEN,
  expected_task_version: 4,
  expires_at: '2026-10-01T06:30:00+08:00',
};

function taskSnapshot() {
  return {
    task_id: TASK_ID,
    state: committed ? 'SUCCEEDED' : 'WAITING_CONFIRMATION',
    version: committed ? 5 : 4,
    event_cursor: committed ? 3 : 1,
    store_id: 'store-ui-001',
    waiting: null,
    proposal: {
      proposal_id: credential.proposal_id,
      version: credential.proposal_version,
      content: {
        action: 'CREATE',
        service_name: '肩颈舒缓',
        duration_minutes: 60,
        start_at: '2026-10-01T07:00:00+08:00',
        end_at: '2026-10-01T08:00:00+08:00',
        amount_minor: 26800,
        currency: 'CNY',
        currency_exponent: 2,
      },
    },
    appointment: committed
      ? {
          appointment_id: APPOINTMENT_ID,
          status: 'CONFIRMED',
          start_at: '2026-10-01T07:00:00+08:00',
          end_at: '2026-10-01T08:00:00+08:00',
          amount_minor: 26800,
          currency: 'CNY',
          service_snapshot: { service_name: '肩颈舒缓' },
        }
      : null,
  };
}

function json(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    headers: { 'access-control-allow-origin': '*' },
    body: JSON.stringify(body),
  });
}

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addInitScript(() => {
  localStorage.setItem('server_url', 'http://api.test');
  localStorage.setItem('username', 'customer-1');
  localStorage.setItem('auth_mode', 'development');
});

const page = await context.newPage();
const pageErrors = [];
page.on('pageerror', (error) => pageErrors.push(error.message));

await context.route(`${API_ORIGIN}/**`, async (route) => {
  const request = route.request();
  const url = new URL(request.url());
  const path = url.pathname;

  if (request.method() === 'OPTIONS') {
    return route.fulfill({
      status: 204,
      headers: {
        'access-control-allow-origin': '*',
        'access-control-allow-methods': 'GET,POST,OPTIONS',
        'access-control-allow-headers': 'content-type,x-user-id',
      },
    });
  }

  if (path === '/auth/config' && request.method() === 'GET') {
    return json(route, {
      mode: 'development',
      token_required: false,
      development_identity_enabled: true,
      refresh_endpoint_configured: false,
    });
  }

  if (path === '/booking/v1/context' && request.method() === 'GET') {
    return json(route, { store_id: 'store-ui-001' });
  }

  if (path === '/booking/v1/messages' && request.method() === 'POST') {
    const body = request.postDataJSON();
    if (body.text !== '预约肩颈舒缓，明天下午三点') {
      return json(route, { detail: `unexpected request text: ${body.text}` }, 400);
    }
    return json(route, {
      task_id: TASK_ID,
      task_state: 'WAITING_CONFIRMATION',
      task_version: 4,
      reply_text: '我找到了一个符合条件的方案，请核对后确认。',
      clarification_question: null,
      pending_confirmation: credential,
      event_cursor: 1,
    });
  }

  if (path === `/booking/v1/tasks/${TASK_ID}` && request.method() === 'GET') {
    return json(route, taskSnapshot());
  }

  if (
    path === `/booking/v1/tasks/${TASK_ID}/confirmation-credential` &&
    request.method() === 'POST'
  ) {
    if (committed) return json(route, { detail: 'confirmation is no longer pending' }, 409);
    return json(route, credential);
  }

  if (path === `/booking/v1/tasks/${TASK_ID}/events` && request.method() === 'GET') {
    return route.fulfill({
      status: 200,
      headers: {
        'access-control-allow-origin': '*',
        'content-type': 'text/event-stream; charset=utf-8',
        'cache-control': 'no-cache',
      },
      body: `id: 1\nevent: reply_end\ndata: ${JSON.stringify({
        task_id: TASK_ID,
        sequence: 1,
        type: 'reply_end',
        task_version: 4,
      })}\n\n`,
    });
  }

  if (path === '/booking/v1/confirmations' && request.method() === 'POST') {
    confirmationPosts += 1;
    confirmationBody = request.postDataJSON();
    if (confirmationBody.confirmation_token !== CONFIRMATION_TOKEN) {
      return json(route, { detail: 'wrong confirmation credential' }, 403);
    }
    committed = true;
    // Model the server committing the appointment while the HTTP response is lost.
    return route.abort('failed');
  }

  if (path === '/booking/v1/operations' && request.method() === 'GET') {
    operationReads += 1;
    if (!committed) return json(route, { detail: 'operation not found' }, 404);
    if (operationReads === 1) {
      return json(route, { detail: 'temporary status lookup failure' }, 503);
    }
    return json(route, {
      operation_id: 'operation-ui-001',
      action: 'CREATE',
      status: 'SUCCEEDED',
      appointment_id: APPOINTMENT_ID,
    });
  }

  return json(route, { detail: `unhandled mock endpoint: ${request.method()} ${path}` }, 404);
});

try {
  await page.goto(`${FRONTEND}/appointment`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: '智能预约' }).waitFor();
  await page.getByLabel('预约需求').fill('预约肩颈舒缓，明天下午三点');
  await page.getByRole('button', { name: '提交需求' }).click();
  await page.locator('[data-slot="card-title"]').filter({ hasText: '请核对预约方案' }).waitFor();
  await page.getByRole('button', { name: '恢复确认凭据' }).waitFor();

  const initialStoredState = await page.evaluate(() => JSON.stringify({
    local: Object.entries(localStorage),
    session: Object.entries(sessionStorage),
  }));
  if (initialStoredState.includes(CONFIRMATION_TOKEN)) {
    throw new Error('confirmation credential leaked into browser storage');
  }
  if (confirmationPosts !== 0) throw new Error('appointment was written before explicit confirmation');

  // A page reload restores the pending task and retrieves a fresh credential,
  // while still waiting for the user to explicitly click Confirm.
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.locator('[data-slot="card-title"]').filter({ hasText: '请核对预约方案' }).waitFor();
  const confirmButton = page.getByRole('button', { name: '确认预约' });
  await confirmButton.waitFor();
  await confirmButton.waitFor({ state: 'visible' });
  if (confirmationPosts !== 0) throw new Error('reload unexpectedly committed an appointment');

  await confirmButton.click();
  await page.getByRole('alert').waitFor();
  if (confirmationPosts !== 1) throw new Error(`expected one confirmation POST, got ${confirmationPosts}`);
  if (!confirmationBody?.idempotency_key) throw new Error('confirmation omitted its idempotency key');

  const pendingAttempt = await page.evaluate(() =>
    Object.entries(sessionStorage).find(([key]) => key.startsWith('appointment:pending-attempt:'))?.[1] ?? null,
  );
  if (!pendingAttempt || !pendingAttempt.includes(confirmationBody.idempotency_key)) {
    throw new Error('pending idempotency key was not persisted after an ambiguous response');
  }
  if (pendingAttempt.includes(CONFIRMATION_TOKEN)) {
    throw new Error('confirmation credential was persisted with the pending attempt');
  }

  // The first status read is injected to fail. A second browser restart must
  // reconcile the same idempotency key with the committed operation and order.
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.locator('[data-slot="card-title"]').filter({ hasText: '预约已确认' }).waitFor();
  await page.getByText(APPOINTMENT_ID, { exact: true }).waitFor();
  await page.getByText('肩颈舒缓', { exact: true }).waitFor();

  const storedStateAfterCompletion = await page.evaluate(() => JSON.stringify({
    local: Object.entries(localStorage),
    session: Object.entries(sessionStorage),
  }));
  if (storedStateAfterCompletion.includes(CONFIRMATION_TOKEN)) {
    throw new Error('confirmation credential leaked into browser storage after completion');
  }
  if (confirmationPosts !== 1) throw new Error(`duplicate confirmation POST detected: ${confirmationPosts}`);
  if (operationReads < 2) throw new Error(`expected status reconciliation across reload, got ${operationReads} reads`);
  if (pageErrors.length) throw new Error(`browser page errors: ${pageErrors.join('; ')}`);

  console.log(JSON.stringify({
    verdict: 'PASS',
    checks: [
      'explicit confirmation required before write',
      'confirmation credential is not persisted in browser storage',
      'pending proposal and credential recover after page reload',
      'idempotency key persists when commit response is lost',
      'committed operation and appointment details recover after another reload',
      'no duplicate confirmation request',
    ],
    confirmationPosts,
    operationReads,
    appointmentId: APPOINTMENT_ID,
    pageErrors,
  }, null, 2));
} finally {
  await browser.close();
}
