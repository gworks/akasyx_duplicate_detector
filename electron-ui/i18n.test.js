// i18n.test.js - 辞書（locales/*.json）の整合と、言語の決め方（node --test）
//
// youtube_downloder で de.json / et.json が JSON として壊れたまま出荷されていた（„…" の閉じが ASCII の "）。
// 読めない辞書・キーの抜け・差し込み名の食い違いをここで止める。
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const i18n = require('./i18n');
const { buildArgs, FormError } = require('./commands');

const load = (lang) => JSON.parse(fs.readFileSync(path.join(i18n.LOCALES_DIR, `${lang}.json`), 'utf-8'));
const vars = (s) => [...String(s).matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
const html = fs.readFileSync(path.join(__dirname, 'renderer', 'index.html'), 'utf-8');

test('locales/ には対応言語の辞書だけがあり、すべて JSON として読める', () => {
  const files = fs.readdirSync(i18n.LOCALES_DIR).filter((f) => f.endsWith('.json')).sort();
  assert.deepStrictEqual(files, i18n.SUPPORTED_LANGS.map((l) => `${l}.json`).sort());
  for (const lang of i18n.SUPPORTED_LANGS) load(lang); // 壊れていればここで失敗する
});

test('全言語が en と同じキー・同じ差し込み名を持ち、空の値が無い', () => {
  const en = load('en');
  for (const lang of i18n.SUPPORTED_LANGS) {
    const d = load(lang);
    assert.deepStrictEqual(Object.keys(d).sort(), Object.keys(en).sort(), `${lang}.json のキー`);
    for (const key of Object.keys(en)) {
      assert.ok(String(d[key]).trim(), `${lang}.json: ${key} が空`);
      assert.deepStrictEqual(vars(d[key]), vars(en[key]), `${lang}.json: ${key} の差し込み名`);
    }
  }
});

test('HTML を含む値は *_html キーだけ（innerHTML で入れるのはそれだけ）', () => {
  for (const lang of i18n.SUPPORTED_LANGS) {
    for (const [key, value] of Object.entries(load(lang))) {
      if (!key.endsWith('_html')) assert.ok(!/<\/?(code|strong|em|br|b|i|a|span)\b/i.test(value), `${lang}.json: ${key} にタグがある`);
    }
  }
});

test('index.html が参照するキーはすべて辞書にある', () => {
  const en = load('en');
  const refs = [...html.matchAll(/data-(?:i18n(?:-html|-placeholder|-aria-label)?|pick-title)="([^"]+)"/g)].map((m) => m[1]);
  assert.ok(refs.length > 50);
  for (const key of refs) assert.ok(key in en, `index.html の ${key} が辞書に無い`);
  for (const m of html.matchAll(/data-i18n-html="([^"]+)"/g)) assert.ok(m[1].endsWith('_html'), m[1]);
});

test('入力エラー（FormError）のキーはすべて辞書にある', () => {
  const en = load('en');
  const bad = [
    { mode: 'nope' },
    { mode: 'add' },
    { mode: 'add', archiveRoot: '/a' },
    { mode: 'add', archiveRoot: '/a', sourcePath: '/b', folderLimit: '0' },
    { mode: 'add', archiveRoot: '/a', sourcePath: '/b', minSize: '-1' },
    { mode: 'report', archiveRoot: '/a', ingestId: 'x' },
  ];
  for (const form of bad) {
    assert.throws(() => buildArgs(form), (e) => e instanceof FormError && e.key in en, JSON.stringify(form));
  }
});

test('言語の決め方: 前回選んだ言語 → OS の言語（地域付きも可） → en', () => {
  assert.strictEqual(i18n.resolveLang('de', 'ja-JP'), 'de');
  assert.strictEqual(i18n.resolveLang(undefined, 'fr-CA'), 'fr');
  assert.strictEqual(i18n.resolveLang('xx', 'pt-BR'), 'en');
  assert.strictEqual(i18n.resolveLang(null, 'et'), 'et');
});

test('翻訳: 差し込み・英語での穴埋め・未知のキー', () => {
  const tr = i18n.translator('ja');
  assert.strictEqual(tr('exit_code', { code: 3 }), '終了コード 3');
  assert.strictEqual(tr('no_such_key'), 'no_such_key');
  assert.strictEqual(i18n.format('{a} {b}', { a: 1 }), '1 {b}');
});
