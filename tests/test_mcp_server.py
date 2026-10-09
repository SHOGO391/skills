import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import Client
from mcp.client.stdio import StdioServerParameters

import mcp_server as server


ORIGINAL = '<!DOCTYPE html>\r\n<html><head><!--保持--></head>\r\n<body><header>元</header><section id="hero">こんにちは</section>\r\n<footer>株式会社サンプル</footer></body></html>'
OLD = '<section id="hero">こんにちは</section>'
NEW = '<section id="hero" class="lpsr-hero-root"><h1>こんにちは</h1></section>'


class ReplacementTests(unittest.TestCase):
    def test_exact_unicode_and_crlf_preservation(self):
        result = server.replace_lp_section(ORIGINAL, OLD, NEW)
        self.assertEqual(result['html'], ORIGINAL.replace(OLD, NEW))
        self.assertTrue(result['outside_unchanged'])
        prefix, suffix = ORIGINAL.split(OLD)
        self.assertEqual(result['html'].encode(), prefix.encode() + NEW.encode() + suffix.encode())

    def test_invalid_or_ambiguous_fragment(self):
        for original, old, new in [('x', '', 'y'), ('x', 'missing', 'y'),
                                   ('x x', 'x', 'y'), ('aaa', 'aa', 'b'), ('x', 'x', '')]:
            with self.subTest(original=original, old=old):
                with self.assertRaises(ValueError):
                    server.replace_lp_section(original, old, new)

    def test_detects_outside_changes_and_wrong_replacement(self):
        updated = ORIGINAL.replace(OLD, NEW)
        self.assertTrue(server.verify_section_replacement(ORIGINAL, updated, OLD, NEW)['matches_expected_replacement'])
        for edited in [updated.replace('株式会社サンプル', '変更'), updated.replace('\r\n', '\n'),
                       updated.replace('</head>', '<style>x</style></head>'), updated.replace('こんにちは', '別文言')]:
            with self.subTest(edited=edited):
                result = server.verify_section_replacement(ORIGINAL, edited, OLD, NEW)
                self.assertFalse(result['matches_expected_replacement'])
                self.assertIsNone(result['outside_unchanged'])

    def test_byte_limits_apply_to_input_and_output(self):
        huge = 'あ' * (server.MAX_HTML_BYTES // 3 + 1)
        with self.assertRaises(ValueError):
            server.replace_lp_section(huge, 'あ', 'b')
        large = 'a' * server.MAX_HTML_BYTES
        with self.assertRaises(ValueError):
            server.replace_lp_section(large, large, large + 'b')
        with self.assertRaises(ValueError):
            server.replace_lp_section('x' + large[1:], 'x', 'bb')
        with self.assertRaises(ValueError):
            server.verify_section_replacement(ORIGINAL, huge, OLD, NEW)
        with self.assertRaises(ValueError):
            server.replace_lp_section('x', 'x', '\ud800')

    def test_bundled_workflows_and_validated_prompt_inputs(self):
        for name in ('lp-copy', 'lp-section-replace'):
            self.assertIn('##', server.workflow(name))
        with self.assertRaises(ValueError):
            server.workflow('../../LICENSE')
        for name in server.SECTIONS:
            self.assertIn(name, server.get_section_replace_workflow(name))
        with self.assertRaises(ValueError):
            server.section_prompt('everything', 'https://example.com')
        for url in ('file:///etc/passwd', 'https://user:pass@example.com', 'https://example.com\nx', 'https://example.com:bad'):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    server.lp_copy_prompt(url)


class StdioTests(unittest.TestCase):
    def test_legacy_initialize_handshake(self):
        async def exercise():
            params = StdioServerParameters(command=sys.executable,
                args=[str(Path(server.__file__).resolve())])
            async with Client(params, mode='legacy', read_timeout_seconds=15) as client:
                self.assertEqual(client.server_info.name, 'shogo391-lp-skills')
                result = await client.call_tool('get_lp_copy_workflow', {})
                self.assertFalse(result.is_error)
                self.assertIn('8段階', result.content[0].text)
        asyncio.run(asyncio.wait_for(exercise(), timeout=30))

    def test_real_subprocess_protocol(self):
        async def exercise():
            # Different cwd proves resources resolve from the server, not its host.
            with tempfile.TemporaryDirectory() as directory:
                params = StdioServerParameters(command=sys.executable,
                    args=[str(Path(server.__file__).resolve())], cwd=directory)
                async with Client(params, read_timeout_seconds=15) as client:
                    self.assertEqual(client.server_info.name, 'shogo391-lp-skills')
                    tools = (await client.list_tools()).tools
                    self.assertEqual({tool.name for tool in tools}, {
                        'get_lp_copy_workflow', 'get_section_replace_workflow',
                        'replace_lp_section', 'verify_section_replacement'})
                    for tool in tools:
                        self.assertTrue(tool.annotations.read_only_hint)
                    result = await client.call_tool('get_lp_copy_workflow', {})
                    self.assertFalse(result.is_error)
                    self.assertIn('8段階', result.content[0].text)
                    for section in server.SECTIONS:
                        result = await client.call_tool('get_section_replace_workflow', {'section': section})
                        self.assertFalse(result.is_error)
                        self.assertIn(section, result.content[0].text)
                    result = await client.call_tool('replace_lp_section', {
                        'original_html': ORIGINAL, 'original_section': OLD, 'replacement_section': NEW})
                    self.assertFalse(result.is_error)
                    self.assertEqual(result.structured_content['html'], ORIGINAL.replace(OLD, NEW))
                    result = await client.call_tool('verify_section_replacement', {
                        'original_html': ORIGINAL, 'updated_html': ORIGINAL.replace(OLD, NEW) + 'x',
                        'original_section': OLD, 'replacement_section': NEW})
                    self.assertFalse(result.structured_content['matches_expected_replacement'])
                    result = await client.call_tool('replace_lp_section', {
                        'original_html': 'same same', 'original_section': 'same', 'replacement_section': 'new'})
                    self.assertTrue(result.is_error)
                    result = await client.call_tool('get_section_replace_workflow', {'section': '../../LICENSE'})
                    self.assertTrue(result.is_error)
                    resources = (await client.list_resources()).resources
                    self.assertEqual(len(resources), 2)
                    for resource in resources:
                        result = await client.read_resource(str(resource.uri))
                        self.assertTrue(result.contents[0].text)
                    prompts = (await client.list_prompts()).prompts
                    self.assertEqual({prompt.name for prompt in prompts}, {'lp-copy', 'lp-section-replace'})
                    for name, args in [('lp-copy', {'url': 'https://example.com/'}),
                                       ('lp-section-replace', {'section': 'Hero', 'url': 'https://example.com/'})]:
                        prompt = await client.get_prompt(name, args)
                        self.assertIn('https://example.com/', prompt.messages[0].content.text)
        asyncio.run(asyncio.wait_for(exercise(), timeout=60))


if __name__ == '__main__':
    unittest.main()
