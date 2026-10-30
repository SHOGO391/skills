#!/usr/bin/env python3
"""ui_copy_lint.py — 画面に出る文字列を集めて、ui-copy の規則の候補を行番号つきで出す。

判定はしない。人と AI が「操作の文言／商品の中身／注意／内部の漏れ」に分類する材料。
T10（内部の語）だけは当たれば終了コード 1（commit 前のフック・CI 向け）。

使い方:
  python ui_copy_lint.py web/index.html src/App.tsx
  python ui_copy_lint.py --terms .ui-copy-terms.txt --keep .ui-copy-keep.txt --json out.json web/*.html
  python ui_copy_lint.py --preset myapp web/index.html      # scripts/terms/private/myapp.txt（git の対象外）を足す
  python ui_copy_lint.py --only T10 web/index.html           # 内部の語だけ
  python ui_copy_lint.py --lang en src/**/*.tsx              # 英語の文字列だけ

対象: .html .htm .vue .svelte（HTML として読む） / .js .mjs .ts .tsx .jsx（文字列リテラルと JSX の本文）
集めるもの: 本文テキスト、title/placeholder/aria-label/alt/value 属性、日本語か英語の文らしい文字列リテラル
集めないもの: style、HTML コメント、// と /* */ のコメント、console.* の行、コードらしい文字列（記号だらけ・パス・クラス名）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from html.parser import HTMLParser

JP = re.compile(r"[ぁ-んァ-ヶ一-龥ー]")
EN_WORD = re.compile(r"[A-Za-z]{3,}")
CODE_LIKE = re.compile(r"[{}<>=;_\\]|https?://|^[./#]|^[a-z0-9-]+( [a-z0-9-]+)*$|\$\{")
HERE = os.path.dirname(os.path.abspath(__file__))
TERMS_DIR = os.path.join(HERE, "terms")

# ---------------------------------------------------------------- 収集


@dataclass
class S:
    file: str
    line: int
    kind: str   # text / attr:title / js / jsx
    text: str


def looks_like_copy(t: str, lang: str, kind: str) -> bool:
    """画面に出る文らしいか。日本語は 1 文字でも拾う。英語は語が 2 つ以上でコードらしくないもの。"""
    if lang in ("ja", "auto") and JP.search(t):
        return True
    if lang in ("en", "auto"):
        words = EN_WORD.findall(t)
        if kind.startswith("attr") and len(words) >= 2:
            return True
        if len(words) >= 2 and not CODE_LIKE.search(t) and (len(words) >= 3 or t[:1].isupper()):
            return True
    return False


class _P(HTMLParser):
    def __init__(self, file: str, lang: str):
        super().__init__(convert_charrefs=True)
        self.file = file
        self.lang = lang
        self.out: list[S] = []
        self.skip = 0
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        line = self.getpos()[0]
        if tag == "style":
            self.skip += 1
        if tag == "script":
            self.in_script = True
        for k, v in attrs:
            if k in ("title", "placeholder", "aria-label", "alt", "value", "data-tip", "data-tooltip") and v:
                v = v.strip()
                if looks_like_copy(v, self.lang, f"attr:{k}"):
                    self.out.append(S(self.file, line, f"attr:{k}", v))

    def handle_endtag(self, tag):
        if tag == "style" and self.skip:
            self.skip -= 1
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        line = self.getpos()[0]
        if self.in_script:
            self.out.extend(js_strings(self.file, data, line, self.lang))
            return
        if self.skip:
            return
        for i, raw in enumerate(data.split("\n")):
            t = re.sub(r"\s+", " ", raw).strip()
            if t and looks_like_copy(t, self.lang, "text"):
                self.out.append(S(self.file, line + i, "text", t))


_JS_STR = re.compile(r"""'((?:\\.|[^'\\\n])*)'|"((?:\\.|[^"\\\n])*)"|`((?:\\.|[^`\\])*)`""", re.S)
_JSX_TEXT = re.compile(r">([^<>{}\n]{3,})<")
_LINE_COMMENT = re.compile(r"^\s*//")
_BLOCK_OPEN = re.compile(r"/\*")
_BLOCK_CLOSE = re.compile(r"\*/")


def strip_comments(src: str) -> str:
    cleaned = []
    in_block = False
    for ln in src.split("\n"):
        if in_block:
            m = _BLOCK_CLOSE.search(ln)
            if m:
                ln = ln[m.end():]
                in_block = False
            else:
                cleaned.append("")
                continue
        if _LINE_COMMENT.match(ln) or "console." in ln:
            cleaned.append("")
            continue
        m = _BLOCK_OPEN.search(ln)
        if m:
            m2 = _BLOCK_CLOSE.search(ln, m.end())
            if m2:
                ln = ln[: m.start()] + ln[m2.end():]
            else:
                ln = ln[: m.start()]
                in_block = True
        if "//" in ln and "http" not in ln:
            ln = re.sub(r"(?<!:)//(?!/).*$", "", ln)
        cleaned.append(ln)
    return "\n".join(cleaned)


def js_strings(file: str, src: str, base_line: int, lang: str, jsx: bool = False) -> list[S]:
    """JS/TS ソースから文らしい文字列リテラル（と JSX の本文）を拾う。"""
    out: list[S] = []
    text = strip_comments(src)
    for m in _JS_STR.finditer(text):
        s = next(g for g in m.groups() if g is not None)
        line = base_line + text.count("\n", 0, m.start())
        for piece in re.split(r"<[^>]+>|\\n", s):
            piece = re.sub(r"\$\{[^}]*\}", "〔〕", piece)
            piece = re.sub(r"\s+", " ", piece).strip()
            if piece and looks_like_copy(piece, lang, "js"):
                out.append(S(file, line, "js", piece))
    if jsx:
        for m in _JSX_TEXT.finditer(text):
            piece = re.sub(r"\s+", " ", m.group(1)).strip()
            if piece and looks_like_copy(piece, lang, "jsx"):
                out.append(S(file, base_line + text.count("\n", 0, m.start()), "jsx", piece))
    return out


BUNDLE_NAME = re.compile(r"(\.min\.(js|css)$|[-.][A-Za-z0-9_]{8}\.js$)")
SKIP_DIRS = {"node_modules", "dist", "build", ".next", "vendor", "__pycache__"}


def is_bundle(path: str, src: str) -> bool:
    """ビルド済み・依存物の束は利用者向けの文言の置き場ではないので飛ばす。"""
    parts = set(os.path.normpath(path).replace("\\", "/").split("/"))
    if parts & SKIP_DIRS or BUNDLE_NAME.search(os.path.basename(path)):
        return True
    head = src[:4000].split("\n")
    return any(len(ln) > 2000 for ln in head)


def collect(path: str, lang: str) -> list[S]:
    src = open(path, encoding="utf-8", errors="replace").read()
    if is_bundle(path, src):
        print(f"skip (bundle): {path}", file=sys.stderr)
        return []
    ext = os.path.splitext(path)[1].lower()
    if ext in (".html", ".htm", ".vue", ".svelte"):
        p = _P(path, lang)
        p.feed(src)
        return p.out
    return js_strings(path, src, 1, lang, jsx=ext in (".jsx", ".tsx"))


# ---------------------------------------------------------------- 規則

RX = {
    # 日本語
    "ja": {
        "T1": re.compile(r"(ページです|画面です|を表示します|を表示する|できます。|が可能です|となります|しています。|します。$)"),
        "T4": re.compile(r"(保存され|保存します|読み込み(?!中)|読み直|同期|キャッシュ|ログに|記録され|バックグラウンド|自動で更新|再学習|計算し|集計し|取得し)"),
        "T5": re.compile(r"(いつでも.{0,6}(戻|解除|変更)|再度.{0,4}(押|クリック|タップ)|もう一度)"),
        "T7": re.compile(r"(ありません|まだ.{0,8}ない|^なし$|^未設定$|データなし|該当なし)"),
        "T8": re.compile(r"(してください|して下さい|ご確認ください|ご利用ください|ご覧ください|を参照|から入力|から選択|で確認でき|を押して|をクリック|をタップ)"),
        "T13": re.compile(r"(ご利用(?!ガイド)|ご確認|ご覧|ぜひ|お気軽に|しっかり|簡単に|便利に)"),
    },
    # 英語
    "en": {
        "T1": re.compile(r"\b(This (page|screen|section|feature|tool|view)|allows you to|lets you|enables you to|is used to|Here you can|You can use this|gives you)\b", re.I),
        "T4": re.compile(r"\b(saved (only )?to|stored (in|on|locally)|synced|syncs? (to|with|across)|logged|in the background|cached|processed automatically|updated automatically)\b", re.I),
        "T5": re.compile(r"\b(turn it back|you can (always|undo|re-enable|re-?activate)|at any time|anytime)\b", re.I),
        "T7": re.compile(r"\b(No \w+( \w+)? (yet|found|available)|There are no|Nothing (here|to show)|not (available|supported) (yet|on)|isn't available)\b", re.I),
        "T8": re.compile(r"\b(Please |Click here|Go to |Navigate to|Head to|Enter your|Select .{1,30} from|Use the .{1,30} to|To (enable|turn on|activate|get started|change)|you (need|have) to|make sure to)", re.I),
        "T13": re.compile(r"\b(simply|easily|just |feel free|seamlessly|powerful|effortless(ly)?|hassle-free)\b", re.I),
    },
}
SEPARATORS = re.compile(r"( / |／|＝| = |→|->|｜| \| |：| : )")
PAREN_SUPPL_JA = re.compile(r"[（(](?![〔〕\d.,:/ -]*[)）])[^（()）]{4,}[)）]")
PAREN_SUPPL_EN = re.compile(r"\((?![\d.,:/ -]*\))[^()]{10,}\)")

RULES = {
    "T1": "自己説明（〜ページです・This page allows you to）",
    "T4": "裏側の動き（保存・同期・再学習・stored・synced）",
    "T5": "戻し方の説明",
    "T7": "無いものの説明（ありません・No items yet）",
    "T8": "文で案内（〜してください・Please・Go to）",
    "T9": "記号で区切った文（／ ＝ ： → |）",
    "T10": "内部の語（ファイル名・サービス名・社内の呼び名）",
    "T11": "括弧書きの補足・長い title",
    "T13": "埋め草（ご利用ください・simply・easily）",
}


def load_terms(paths: list[str]) -> list[str]:
    terms: list[str] = []
    for p in paths:
        if not p or not os.path.exists(p):
            if p:
                print(f"terms file not found: {p}", file=sys.stderr)
            continue
        for ln in open(p, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                terms.append(ln)
    return terms


def term_matcher(term: str):
    """ASCII の語は単語境界つき（大文字を含む語は大小区別）。それ以外は部分一致。"""
    if term.isascii():
        flags = 0 if any(c.isupper() for c in term) else re.I
        lead = r"(?<![A-Za-z0-9])" if term[0].isalnum() else ""
        tail = r"(?![A-Za-z0-9])" if term[-1].isalnum() else ""
        return re.compile(lead + re.escape(term) + tail, flags)
    return re.compile(re.escape(term))


@dataclass
class Hit:
    file: str
    line: int
    rule: str
    label: str
    text: str
    hard: bool


def check(s: S, terms: list[tuple[str, re.Pattern]]) -> list[Hit]:
    hits: list[Hit] = []
    t = s.text
    lang = "ja" if JP.search(t) else "en"
    for term, rx in terms:
        if rx.search(t):
            hits.append(Hit(s.file, s.line, "T10", f"内部の語「{term}」", t, True))
    if s.kind == "attr:title" and (len(t) > 30 or "。" in t or ". " in t):
        hits.append(Hit(s.file, s.line, "T11", "長い title", t, False))
    paren = PAREN_SUPPL_JA if lang == "ja" else PAREN_SUPPL_EN
    if paren.search(t) and len(t) < 60:
        hits.append(Hit(s.file, s.line, "T11", "括弧書きの補足", t, False))
    for rule, rx in RX[lang].items():
        if rx.search(t):
            hits.append(Hit(s.file, s.line, rule, RULES[rule].split("（")[0], t, False))
    if len(SEPARATORS.findall(t)) >= 2 or "→" in t or "->" in t:
        hits.append(Hit(s.file, s.line, "T9", "記号で区切り", t, False))
    return hits


# ---------------------------------------------------------------- 本体


def preset_path(name: str) -> str:
    """Public presets live in terms/; project presets with internal names live in
    terms/private/, which git ignores so they never reach the public repository."""
    for folder in (TERMS_DIR, os.path.join(TERMS_DIR, "private")):
        path = os.path.join(folder, f"{name}.txt")
        if os.path.isfile(path):
            return path
    raise SystemExit(f"--preset {name}: scripts/terms/{name}.txt も scripts/terms/private/{name}.txt もありません")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--lang", choices=["auto", "ja", "en"], default="auto", help="集める言語（既定 auto＝両方）")
    ap.add_argument("--terms", action="append", default=[], help="プロジェクト固有の内部の語（1 行 1 語・複数可）")
    ap.add_argument("--preset", action="append", default=[], help="語の一覧 scripts/terms/NAME.txt か scripts/terms/private/NAME.txt を足す")
    ap.add_argument("--no-default-terms", action="store_true", help="既定の語（terms/general.txt）を使わない")
    ap.add_argument("--keep", action="append", default=[], help="残すと決めた文字列（部分一致）を 1 行 1 つ。出さない")
    ap.add_argument("--only", help="出す規則をカンマ区切りで（例 T10,T11）")
    ap.add_argument("--json", help="結果を JSON で保存するパス")
    ap.add_argument("--max-text", type=int, default=90, help="画面に出す文字列の最大長")
    a = ap.parse_args(argv)

    term_files = [] if a.no_default_terms else [os.path.join(TERMS_DIR, "general.txt")]
    term_files += [preset_path(p) for p in a.preset]
    term_files += a.terms
    terms = [(t, term_matcher(t)) for t in load_terms(term_files)]
    keep = load_terms(a.keep)
    only = set(a.only.split(",")) if a.only else None

    hits: list[Hit] = []
    n_str = 0
    for f in a.files:
        if not os.path.isfile(f):
            print(f"skip (not a file): {f}", file=sys.stderr)
            continue
        for s in collect(f, a.lang):
            n_str += 1
            if any(k in s.text for k in keep):
                continue
            for h in check(s, terms):
                if only and h.rule not in only:
                    continue
                hits.append(h)

    seen = set()
    uniq: list[Hit] = []
    for h in hits:
        k = (h.file, h.line, h.rule, h.text)
        if k not in seen:
            seen.add(k)
            uniq.append(h)
    uniq.sort(key=lambda h: (h.file, h.line, h.rule))

    by_rule: dict[str, int] = {}
    for h in uniq:
        by_rule[h.rule] = by_rule.get(h.rule, 0) + 1

    for h in uniq:
        mark = "!!" if h.hard else "  "
        txt = h.text if len(h.text) <= a.max_text else h.text[: a.max_text] + "…"
        print(f"{mark} {h.file}:{h.line}  [{h.rule} {h.label}]  {txt}")
    print()
    print(f"文字列 {n_str} 件、候補 {len(uniq)} 件", end="")
    if by_rule:
        print("  " + "  ".join(f"{r}={by_rule[r]}" for r in sorted(by_rule)), end="")
    print()
    print("規則: " + "  ".join(f"{k}={v}" for k, v in RULES.items() if not only or k in only))
    hard = sum(1 for h in uniq if h.hard)
    if hard:
        print(f"!! 内部の語 {hard} 件。利用者の言葉に替えるか消す（SKILL.md 規則 10）")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fp:
            json.dump({"files": a.files, "strings": n_str, "hits": [asdict(h) for h in uniq], "by_rule": by_rule},
                      fp, ensure_ascii=False, indent=1)
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
