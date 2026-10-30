from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import validate_skills  # noqa: E402

# Fixtures are assembled at run time so this file does not itself look like a leak.
SEP = "\\"


def found(text):
    return validate_skills.disclosures(text.encode("utf-8"))


class DisclosureTest(unittest.TestCase):
    def test_tokens(self):
        cases = {
            "API key (sk-)": "key=" + "sk-" + "ant-" + "a1B2" * 6,
            "Google API key": "AI" + "za" + "x" * 35,
            "Slack token": "xo" + "xb-" + "1234567890-abc",
            "JWT": ".".join("ey" + "J" + "a" * 12 for _ in range(2)) + "." + "s" * 12,
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertIn(label, found(text))

    def test_personal_paths(self):
        for text in ["C:" + SEP + "Users" + SEP + "名前" + SEP + "x",
                     "c:/" + "Users/Taro Yamada/Desktop",
                     "/" + "Users/山田/.ssh",
                     "/" + "home/a.b-c/x"]:
            with self.subTest(text):
                self.assertIn("personal absolute path", found(text))

    def test_placeholders_and_plain_text_pass(self):
        for text in ["~/.claude/skills/", "sk-short", "C:" + SEP + "Program Files" + SEP + "x", "https://example.com/users/"]:
            with self.subTest(text):
                self.assertEqual(found(text), [])

    def test_binary_files_do_not_crash(self):
        png = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + bytes(range(256))
        self.assertEqual(validate_skills.disclosures(png), [])
        self.assertEqual(validate_skills.disclosures(b"\xff\xfe broken utf-8"), [])


if __name__ == "__main__":
    unittest.main()
