import json
import os
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import psutil

from courseware.config import settings
from courseware.service import initialize, owned_process, state_for


def test_init_creates_private_config_without_overwriting(tmp_path):
    path = tmp_path / 'backend/config.json'
    initialize(path)
    data = json.loads(path.read_text())
    assert len(data['CW_SECRET_KEY']) >= 32
    assert path.stat().st_mode & 0o777 == 0o600
    assert not owned_process({'pid': os.getpid(), 'created_at': 0})
    assert owned_process(state_for(psutil.Process())).pid == os.getpid()


def test_installed_style_server_manages_api_and_worker(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({
        'CW_DATABASE_URL': settings().database_url,
        'CW_SECRET_KEY': 'service-test-secret-' * 3,
        'CW_STORAGE_PATH': str(tmp_path / 'storage'),
        'CW_PUBLIC_ORIGIN': 'http://127.0.0.1:8814',
        'CW_SECURE_COOKIES': 'false',
    }))
    config.chmod(0o600)
    env = {k: v for k, v in os.environ.items() if not k.startswith('CW_')}
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    base = [sys.executable, '-m', 'courseware.cli']

    def command(*args, check=True):
        return subprocess.run(base + list(args) + ['--config', str(config)], env=env,
                              cwd=tmp_path, text=True, capture_output=True, timeout=30, check=check)

    command('init')
    before = config.read_bytes()
    command('init')
    assert config.read_bytes() == before
    state_file = tmp_path / 'run/server.json'
    try:
        command('server', 'start', '--port', str(port))
        state = json.loads(state_file.read_text())
        assert len(state['children']) == 2
        with urlopen(f'http://127.0.0.1:{port}/health/ready') as response:
            assert response.status == 200
        assert command('server', 'start', '--port', str(port), check=False).returncode != 0
        command('server', 'stop')
        assert not any(owned_process(p) for p in [state, *state['children']])
        command('server', 'start', '--port', str(port))
        state = json.loads(state_file.read_text())
        owned_process(state['children'][1]).terminate()
        for _ in range(100):
            if not owned_process(state):
                break
            time.sleep(0.1)
        assert not owned_process(state)
        assert not owned_process(state['children'][0])
    finally:
        command('server', 'stop', check=False)
