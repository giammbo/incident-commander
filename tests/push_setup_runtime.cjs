// Execute the real template script against browser boundaries. Provider/network calls are fake.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('app/templates/account_notifications.html', 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  .replace(/{{\s*vapid_public_key\s*\|\s*tojson\s*}}/, '"dGVzdA"');

async function scenario({ permission = 'granted', registrationError, saveOK = true, existing = false, installing = false, redirected = false } = {}) {
  let click, registered, saved, subscribed = 0;
  const status = { textContent: '0 devices subscribed.' };
  const button = { disabled: false, addEventListener: (name, handler) => { click = handler; } };
  const sub = { toJSON: () => ({ endpoint: 'https://push.example/device', keys: { p256dh: 'p', auth: 'a' } }) };
  const worker = { state: 'installing', addEventListener: (name, fn) => { setTimeout(() => { worker.state = 'activated'; fn(); }, 5); }, removeEventListener: () => {} };
  const reg = {
    active: installing ? null : {}, installing: installing ? worker : null,
    pushManager: {
      getSubscription: async () => existing ? sub : null,
      subscribe: async () => { subscribed++; return sub; },
    },
  };
  const Notification = { requestPermission: async () => permission };
  const context = {
    document: { getElementById: id => id === 'push-enable' ? button : status },
    navigator: { serviceWorker: {
      // This deliberately never resolves: the broken /static/ implementation used it.
      ready: new Promise(() => {}),
      register: async (url, options) => { registered = { url, options }; if (registrationError) throw Error(registrationError); return reg; },
    } },
    window: { PushManager: {}, Notification }, Notification,
    fetch: async (url, options) => { saved = { url, options }; return { ok: saveOK, redirected, json: async () => ({ ok: saveOK }) }; },
    Uint8Array, atob, setTimeout, clearTimeout, Error, Promise,
  };
  vm.runInNewContext(source, context);
  await Promise.race([click(), new Promise((_, reject) => setTimeout(() => reject(Error('Push activation never completed')), 1000))]);
  return { status: status.textContent, button, registered, saved, subscribed };
}

(async () => {
  const success = await scenario();
  assert.equal(success.registered.url, '/oncall-sw.js');
  assert.equal(success.registered.options.scope, '/');
  assert.equal(success.saved.url, '/account/notifications/push');
  assert.match(success.status, /subscribed/i);
  assert.equal(success.button.disabled, false);
  const activation = await scenario({ installing: true });
  assert.match(activation.status, /subscribed/i);
  const denied = await scenario({ permission: 'denied' });
  assert.match(denied.status, /denied|blocked/i);
  assert.equal(denied.saved, undefined);
  assert.equal(denied.button.disabled, false);
  const failed = await scenario({ registrationError: 'worker unavailable' });
  assert.match(failed.status, /worker unavailable/);
  assert.equal(failed.button.disabled, false);
  const notSaved = await scenario({ saveOK: false });
  assert.match(notSaved.status, /could not save|failed/i);
  const reused = await scenario({ existing: true });
  assert.equal(reused.subscribed, 0);
  assert.ok(reused.saved);
  const expiredSession = await scenario({ redirected: true });
  assert.match(expiredSession.status, /sign in|session|could not save|failed/i);
  console.log('Push setup runtime: success, activation, denial, registration failure, save failure, existing subscription passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
