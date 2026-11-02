import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiment_archive.store import Archive, ArchiveError, file_sha
from experiment_archive.translation import TranslationService, blocks, protected, restore, validate_config


class FakeTranslator:
    name = 'fake'
    network = False
    identity = {'provider': 'fake', 'model': 'test-v1'}
    calls = 0
    def translate(self, text, target):
        self.calls += 1
        return text.replace('The experiment', '该实验').replace('Confidence interval', '置信区间')


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = Archive(Path(self.temp.name))
        self.archive.init()
        self.backend = FakeTranslator()
        self.service = TranslationService(self.archive, lambda config: self.backend)

    def tearDown(self):
        self.temp.cleanup()

    def test_protection_roundtrip_numbers_code_paths_hashes_and_terms(self):
        original = '# The experiment used 551 frames at lr=2e-4 and AbsRel .049. `raw.json` D:/data/run.csv SHA abcdef0123456789'
        encoded, values, prefix = protected(original, ['AbsRel'])
        translated = restore(encoded.replace('The experiment', '该实验'), values, prefix)
        self.assertEqual(translated, original.replace('The experiment', '该实验'))

    def test_modified_removed_or_extra_numeric_protection_fails(self):
        encoded, values, prefix = protected('The experiment used 551 frames')
        for output in (encoded.replace('0000QXZ','0001QXZ'), encoded + ' 999', encoded + encoded):
            with self.subTest(output=output), self.assertRaises(ArchiveError):
                restore(output, values, prefix)

    def test_protection_handles_multiline_markdown_without_regex_error(self):
        original = '# Title\n- lr 0.001\n> quote 51'
        encoded, values, prefix = protected(original)
        self.assertEqual(restore(encoded, values, prefix), original)

    def test_tables_and_code_fences_remain_verbatim(self):
        original = 'The experiment is done.\n\n|metric|value|\n|abs_rel|0.1|\n\n```python\nx=42\n```\n'
        result = self.service.text(original, 'zh-CN')
        self.assertIn('|abs_rel|0.1|', result['text'])
        self.assertIn('```python\nx=42\n```', result['text'])
        self.assertEqual(result['backend_calls'], 1)

    def test_cache_is_content_language_backend_and_glossary_specific(self):
        one = self.service.text('The experiment used 3 frames', 'zh-CN')
        two = self.service.text('The experiment used 3 frames', 'zh-CN')
        self.assertFalse(one['cached'])
        self.assertTrue(two['cached'])
        self.assertEqual(self.backend.calls, 1)
        self.service.text('The experiment used 4 frames', 'zh-CN')
        self.service.text('The experiment used 3 frames', 'zh-CN', terms=['frames'])
        self.assertEqual(self.backend.calls, 3)

    def test_record_translation_never_changes_original_or_generation(self):
        metadata = {'schema_version':1,'experiment_id':'exp_t','title':'The experiment',
            'source_refs':[{'id':'source'}],'domains':[]}
        self.archive.publish(metadata, 'The experiment used 551 frames.\n')
        before = self.archive.snapshot()[1]
        result = self.service.record('exp_t')
        self.assertTrue(result['machine_translation'])
        self.assertEqual(result['title'], '该实验')
        self.assertEqual(before, self.archive.snapshot()[1])
        self.assertEqual(self.archive.record('exp_t')['note'], 'The experiment used 551 frames.\n')

    def test_failure_does_not_publish_translation_cache(self):
        class Broken(FakeTranslator):
            def translate(self, text, target):
                return 'Lost the numbers 999'
        service = TranslationService(self.archive, lambda config: Broken())
        with self.assertRaises(ArchiveError):
            service.text('The experiment used 551 frames', 'zh-CN')
        self.assertFalse(list(service.root.glob('*.json')))

    def test_no_network_on_settings_or_original_reads(self):
        with patch('experiment_archive.translation.request_json', side_effect=AssertionError('network called')):
            service = TranslationService(self.archive)
            self.assertEqual(service.settings()['config']['provider'], 'local')
            service.save_settings({'provider':'google'})
            self.assertEqual(service.settings()['config']['provider'], 'google')

    def test_local_endpoint_must_stay_loopback(self):
        for address in ('https://example.com', 'http://192.168.1.1:11434', 'http://localhost@external.com', 'http://localhost/api/tags'):
            with self.subTest(address=address), self.assertRaises(ArchiveError):
                validate_config({'ollama_url':address})

    def test_long_prose_is_not_truncated_and_fences_stay_untranslated(self):
        original = 'Long prose. ' * 500 + '\n\n~~~\n551 secret code\n~~~\n'
        parts = list(blocks(original))
        self.assertEqual(''.join(part for _,part in parts), original)
        self.assertTrue(all(len(part) <= 1300 for raw,part in parts if not raw))
        self.assertIn((True,'551 secret code\n'),parts)

    def test_english_target_already_english_skips_provider(self):
        result = self.service.text('Already English prose.', 'en')
        self.assertEqual(result['text'], 'Already English prose.')
        self.assertEqual(self.backend.calls, 0)

    def test_evidence_translation_adds_fields_without_changing_metrics(self):
        result = {'results':[{'experiment_id':'exp_t','domains':[],'title':'The experiment',
            'snippet':'The experiment used 3 frames','revision':'v1','metric_rows':[{'value':'.123456789'}]}]}
        before = copy.deepcopy(result)
        self.service.evidence(result, 'zh-CN')
        self.assertEqual(result['results'][0]['metric_rows'], before['results'][0]['metric_rows'])
        self.assertEqual(result['results'][0]['snippet'], before['results'][0]['snippet'])
        self.assertTrue(result['results'][0]['translation']['machine_translation'])

    def test_local_protection_failure_keeps_original_paragraph(self):
        class SmallModel(FakeTranslator):
            name = 'local'
            def translate(self, text, target):
                return '新增错误指标 999'
        service = TranslationService(self.archive, lambda config: SmallModel())
        result = service.text('The experiment used 551 frames.', 'zh-CN')
        self.assertEqual(result['text'], 'The experiment used 551 frames.')
        self.assertEqual(result['untranslated_blocks'], 1)

    def test_cache_changes_with_model_revision_and_detects_tampering(self):
        self.service.text('The experiment', 'zh-CN')
        self.backend.identity = {'provider':'fake','model':'test-v2'}
        self.assertFalse(self.service.text('The experiment', 'zh-CN')['cached'])
        from experiment_archive.store import read_json, atomic_write, canonical
        path = next(path for path in self.service.root.glob('*.json')
                    if read_json(path)['descriptor']['backend']['model']=='test-v1')
        corrupted=read_json(path)
        corrupted['text']='Changed outside cache writer'
        atomic_write(path,canonical(corrupted))
        with self.assertRaises(ArchiveError):
            service=TranslationService(self.archive, lambda config: FakeTranslator())
            service.text('The experiment','zh-CN')


if __name__ == '__main__':
    unittest.main()
