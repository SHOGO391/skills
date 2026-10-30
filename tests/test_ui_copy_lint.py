from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ui-copy" / "scripts"))
import ui_copy_lint  # noqa: E402

LEAK = "内部コードネーム甲"
JS = f'export const title = "{LEAK}で予測した結果です";\n'


class BundleSkipTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.terms = self.root / "terms.txt"
        self.terms.write_text(LEAK + "\n", encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def lint(self, name, text, *extra):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return ui_copy_lint.main(["--no-default-terms", "--terms", str(self.terms), *extra, str(path)])

    def test_word_like_names_are_read(self):
        for name in ["ui-messages.js", "app.settings.js", "copy-overview.mjs"]:
            with self.subTest(name):
                self.assertEqual(self.lint(name, JS), 1)

    def test_long_line_html_is_read(self):
        html = "<html><body>" + "<span>x</span>" * 300 + f"<p>{LEAK}で予測した結果です</p></body></html>"
        self.assertEqual(self.lint("index.html", html), 1)

    def test_bundles_are_skipped_unless_asked(self):
        bundles = {"main.3f2a9c1b.js": JS, "index-DiwrgTda.js": JS, "vendor.min.js": JS,
                   "app.js": "var a=1;" * 400 + "\n" + JS}
        for name, text in bundles.items():
            with self.subTest(name):
                self.assertEqual(self.lint(name, text), 0)
                self.assertEqual(self.lint(name, text, "--include-bundles"), 1)


if __name__ == "__main__":
    unittest.main()
