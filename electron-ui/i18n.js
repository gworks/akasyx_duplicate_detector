// i18n.js - 画面の多言語対応（ja / en / fr / de / it / et）。辞書は locales/<lang>.json
//
// 辞書はメインプロセスが読み、renderer には IPC で渡す（renderer の CSP は fetch を許していないため）。
// メインプロセス自身の文言（ダイアログ・終了コードの説明）も同じ辞書で訳す。
// 言語の決め方: 前回選んだ言語（settings.json）→ OS の言語 → en。
// 辞書にキーが無ければ英語の値、それも無ければキー名を出す（読み込みに失敗した言語は英語で表示する）。
const fs = require('node:fs');
const path = require('node:path');

const SUPPORTED_LANGS = ['ja', 'en', 'fr', 'de', 'it', 'et'];
const FALLBACK_LANG = 'en';
const LOCALES_DIR = path.join(__dirname, 'locales');

const cache = new Map();

function readDict(lang) {
  if (!cache.has(lang)) {
    try {
      cache.set(lang, JSON.parse(fs.readFileSync(path.join(LOCALES_DIR, `${lang}.json`), 'utf-8')));
    } catch (e) {
      console.error(`Failed to load locale ${lang}:`, e);
      cache.set(lang, {});
    }
  }
  return cache.get(lang);
}

/** 対応言語ならそのまま、`fr-CA` のような地域付きは先頭だけ見て、対応外は null。 */
function normalizeLang(value) {
  if (typeof value !== 'string') return null;
  const base = value.toLowerCase().split(/[-_]/)[0];
  return SUPPORTED_LANGS.includes(base) ? base : null;
}

/** 使う言語を決めます。saved は前回選んだ言語、systemLocale は app.getLocale() の値。 */
function resolveLang(saved, systemLocale) {
  return normalizeLang(saved) || normalizeLang(systemLocale) || FALLBACK_LANG;
}

/** その言語の辞書（英語の値で穴埋めしたもの）。 */
function dictionary(lang) {
  const base = readDict(FALLBACK_LANG);
  return lang === FALLBACK_LANG ? { ...base } : { ...base, ...readDict(lang) };
}

/** `{name}` を vars の値で置き換えます。 */
function format(template, vars = {}) {
  return String(template).replace(/\{(\w+)\}/g, (all, name) => (name in vars ? String(vars[name]) : all));
}

function translator(lang) {
  const dict = dictionary(lang);
  return (key, vars) => format(key in dict ? dict[key] : key, vars);
}

module.exports = { SUPPORTED_LANGS, FALLBACK_LANG, LOCALES_DIR, normalizeLang, resolveLang, dictionary, format, translator };
