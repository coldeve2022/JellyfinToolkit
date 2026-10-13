"""Real loopback HTTP: collection, reviewed server clean, and differential restore."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import urlsplit

from config import ToolkitConfig
from automation.orchestrator import collect_snapshot, run_orchestrator
from utils.jellyfin_api import execute_restore_plan


def test_actual_http_viewer_scope_review_clean_restore(tmp_path, monkeypatch):
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    video = tmp_path / 'DEMO-101.mp4'
    video.write_bytes(b'synthetic media')
    metadata = {'Id': 'synthetic-movie', 'Name': 'DEMO-101', 'Path': str(video),
                'Type': 'Movie', 'Tags': ['original', '发行:dirty'], 'Genres': [],
                'MediaSources': [{'Size': 2_000_000_000}], 'Studios': [{'Name': 'Synthetic Studio'}]}
    writes, user_reads = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, value, code=200):
            payload = json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ('/System/Info/Public', '/System/Info'):
                return self.respond({'ProductName': 'Jellyfin Server', 'Version': '10.11.0', 'ServerName': 'Synthetic'})
            if self.headers.get('X-Emby-Token') != 'synthetic-api-secret':
                return self.respond({}, 401)
            if path == '/Users':
                return self.respond([{'Id': 'admin', 'Name': 'Admin', 'Policy': {'IsAdministrator': True}},
                                     {'Id': 'viewer', 'Name': 'Viewer', 'Policy': {'IsAdministrator': False}}])
            if path == '/Items':
                return self.respond({'Items': [deepcopy(metadata)], 'TotalRecordCount': 1})
            if path.startswith('/Users/'):
                user = path.split('/')[2]
                user_reads.append(user)
                row = deepcopy(metadata)
                row['UserData'] = {'Played': user == 'admin', 'PlayCount': 1 if user == 'admin' else 0,
                                   'IsFavorite': user == 'admin', 'PlaybackPositionTicks': 0}
                if path.endswith('/Items'):
                    return self.respond({'Items': [row], 'TotalRecordCount': 1})
                return self.respond(row)
            return self.respond({}, 404)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            if urlsplit(self.path).path == '/Items/synthetic-movie':
                data = json.loads(body)
                metadata.update(data)
                writes.append(data)
            self.respond(None, 204)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    try:
        cfg = ToolkitConfig(library_source='api', jellyfin_server=f'http://127.0.0.1:{server.server_port}',
                            jellyfin_api_key='synthetic-api-secret', jellyfin_user_id='viewer', ai_enabled=False)
        snapshot = collect_snapshot(cfg, log=lambda _: None)
        assert len(snapshot['items']) == 1
        assert snapshot['items'][0].behavior_user_id == 'viewer'
        assert not snapshot['items'][0].is_played and not snapshot['items'][0].is_favorite
        assert user_reads == ['viewer']
        preview = run_orchestrator(cfg, {'selected_actions': ['jellyfin_clean']}, log=lambda _: None)
        assert preview['status'] == 'planned', preview.get('error')
        assert len(preview['plan']['actions']) == 1 and writes == []
        assert 'synthetic-api-secret' not in json.dumps(preview)
        metadata['Tags'].append('concurrent-added')
        result = run_orchestrator(cfg, {'approved_plan_id': preview['plan']['id'],
                   'apply_jellyfin_clean': True, 'refresh_library': False}, log=lambda _: None)
        assert result['status'] == 'succeeded', result.get('error')
        assert metadata['Tags'] == ['original', 'concurrent-added']
        backup = result['operations'][0]['detail']['backup']
        metadata['Tags'].append('later-added')
        restored = execute_restore_plan(cfg.jellyfin_server, cfg.jellyfin_api_key, backup, refresh=False)
        assert restored['status'] == 'succeeded', restored.get('error')
        assert metadata['Tags'] == ['original', 'concurrent-added', 'later-added', '发行:dirty']
        assert all('UserData' not in write for write in writes)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
