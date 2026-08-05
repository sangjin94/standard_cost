"""배송비 단가 시스템 연동 래퍼: delivery_bridge.py를 subprocess로 실행하고 결과를 캐시."""
import json
import os
import subprocess
import sys
import time

_cache = {}          # key → (timestamp, data)
CACHE_TTL = 600      # 초. 배송단가 재산정이 잦지 않으므로 10분 캐시

_BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'delivery_bridge.py')


def _run(dp_dir, args, timeout=180):
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    proc = subprocess.run(
        [sys.executable, _BRIDGE, dp_dir] + [str(a) for a in args],
        capture_output=True, text=True, encoding='utf-8',
        timeout=timeout, env=env,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or '').strip().splitlines()
        raise RuntimeError(tail[-1] if tail else '배송단가 시스템 연동 실패')
    for line in reversed((proc.stdout or '').strip().splitlines()):
        line = line.strip()
        if line.startswith('{') or line.startswith('['):
            return json.loads(line)
    raise RuntimeError('배송단가 시스템 응답 파싱 실패')


def _cached(key, fetch, refresh=False):
    now = time.time()
    if not refresh and key in _cache:
        ts, data = _cache[key]
        if now - ts < CACHE_TTL:
            return data
    data = fetch()
    _cache[key] = (now, data)
    return data


def list_customers(dp_dir, refresh=False):
    """산정 이력이 있는 고객사 목록 [{id, name, result_rows}]"""
    return _cached(('list', dp_dir), lambda: _run(dp_dir, ['list'], timeout=60), refresh)


def get_summary(dp_dir, customer_id, refresh=False):
    """고객사 물동·배송단가 요약 {customer_name, volume, delivery}"""
    data = _cached(('summary', dp_dir, customer_id),
                   lambda: _run(dp_dir, ['summary', customer_id]), refresh)
    if data.get('error'):
        raise RuntimeError(data['error'])
    return data
