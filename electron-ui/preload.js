// preload.js - renderer に渡す最小限の窓口（contextIsolation 前提）
const { contextBridge, ipcRenderer, webUtils } = require('electron');

const listen = (channel) => (callback) => {
  const handler = (_event, payload) => callback(payload);
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
};

contextBridge.exposeInMainWorld('detector', {
  getContext: () => ipcRenderer.invoke('app:context'),
  loadSettings: () => ipcRenderer.invoke('settings:load'),
  saveSettings: (settings) => ipcRenderer.invoke('settings:save', settings),
  pick: (options) => ipcRenderer.invoke('dialog:pick', options),
  preview: (form) => ipcRenderer.invoke('command:preview', form),
  start: (form) => ipcRenderer.invoke('run:start', form),
  stop: () => ipcRenderer.invoke('run:stop'),
  reveal: (target) => ipcRenderer.invoke('shell:reveal', target),
  open: (target) => ipcRenderer.invoke('shell:open', target),
  copy: (text) => ipcRenderer.invoke('clipboard:write', text),
  // ドラッグ＆ドロップされた File から実パスを得る（file.path は廃止済み）
  pathForFile: (file) => {
    try {
      return webUtils.getPathForFile(file);
    } catch {
      return '';
    }
  },
  onLog: listen('run:log'),
  onProgress: listen('run:progress'),
  onExit: listen('run:exit'),
});
