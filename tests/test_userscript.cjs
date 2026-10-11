'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../userscripts/pikpak-credentials.user.js'), 'utf8');
const endpoint = 'https://api-drive.mypikpak.com/drive/v1/files';
const headers = {
  Authorization: 'Bearer sample-auth',
  'X-Device-Id': 'sample-device',
  'X-Captcha-Token': 'sample-captcha',
};
const dotenv = 'PIKPAK_AUTHORIZATION="Bearer sample-auth"\nPIKPAK_DEVICE_ID="sample-device"\nPIKPAK_CAPTCHA_TOKEN="sample-captcha"\n';

function harness({ delayedMount = false } = {}) {
  const elements = [];
  const copied = [];
  const requests = [];
  let ready;
  const promise = Promise.resolve({ ok: true });
  class Element {
    constructor(tag) {
      this.tag = tag;
      this.style = {};
      this.attributes = {};
      this.textContent = '';
      this.children = [];
      elements.push(this);
    }
    setAttribute(key, value) { this.attributes[key] = value; }
    append(...children) { this.children.push(...children); }
    addEventListener(name, callback) { this[name] = callback; }
  }
  class XHR {
    open(...args) { this.openArgs = args; this.sentHeaders = []; }
    setRequestHeader(...args) { this.sentHeaders.push(args); }
    send(...args) { this.sendArgs = args; return 'sent'; }
  }
  const body = new Element('body');
  const page = {
    Headers, Request, URL,
    location: { href: 'https://mypikpak.com/drive' },
    XMLHttpRequest: XHR,
    loginUI: { login: { captcha: {
      _captchaTokenInfoManager: {
        _originDeviceId: 'sample-device',
        _localCaptchaTokenInfoManager: {
          async getCaptchaTokenInfo() {
            return { meta: { captcha_sign: 'sign', client_version: '1', package_name: 'web', user_id: 'user', timestamp: '1' } };
          },
        },
      },
      async getCaptchaToken() { return 'sample-captcha'; },
    } } },
    fetch(...args) { requests.push({ receiver: this, args }); return promise; },
  };
  const document = {
    body: delayedMount ? null : body,
    createElement: tag => new Element(tag),
    addEventListener(name, callback) {
      assert.equal(name, 'DOMContentLoaded');
      ready = callback;
    },
  };
  vm.runInNewContext(source, {
    unsafeWindow: page, document,
    GM_setClipboard(text, type, done) { copied.push({ text, type }); done(); },
  });
  return {
    page, copied, requests, elements, promise,
    get button() { return elements.find(element => element.tag === 'button'); },
    get status() { return elements.find(element => element.attributes.role === 'status'); },
    mount() { document.body = body; ready(); },
  };
}

test('fetch capture uses effective Request headers, preserves body/return/receiver, copies only on click', async () => {
  const h = harness({ delayedMount: true });
  const request = new Request(endpoint, { method: 'POST', headers: { Authorization: 'Bearer old' }, body: 'payload' });
  const init = { headers };
  const receiver = {};
  assert.equal(h.page.fetch.call(receiver, request, init), h.promise);
  assert.equal(h.requests[0].receiver, receiver);
  assert.equal(h.requests[0].args[0], request);
  assert.equal(h.requests[0].args[1], init);
  assert.equal(request.bodyUsed, false);
  assert.equal(await request.text(), 'payload');
  assert.equal(h.copied.length, 0);
  h.mount();
  assert.equal(h.button.disabled, false);
  await h.button.click();
  assert.deepEqual(h.copied, [{ text: dotenv, type: 'text' }]);
  assert.equal(h.status.attributes['aria-live'], 'polite');
  const visible = h.elements.map(element => element.textContent).join(' ');
  for (const secret of Object.values(headers)) assert.equal(visible.includes(secret), false);
});

test('XHR preserves open/header/send arguments and uses the headers of that send', async () => {
  const h = harness();
  const xhr = new h.page.XMLHttpRequest();
  const openArgs = ['POST', endpoint, false];
  xhr.open(...openArgs);
  for (const pair of Object.entries(headers)) xhr.setRequestHeader(...pair);
  xhr.setRequestHeader('Content-Type', 'text/plain');
  const body = { payload: true };
  assert.equal(xhr.send(body), 'sent');
  assert.deepEqual(xhr.openArgs, openArgs);
  assert.deepEqual(xhr.sentHeaders, [...Object.entries(headers), ['Content-Type', 'text/plain']]);
  assert.equal(xhr.sendArgs[0], body);
  await h.button.click();
  assert.equal(h.copied[0].text, dotenv);
  xhr.open('GET', endpoint);
  xhr.send();
  assert.equal(h.button.disabled, true);
  assert.match(h.status.textContent, /缺少请求头：PIKPAK_AUTHORIZATION/);
});

test('external hosts, HTTP, userinfo and non-HTTPS ports cannot populate either interceptor', async () => {
  for (const url of [
    'https://example.com/drive/v1/files',
    'https://api-drive.mypikpak.com.evil.example/',
    'http://api-drive.mypikpak.com/',
    'https://user@api-drive.mypikpak.com/',
    'https://api-drive.mypikpak.com:444/',
  ]) {
    const h = harness();
    h.page.fetch(url, { headers });
    const xhr = new h.page.XMLHttpRequest();
    xhr.open('GET', url);
    for (const pair of Object.entries(headers)) xhr.setRequestHeader(...pair);
    xhr.send();
    assert.equal(h.requests.length, 1);
    assert.equal(h.button.disabled, true, url);
    await h.button.click();
    assert.equal(h.copied.length, 0, url);
  }
});

test('partial requests never combine credentials; a complete Request can be captured without init', async () => {
  const h = harness();
  h.page.fetch(endpoint, { headers: { Authorization: headers.Authorization } });
  h.page.fetch(endpoint, { headers: { 'X-Device-Id': headers['X-Device-Id'], 'X-Captcha-Token': headers['X-Captcha-Token'] } });
  assert.equal(h.button.disabled, true);
  assert.match(h.status.textContent, /缺少请求头：PIKPAK_AUTHORIZATION/);
  h.page.fetch(new Request(endpoint, { headers }));
  await h.button.click();
  assert.equal(h.copied[0].text, dotenv);
});

test('dotenv quoting preserves literal quotes/backslashes, while malformed auth is not copied', async () => {
  const h = harness();
  h.page.loginUI.login.captcha._captchaTokenInfoManager._originDeviceId = 'device"\\quoted';
  h.page.fetch(endpoint, { headers: { ...headers, 'X-Device-Id': 'device"\\quoted' } });
  await h.button.click();
  assert.match(h.copied[0].text, /PIKPAK_DEVICE_ID="device\\"\\\\quoted"\n/);
  h.page.fetch(endpoint, { headers: { ...headers, Authorization: 'sample-auth' } });
  assert.equal(h.button.disabled, true);
  assert.match(h.status.textContent, /凭据格式无效/);
});

test('copy requests a fresh write CAPTCHA once per user click, never the observed read token', async () => {
  const h = harness();
  const captcha = h.page.loginUI.login.captcha;
  const meta = { captcha_sign: 'sign', client_version: '1', package_name: 'web', user_id: 'user', timestamp: '1' };
  let infoCalls = 0;
  let tokenCalls = 0;
  let resolveToken;
  captcha._captchaTokenInfoManager._localCaptchaTokenInfoManager.getCaptchaTokenInfo = async () => {
    infoCalls++;
    return { meta };
  };
  captcha.getCaptchaToken = (action, suppliedMeta, force) => {
    tokenCalls++;
    assert.equal(action, 'POST:/drive/v1/files');
    assert.deepEqual(suppliedMeta, meta);
    assert.equal(force, true);
    return new Promise(resolve => { resolveToken = resolve; });
  };
  h.page.fetch(endpoint, { headers: { ...headers, 'X-Captcha-Token': 'read-only-token' } });
  assert.equal(infoCalls, 0);
  const pending = h.button.click();
  assert.equal(h.button.disabled, true);
  await Promise.resolve();
  assert.equal(infoCalls, 1);
  await h.button.click();
  assert.equal(tokenCalls, 1, 'a busy button cannot start another credential request');
  assert.equal(h.copied.length, 0);
  resolveToken('fresh-write-token');
  await pending;
  assert.equal(h.button.disabled, false);
  assert.equal(h.copied[0].text, 'PIKPAK_AUTHORIZATION="Bearer sample-auth"\nPIKPAK_DEVICE_ID="sample-device"\nPIKPAK_CAPTCHA_TOKEN="fresh-write-token"\n');
});

test('SDK failure, missing metadata, device mismatch or a changing login copies nothing', async () => {
  for (const scenario of ['missing SDK', 'missing metadata', 'throwing SDK', 'wrong device', 'changed auth', 'changed device']) {
    const h = harness();
    const captcha = h.page.loginUI.login.captcha;
    if (scenario === 'missing SDK') delete h.page.loginUI;
    if (scenario === 'missing metadata') {
      captcha._captchaTokenInfoManager._localCaptchaTokenInfoManager.getCaptchaTokenInfo = async () => ({});
    }
    if (scenario === 'throwing SDK') captcha.getCaptchaToken = async () => { throw new Error('SDK failed'); };
    if (scenario === 'wrong device') captcha._captchaTokenInfoManager._originDeviceId = 'another-device';
    if (scenario === 'changed auth' || scenario === 'changed device') {
      captcha.getCaptchaToken = async () => {
        h.page.fetch(endpoint, { headers: { ...headers,
          ...(scenario === 'changed auth' ? { Authorization: 'Bearer another-account' } : { 'X-Device-Id': 'another-device' }),
        } });
        return 'fresh-write-token';
      };
    }
    h.page.fetch(endpoint, { headers });
    await h.button.click();
    assert.equal(h.copied.length, 0, scenario);
    assert.match(h.status.textContent, /未复制.*请刷新并登录 PikPak/, scenario);
    assert.equal(h.button.disabled, false, scenario);
  }
});

test('observation errors leave the original fetch return and arguments intact', () => {
  const h = harness();
  const init = { get headers() { throw new Error('unreadable headers'); } };
  assert.equal(h.page.fetch(endpoint, init), h.promise);
  assert.equal(h.requests[0].args[1], init);
  assert.equal(h.copied.length, 0);
});
