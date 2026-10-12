// stop-file.test.js - 停止ファイルで中断を頼む（#32）。UI と detector で環境変数の名前が同じか（node --test）
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

test('停止ファイルの環境変数の名前は、main.js と detector/stop_watch.py で同じ', () => {
  const main = fs.readFileSync(path.join(__dirname, 'main.js'), 'utf-8');
  const watch = fs.readFileSync(path.join(__dirname, '..', 'detector', 'stop_watch.py'), 'utf-8');
  const ui = main.match(/const STOP_FILE_ENV = '([^']+)'/);
  const detector = watch.match(/STOP_FILE_ENV = "([^"]+)"/);
  assert.ok(ui && detector, '定義が見つからない');
  assert.strictEqual(ui[1], detector[1]);
});
