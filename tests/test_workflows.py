import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiment_archive.api import ArchiveClient, ExperimentRecord
from experiment_archive.app import AppService
from experiment_archive.importers import import_markdown, publish_import
from experiment_archive.pipeline import sync
from experiment_archive.search import SearchIndex, match_where
from experiment_archive.store import Archive, ArchiveError, canonical
from test_archive import metadata, metric, FakeEmbedder


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = ArchiveClient(self.root / 'archive')
        self.client.archive.init()

    def tearDown(self):
        self.temp.cleanup()

    def test_time_sort_happens_before_search_pagination(self):
        for index in range(16):
            meta = metadata(f'exp_{index:02}')
            meta['experiment_date'] = f'2026-10-{index+1:02}'
            self.client.publish(ExperimentRecord(meta, '投影实验'))
        index = SearchIndex(self.client.archive)
        newest = index.search('投影', mode='lexical', sort='time_desc', limit=3)
        oldest = index.search('投影', mode='lexical', sort='time_asc', limit=3, offset=1)
        self.assertEqual([h['experiment_id'] for h in newest['results']], ['exp_15', 'exp_14', 'exp_13'])
        self.assertEqual([h['experiment_id'] for h in oldest['results']], ['exp_01', 'exp_02', 'exp_03'])
        self.assertEqual(newest['total'], 16)
        with self.assertRaises(ArchiveError):
            index.search('投影', mode='lexical', sort='unsupported')

    def test_section_position_change_reuses_revision_and_preview_explains(self):
        source = self.root / 'log.md'
        source.write_text('## 2026-10-27 原实验\n投影结果\n', encoding='utf-8')
        config = self.root / 'pipeline.json'
        config.write_bytes(canonical({'schema_version': 1, 'sources': [{'id': 'log', 'adapter': 'markdown', 'path': str(source), 'split_sections': True}]}))
        sync(self.client.archive, config, index_mode='none')
        before = self.client.archive.snapshot()[1]
        source.write_text('## 2026-10-28 新实验\n新结果\n\n## 2026-10-27 原实验\n投影结果\n', encoding='utf-8')
        plan = sync(self.client.archive, config, dry_run=True)
        self.assertEqual(plan['counts'], {'new': 1, 'reuse': 1})
        reused = next(x for x in plan['details'][0]['records'] if x['action'] == 'reuse')
        self.assertEqual(reused['changes'], ['location_only'])
        self.assertEqual(before, self.client.archive.snapshot()[1])
        report = sync(self.client.archive, config, index_mode='none')
        self.assertEqual(report['tasks'][0]['revisions_published'], 1)
        self.assertEqual(len(self.client.archive.history(reused['experiment_id'])), 1)
        source.write_text(source.read_text(encoding='utf-8').replace('投影结果', '投影结果已更正'), encoding='utf-8')
        report = sync(self.client.archive, config, index_mode='none')
        self.assertEqual(report['tasks'][0]['revisions_published'], 1)

    def test_same_source_section_never_hides_changed_metric(self):
        meta = metadata()
        meta['source_refs'] = [{'id': 'fixture', 'section_sha256': 'section', 'sha256': 'old', 'start_line': 1}]
        publish_import(self.client.archive, meta, '投影', [metric()])
        updated = copy.deepcopy(meta)
        updated['source_refs'][0].update(sha256='new', start_line=30)
        result = publish_import(self.client.archive, updated, '投影', [metric('.5')], reason='metric correction')
        self.assertTrue(result['changed'])

    def test_bundle_export_and_explicit_lifecycle_survive_roundtrip(self):
        meta = metadata()
        meta['lifecycle'] = {'run': 'cancelled', 'evaluation': 'completed_subset', 'conclusion': 'reported_pass'}
        bundle = ExperimentRecord(meta, '投影', [metric()]).write_bundle(self.root / 'bundle')
        self.client.ingest(bundle)
        self.assertEqual(self.client.read('exp_test')['metadata']['lifecycle'], meta['lifecycle'])
        self.assertTrue(match_where(meta, 'lifecycle.run=cancelled'))
        self.assertFalse(match_where(meta, 'lifecycle.evaluation=completed'))
        with self.assertRaises(FileExistsError):
            ExperimentRecord(meta, '投影').write_bundle(bundle)
        report = self.client.accept()
        self.assertEqual(report['verification']['status'], 'pass')
        self.assertEqual(report['status'], 'needs_review')
        self.assertTrue((self.client.archive.root / '.cache/acceptance-receipt.json').is_file())

    def test_path_changes_are_validated_persisted_and_blocked_during_jobs(self):
        config = self.root / 'pipeline.json'
        config.write_bytes(canonical({'schema_version': 1, 'sources': [{'id': 'notes', 'adapter': 'markdown', 'path': 'old.md'}]}))
        source = self.root / '新路径.md'
        source.write_text('test', encoding='utf-8')
        service = AppService(self.client.archive.root)
        try:
            service.save_paths({'target': 'config', 'path': str(config)})
            service.save_paths({'target': 'source', 'id': 'notes', 'path': str(source)})
            self.assertEqual(json.loads(config.read_text(encoding='utf-8'))['sources'][0]['path'], str(source.resolve()))
            other = self.root / 'second'
            other.mkdir()
            service.save_paths({'target': 'archive', 'path': str(other)})
            self.assertEqual(service.client.archive.root, other.resolve())
            with patch('experiment_archive.app.subprocess.run') as run:
                run.return_value.returncode = 0
                run.return_value.stdout = '{"path": null}'
                self.assertIsNone(service.pick_path({'kind': 'directory'})['path'])
            service.jobs['busy'] = {'status': 'running'}
            with self.assertRaises(ArchiveError):
                service.save_paths({'target': 'config', 'path': str(config)})
        finally:
            service.close()
        restored = AppService(self.client.archive.root)
        try:
            self.assertEqual(restored.client.archive.root, other.resolve())
            self.assertEqual(restored.config, config.resolve())
        finally:
            restored.close()

    def test_acceptance_returns_failed_receipt_for_corrupt_archive(self):
        self.client.publish(ExperimentRecord(metadata(), 'original'))
        path = Path(self.client.read('exp_test')['folder']) / 'note.md'
        path.write_text('tampered', encoding='utf-8')
        report = self.client.accept()
        self.assertEqual(report['status'], 'failed')
        self.assertIn('Archive file changed', report['issues'][0]['error'])
        self.assertEqual(json.loads((self.client.archive.root / '.cache/acceptance-receipt.json').read_text(encoding='utf-8'))['status'], 'failed')

    def test_acceptance_detects_corrupt_index_and_cli_exit_codes(self):
        from experiment_archive.cli import main
        self.client.publish(ExperimentRecord(metadata(), '投影'))
        SearchIndex(self.client.archive).build(FakeEmbedder())
        pointer = json.loads((self.client.archive.root / '.cache/index.json').read_text(encoding='utf-8'))
        vector = self.client.archive.root / pointer['path'] / 'embeddings.npy'
        vector.write_bytes(b'corrupted')
        self.assertEqual(self.client.accept()['status'], 'failed')
        with patch('experiment_archive.cli.emit'), patch('experiment_archive.cli.run') as execute:
            for status, expected in [('pass', 0), ('failed', 1), ('needs_review', 3)]:
                execute.return_value = {'status': status}
                self.assertEqual(main(['--archive', str(self.client.archive.root), 'accept']), expected)


if __name__ == '__main__':
    unittest.main()
