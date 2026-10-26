// "Add to Stash" toolbar button and context menus. STASH_ORIGIN comes from config.js,
// which the server writes into the zip (Chrome loads it here, Firefox via manifest "scripts").
if (typeof importScripts === 'function') importScripts('config.js');
const ext = globalThis.browser ?? globalThis.chrome;

function openAdd(suffix) {
  ext.windows.create({ url: `${STASH_ORIGIN}/add${suffix}`, type: 'popup', width: 560, height: 760 });
}

// everything goes in the URL fragment, which is not sent to the server
const single = (url, title) => openAdd(`#${new URLSearchParams({ url: url || '', title: title || '' })}`);

// a tab list is passed as base64url JSON
function packed(list) {
  let binary = '';
  for (const byte of new TextEncoder().encode(JSON.stringify(list))) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

const webTabs = (tabs) => tabs.filter((t) => /^https?:/.test(t.url || '')).map((t) => ({ url: t.url, title: t.title || '' }));

ext.action.onClicked.addListener((tab) => single(tab.url, tab.title));

ext.runtime.onInstalled.addListener(() => {
  ext.contextMenus.create({ id: 'page', title: ext.i18n.getMessage('menuPage'), contexts: ['page'] });
  ext.contextMenus.create({ id: 'link', title: ext.i18n.getMessage('menuLink'), contexts: ['link'] });
  ext.contextMenus.create({ id: 'all', title: ext.i18n.getMessage('menuAll'), contexts: ['action'] });
  ext.contextMenus.create({ id: 'send', title: ext.i18n.getMessage('menuSend'), contexts: ['action'] });
});

ext.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === 'link') return single(info.linkUrl, info.linkText || info.selectionText);
  if (info.menuItemId === 'page') return single(tab.url, tab.title);
  if (info.menuItemId === 'all') return openAdd(`#batch=${packed(webTabs(await ext.tabs.query({ currentWindow: true })))}`);
  // every window: the tabs go to Open tabs in Stash, to be sorted out later, so the browser can be emptied
  return openAdd(`#tabs=${packed(webTabs(await ext.tabs.query({})))}`);
});
