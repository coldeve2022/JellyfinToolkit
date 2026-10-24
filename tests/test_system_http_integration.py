"""Real loopback HTTP: collection, reviewed server clean, and differential restore."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from config import ToolkitConfig
from automation.orchestrator import collect_snapshot, run_orchestrator
from utils.jellyfin_api import execute_restore_plan


def test_manual_pixelation_tags_roundtrip_nfo_and_actual_http(tmp_path, monkeypatch):
    from censor import CensorService, CensorOptions, CensorState
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    video = tmp_path / 'generic.mp4'
    video.write_bytes(b'synthetic local media')
    nfo = video.with_suffix('.nfo')
    nfo.write_text('<movie><title>Generic</title><tag>keep-nfo</tag></movie>', encoding='utf-8')
    metadata = {'Id': 'generic-id', 'Name': 'Generic', 'Path': '/media/generic.mp4',
                'Type': 'Movie', 'Tags': ['keep-server'], 'Genres': ['genre'],
                'UserData': {'Played': True}, 'MediaSources': []}
    writes = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def respond(self, data, code=200):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            if self.headers.get('X-Emby-Token') != 'synthetic-key':
                return self.respond({}, 401)
            path = urlsplit(self.path).path
            if path == '/Users':
                return self.respond([{'Id': 'review-user'}])
            if path == '/Items':
                return self.respond({'Items': [deepcopy(metadata)], 'TotalRecordCount': 1})
            if path == '/Users/review-user/Items/generic-id':
                return self.respond(deepcopy(metadata))
            self.respond({}, 404)
        def do_POST(self):
            row = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            writes.append(row)
            metadata.update(row)
            self.respond(None, 204)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    try:
        cfg = ToolkitConfig(censor_visual_enabled=False,
            jellyfin_server=f'http://127.0.0.1:{server.server_port}', jellyfin_api_key='synthetic-key',
            library_path_mappings=[{'remote': '/media', 'local': str(tmp_path)}],
            library_scope_paths=['/media'])
        service = CensorService(cfg, data_dir=tmp_path / 'state')
        result = service.probe(video, CensorOptions(detect_visual=False))
        service.label(video, CensorState.CENSORED, base=result)
        report = service.metadata_sync().run_pending()
        assert report['done'] == 2 and not report['errors']
        assert metadata['Tags'] == ['keep-server', '有码']
        assert [x.text for x in ET.parse(nfo).getroot().findall('tag')] == ['keep-nfo', '有码']
        assert all('UserData' not in row and 'MediaSources' not in row for row in writes)
        assert metadata['UserData']['Played'] is True
        metadata['Tags'].append('concurrent-added')
        tree = ET.parse(nfo)
        ET.SubElement(tree.getroot(), 'tag').text = 'concurrent-added'
        tree.write(nfo, encoding='utf-8')
        service.remove_label(video)
        restored = service.metadata_sync().run_pending()
        assert restored['done'] == 2 and not restored['errors']
        assert metadata['Tags'] == ['keep-server', 'concurrent-added']
        assert [x.text for x in ET.parse(nfo).getroot().findall('tag')] == ['keep-nfo', 'concurrent-added']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
