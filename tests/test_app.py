import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from experiment_archive.app import AppService, make_server
from test_archive import FakeEmbedder, metadata, metric


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = AppService(self.root / 'archive', embedder_factory=FakeEmbedder)
        self.service.client.archive.publish(metadata(), '# 投影实验\n\n有明确负迁移。', [metric()])
        self.server = make_server(self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.service.close()
        self.temp.cleanup()

    def request(self, path, data=None, authenticated=True, extra_headers=None):
        headers = {'Authorization': 'Bearer ' + self.service.token} if authenticated else {}
        headers.update(extra_headers or {})
        if data is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.base + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
        return urllib.request.urlopen(req, timeout=5)

    def test_assets_load_without_api_auth_and_csp_blocks_remote_scripts(self):
        with self.request('/', authenticated=False) as response:
            self.assertIn(b'ExperimentArchive', response.read())
            self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
        for path in ('/app.js', '/style.css', '/icon.svg'):
            with self.request(path, authenticated=False) as response:
                self.assertEqual(response.status, 200)

    def test_api_requires_session_and_rejects_cross_origin_mutation(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/status', authenticated=False)
        self.assertEqual(error.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/jobs', {'kind': 'verify'}, extra_headers={'Origin': 'https://example.com'})
        self.assertEqual(error.exception.code, 403)

    def test_status_catalog_detail_and_revision_aware_csv(self):
        with self.request('/api/status') as response:
            self.assertEqual(json.load(response)['metric_count'], 1)
        with self.request('/api/experiments?structured=true') as response:
            self.assertEqual(json.load(response)['items'][0]['id'], 'exp_test')
        old = self.service.client.read('exp_test')['revision']
        self.service.client.archive.publish(metadata(), 'Changed', [metric('.9')], reason='correction')
        with self.request('/api/table?id=exp_test&metric=AbsRel&revision=' + old) as response:
            self.assertEqual(json.load(response)['rows'][0]['value'], metric()['value'])
        with self.request('/api/export?id=exp_test&metric=abs_rel') as response:
            self.assertIn(b'.9', response.read())
            self.assertIn('attachment', response.headers['Content-Disposition'])

    def test_lexical_search_returns_evidence_and_never_loads_model(self):
        with self.request('/api/search', {'query': '投影', 'mode': 'lexical'}) as response:
            output = json.load(response)
        self.assertEqual(output['results'][0]['experiment_id'], 'exp_test')
        self.assertIsNone(self.service.model)

    def test_invalid_pagination_and_arbitrary_static_paths_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/experiments?limit=99999')
        self.assertEqual(error.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/../manifest.json')
        self.assertEqual(error.exception.code, 404)

    def test_verify_job_completes_and_unconfigured_sync_fails(self):
        with self.request('/api/jobs', {'kind': 'verify'}) as response:
            self.assertEqual(response.status, 202)
        for _ in range(50):
            jobs = self.service.job_list()
            if jobs[0]['status'] not in ('running', 'queued'):
                break
            time.sleep(.02)
        self.assertEqual(jobs[0]['status'], 'completed')
        self.assertEqual(jobs[0]['result']['status'], 'pass')
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/jobs', {'kind': 'sync'})
        self.assertEqual(error.exception.code, 400)

    def test_graceful_quit_stops_accepting_requests(self):
        with self.request('/api/quit', {}) as response:
            self.assertEqual(response.status, 202)
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def test_translation_settings_save_without_network_and_validate_endpoint(self):
        with self.request('/api/translation/settings') as response:
            self.assertEqual(json.load(response)['config']['provider'],'local')
        with self.request('/api/translation/settings',{'provider':'ollama','ollama_model':'example:3b'}) as response:
            self.assertEqual(json.load(response)['config']['ollama_model'],'example:3b')
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/translation/settings',{'ollama_url':'http://external.example'})
        self.assertEqual(error.exception.code,400)

    def test_translation_job_is_derivative_and_revision_bound(self):
        from test_translation import FakeTranslator
        from experiment_archive.translation import TranslationService
        self.service.translator=TranslationService(self.service.client.archive,lambda config:FakeTranslator())
        before=self.service.client.archive.snapshot()[1]
        revision=self.service.client.read('exp_test')['revision']
        with self.request('/api/jobs',{'kind':'translate','experiment_id':'exp_test','revision':revision,'target':'en','provider':'local'}) as response:
            self.assertEqual(response.status,202)
        for _ in range(50):
            jobs=self.service.job_list()
            if jobs[0]['status'] not in ('running','queued'):break
            time.sleep(.02)
        self.assertEqual(jobs[0]['status'],'completed')
        self.assertEqual(jobs[0]['result']['revision'],revision)
        self.assertTrue(jobs[0]['result']['machine_translation'])
        self.assertEqual(self.service.client.archive.snapshot()[1],before)


if __name__ == '__main__':
    unittest.main()
