// ==UserScript==
// @name         PikPak credentials for Python
// @namespace    pikpak-credentials
// @version      1.0.0
// @description  Copy one recent official request's credentials as Python .env text.
// @match        https://mypikpak.com/*
// @run-at       document-start
// @grant        unsafeWindow
// @grant        GM_setClipboard
// ==/UserScript==

(() => {
  'use strict';

  const page = unsafeWindow;
  const fields = [
    ['authorization', 'PIKPAK_AUTHORIZATION'],
    ['x-device-id', 'PIKPAK_DEVICE_ID'],
    ['x-captcha-token', 'PIKPAK_CAPTCHA_TOKEN'],
  ];
  let credentials = null;
  let busy = false;
  let button;
  let status;
  let message = '请先登录并操作 PikPak，等待包含完整凭据的请求。';

  function render() {
    if (button) button.disabled = busy || !credentials;
    if (status) status.textContent = message;
  }

  function official(url) {
    try {
      const parsed = new page.URL(url, page.location.href);
      return parsed.protocol === 'https:' && parsed.hostname === 'api-drive.mypikpak.com'
        && !parsed.username && !parsed.password && (!parsed.port || parsed.port === '443');
    } catch {
      return false;
    }
  }

  function capture(readHeader) {
    const values = fields.map(([header]) => readHeader(header));
    const missing = fields.filter((_, i) => !values[i]).map(([, key]) => key);
    credentials = null;
    if (missing.length) {
      message = `缺少请求头：${missing.join(', ')}。请登录并操作 PikPak 后重试。`;
    } else if (!/^Bearer\s+\S+/i.test(values[0]) || values.some(value => /[\r\n\0]/.test(value))) {
      message = '凭据格式无效，请等待新的登录请求。';
    } else {
      credentials = values;
      message = '凭据已就绪。点击复制后，粘贴到 Python 文件夹的 .env。';
    }
    render();
  }

  const originalFetch = page.fetch;
  page.fetch = function (...args) {
    const result = Reflect.apply(originalFetch, this, args);
    // Observation must never prevent or change the original request.
    try {
      const [input, init] = args;
      const request = input instanceof page.Request;
      const url = request ? input.url : typeof input === 'string' ? input
        : input instanceof page.URL ? input.href : null;
      if (url !== null && official(url)) {
        const headers = new page.Headers(init?.headers === undefined
          ? request ? input.headers : undefined : init.headers);
        capture(name => headers.get(name));
      }
    } catch {
      // Unusual request objects still retain native fetch behavior.
    }
    return result;
  };

  const xhrState = new WeakMap();
  const prototype = page.XMLHttpRequest.prototype;
  const originalOpen = prototype.open;
  const originalSetHeader = prototype.setRequestHeader;
  const originalSend = prototype.send;
  prototype.open = function (...args) {
    const result = Reflect.apply(originalOpen, this, args);
    xhrState.set(this, official(args[1]) ? new Map() : null);
    return result;
  };
  prototype.setRequestHeader = function (...args) {
    const result = Reflect.apply(originalSetHeader, this, args);
    const headers = xhrState.get(this);
    if (headers && typeof args[0] === 'string' && typeof args[1] === 'string') {
      const name = args[0].toLowerCase();
      if (fields.some(([header]) => header === name)) {
        const value = args[1].trim();
        headers.set(name, headers.has(name) ? `${headers.get(name)}, ${value}` : value);
      }
    }
    return result;
  };
  prototype.send = function (...args) {
    try {
      const result = Reflect.apply(originalSend, this, args);
      try {
        const headers = xhrState.get(this);
        if (headers) capture(name => headers.get(name));
      } catch {
        // Credential observation is optional; sending is not.
      }
      return result;
    } finally {
      xhrState.delete(this);
    }
  };

  function mount() {
    const panel = document.createElement('section');
    panel.setAttribute('aria-label', 'PikPak Python 凭据');
    panel.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;max-width:320px;padding:12px;background:#fff;color:#111;border:1px solid #777;border-radius:8px;font:14px/1.5 sans-serif;box-shadow:0 2px 8px #0003';
    button = document.createElement('button');
    button.type = 'button';
    button.textContent = '复制 PikPak .env 凭据';
    button.style.cssText = 'padding:8px;cursor:pointer;font:inherit';
    status = document.createElement('p');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    button.addEventListener('click', async () => {
      if (busy) return;
      if (!credentials) {
        message = '尚未获取完整凭据，请先登录并操作 PikPak。';
        render();
        return;
      }
      const snapshot = credentials.slice();
      busy = true;
      message = '正在获取本次复制所需的写入凭据，请稍候。';
      render();
      try {
        const captcha = page.loginUI?.login?.captcha;
        const manager = captcha?._captchaTokenInfoManager;
        if (!manager || manager._originDeviceId !== snapshot[1]) throw new Error();
        const info = await manager._localCaptchaTokenInfoManager.getCaptchaTokenInfo();
        if (!info?.meta || !['captcha_sign', 'client_version', 'package_name', 'user_id', 'timestamp']
          .every(key => info.meta[key] !== undefined && info.meta[key] !== null && info.meta[key] !== '')) {
          throw new Error();
        }
        snapshot[2] = await captcha.getCaptchaToken('POST:/drive/v1/files', info.meta, true);
        if (typeof snapshot[2] !== 'string' || !snapshot[2] || /[\r\n\0]/.test(snapshot[2])
          || !credentials || credentials[0] !== snapshot[0] || credentials[1] !== snapshot[1]
          || manager._originDeviceId !== snapshot[1]) throw new Error();
        const text = fields.map(([, key], i) =>
          `${key}="${snapshot[i].replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`).join('\n') + '\n';
        GM_setClipboard(text, 'text', () => {
          message = '已复制。请粘贴到 Python 文件夹的 .env，勿分享凭据。';
          render();
        });
      } catch {
        message = '未复制：无法获取写入凭据或登录状态已改变。请刷新并登录 PikPak，操作后重试，并检查剪贴板权限。';
      } finally {
        busy = false;
        render();
      }
    });
    panel.append(button, status);
    document.body.append(panel);
    render();
  }

  if (document.body) mount();
  else document.addEventListener('DOMContentLoaded', mount, { once: true });
})();
