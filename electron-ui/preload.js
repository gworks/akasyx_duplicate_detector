// preload.js - renderer に渡す最小限の窓口（contextIsolation 前提）
const { contextBridge, ipcRenderer, webUtils } = require('electron');

const listen = (channel) => (callback) => {
  const handler = (_event, payload) => callback(payload);
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
};

contextBridge.exposeInMainWorld('detector', {
  getContext: () => ipcRenderer.invoke('app:context'),
  // 画面の言語: { lang, dict }（dict は英語の値で穴埋め済みの辞書）
  getLanguage: () => ipcRenderer.invoke('i18n:get'),
  setLanguage: (lang) => ipcRenderer.invoke('i18n:set', lang),
  loadSettings: () => ipcRenderer.invoke('settings:load'),
  saveSettings: (settings) => ipcRenderer.invoke('settings:save', settings),
  pick: (options) => ipcRenderer.invoke('dialog:pick', options),
  preview: (form) => ipcRenderer.invoke('command:preview', form),
  start: (form) => ipcRenderer.invoke('run:start', form),
  // runId: 画面が見ている実行（もう走っていなければ main は何も送らない。#22）
  stop: (runId) => ipcRenderer.invoke('run:stop', runId),
  // 登録済みの保存フォルダの一覧: { masterDb, archives, error }（#11）
  listArchives: (form) => ipcRenderer.invoke('archives:list', form),
  reveal: (target) => ipcRenderer.invoke('shell:reveal', target),
  open: (target) => ipcRenderer.invoke('shell:open', target),
  openDataDir: () => ipcRenderer.invoke('data:open'),
  copy: (text) => ipcRenderer.invoke('clipboard:write', text),
  // detector が出力した実パス → パス欄に入れる値
  toFieldValue: (target) => ipcRenderer.invoke('path:field', target),
  // ドラッグ＆ドロップされた File から実パスを得る（file.path は廃止済み）
  pathForFile: (file) => {
    try {
      return webUtils.getPathForFile(file);
    } catch {
      return '';
    }
  },
  // かんたん取り込み（#22）: ドロップしたもの（実パス）を確認して列に入れる・列の中身・待ちの取り消し
  // （実行中の項目の中断は stop に項目の実行の id を渡す）
  quickSubmit: (request) => ipcRenderer.invoke('quick:submit', request),
  quickList: () => ipcRenderer.invoke('quick:list'),
  quickCancel: (id) => ipcRenderer.invoke('quick:cancel', id),
  quickResume: () => ipcRenderer.invoke('quick:resume'),
  // 列の写し（#36）: quickList は全体、onQuickQueue は差分（通し番号つき。renderer/list-sync.js で当てる）
  onQuickQueue: listen('quick:queue'),
  // 実行ボタンの実行の出力の履歴（#38）: 全体と差分（列と同じ形）
  historyList: () => ipcRenderer.invoke('run:history-list'),
  onHistory: listen('run:history'),
  // 画面が始めていない実行（列の項目）が始まった
  onStarted: listen('run:started'),
  onLog: listen('run:log'),
  onProgress: listen('run:progress'),
  onExit: listen('run:exit'),
});
