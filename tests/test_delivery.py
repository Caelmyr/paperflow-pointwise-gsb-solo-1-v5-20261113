import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paperflow.web import create_app

HEADERS = {'X-PaperFlow': '1'}
CAPACITY_PAGE = ROOT / 'demo' / 'practice' / 'capacity_delivery' / 'capacity_check.png'


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    directory = tmp_path_factory.mktemp('paperflow-delivery')
    app = create_app(directory, demo=False)
    client = app.test_client()

    def call(method, path, values=None, status=None):
        response = client.open('/api' + path, method=method, json=values, headers=HEADERS)
        if status is not None:
            assert response.status_code == status, (path, response.status_code, response.json)
        else:
            assert response.status_code < 400, (path, response.status_code, response.json)
        return response.json

    collection = call('POST', '/collections', {'name': '交付集合', 'category': '测试'})
    document = call('POST', '/documents', {'collection_id': collection['id'], 'name': '容量交付验收'})
    for source in (CAPACITY_PAGE, ROOT / 'demo' / 'practice' / 'color_marks.png', ROOT / 'demo' / 'demo_photo.png'):
        with source.open('rb') as handle:
            response = client.post('/api/documents/' + document['id'] + '/import',
                                   data={'files': (io.BytesIO(handle.read()), source.name)}, headers=HEADERS)
        assert response.status_code == 201, response.json
    document = call('GET', '/documents/' + document['id'])
    yield app, client, call, collection, document, directory
    app.extensions['scheduler'].shutdown()
    app.extensions['pipelines'].executor.shutdown()


def test_delivery_options_and_ready_plan(workspace):
    app, client, call, collection, document, directory = workspace
    options = call('GET', '/deliveries/options')
    assert set(item['profile'] for item in options['profiles']) >= {'strict', 'balanced', 'compact'}
    assert options['guarantees'] and options['quality_ladder']
    plan = call('POST', '/documents/' + document['id'] + '/deliveries',
                {'options': {'budget_bytes': 40 * 1024 * 1024, 'profile': 'balanced'}}, status=201)
    assert plan['status'] == 'ready'
    assert plan['page_count'] == 3
    assert plan['page_ids'] == document['page_ids']
    assert plan['jpeg_bytes'] <= plan['options']['budget_bytes']
    for page in plan['pages']:
        selection = page['selection']
        assert selection['quality'] >= plan['options']['floor_quality']
        assert selection['sampling'] == '4:4:4'
        assert selection['failures'] == []
        assert selection['margin'] >= 0
        assert page['applicable_metrics']
    # Unconfirmed plans must not download.
    response = client.get('/api/deliveries/' + plan['id'] + '/download')
    assert response.status_code == 409


def test_encoded_previews_are_decoded_from_staged_jpeg(workspace):
    app, client, call, collection, document, directory = workspace
    plan = call('POST', '/documents/' + document['id'] + '/deliveries',
                {'options': {'budget_bytes': 40 * 1024 * 1024, 'profile': 'balanced'}}, status=201)
    page_id = plan['page_ids'][0]
    for family in ('page', 'text', 'color', 'gradient'):
        preview = call('GET', f'/deliveries/{plan["id"]}/preview?page_id={page_id}&family={family}')
        assert preview['pixel_scale'] == 1.0
        assert '1:1' in preview['note']
        assert preview['candidate']['sha256'] == plan['pages'][0]['selection']['sha256']
        for which in ('source', 'candidate', 'difference'):
            response = client.get(f'/api/deliveries/{plan["id"]}/preview-image'
                                  f'?page_id={page_id}&family={family}&which={which}')
            assert response.status_code == 200
            assert response.mimetype == 'image/png'
            image = Image.open(io.BytesIO(response.data))
            assert image.size == tuple(preview['window'][2:])
    # Manual window still resolves and reports the requested origin.
    preview = call('GET', f'/deliveries/{plan["id"]}/preview?page_id={page_id}&family=text&x=500&y=900')
    assert preview['window'][:2] == [500, 900]


def test_confirmation_downloads_exactly_verified_bytes(workspace):
    app, client, call, collection, document, directory = workspace
    plan = call('POST', '/documents/' + document['id'] + '/deliveries',
                {'options': {'budget_bytes': 2 * 1024 * 1024, 'profile': 'balanced'}}, status=201)
    assert plan['status'] == 'ready'
    confirmed = call('POST', f'/deliveries/{plan["id"]}/confirm')
    assert confirmed['confirmed'] and confirmed['download_ready']
    verification = call('GET', f'/deliveries/{plan["id"]}/verify')
    assert verification['valid'], verification['errors']
    assert verification['jpeg_bytes'] == plan['jpeg_bytes']
    response = client.get('/api/deliveries/' + plan['id'] + '/download')
    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    assert archive.testzip() is None
    manifest = json.loads(archive.read('delivery.json'))
    assert manifest['plan_id'] == plan['id']
    assert manifest['page_count'] == len(document['page_ids'])
    import hashlib
    total = 0
    for entry in manifest['pages']:
        payload = archive.read(entry['file'])
        assert hashlib.sha256(payload).hexdigest() == entry['sha256']
        assert len(payload) == entry['bytes']
        image = Image.open(io.BytesIO(payload))
        assert image.size == (entry['width'], entry['height'])
        assert image.format == 'JPEG'
        assert image.mode == 'RGB'
        total += len(payload)
    assert total == manifest['jpeg_bytes'] == plan['jpeg_bytes']
    assert total <= plan['options']['budget_bytes']
    names = archive.namelist()
    assert len(names) == len(document['page_ids']) + 1
    # A second download serves the same confirmed bytes.
    response = client.get('/api/deliveries/' + plan['id'] + '/download')
    assert response.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(response.data)).read(manifest['pages'][0]['file']) == archive.read(manifest['pages'][0]['file'])


def test_budget_below_protection_floor_is_reported_not_hidden(workspace):
    app, client, call, collection, document, directory = workspace
    plan = call('POST', '/documents/' + document['id'] + '/deliveries',
                {'options': {'budget_bytes': 260 * 1024, 'profile': 'strict'}}, status=201)
    assert plan['status'] == 'infeasible_budget'
    allocation = plan['allocation']
    assert allocation['reason'] == 'budget_below_protection_floor'
    assert allocation['deficit_bytes'] > 0
    assert allocation['floor_bytes'] > plan['options']['budget_bytes']
    assert allocation['blocked_pages'], 'must list which pages hold the floor'
    for page in allocation['blocked_pages']:
        assert page['floor_quality'] >= plan['options']['floor_quality']
        assert page['binding'] in page['metrics']
    assert plan['jpeg_bytes'] is None
    assert all('selection' not in page for page in plan['pages'])
    assert allocation['steps'], 'relaxation attempts are shown even when the floor is not reached'
    assert all(step['to_quality'] >= plan['options']['floor_quality'] for step in allocation['steps'])
    assert allocation['alternatives']
    response = client.post('/api/deliveries/' + plan['id'] + '/confirm', headers=HEADERS)
    assert response.status_code == 409
    response = client.get('/api/deliveries/' + plan['id'] + '/download')
    assert response.status_code == 409
    # Relaxing protection is an explicit, visible choice.
    relaxed = call('POST', f'/deliveries/{plan["id"]}/replan',
                   {'options': {'profile': 'compact', 'budget_bytes': 2 * 1024 * 1024}}, status=201)
    comparison = relaxed['comparison']
    assert comparison['protection']['changed']
    assert comparison['budget']['changed']
    assert comparison['deficit_bytes']['before'] == allocation['deficit_bytes']
    assert comparison['after_status'] == 'ready'
    assert relaxed['jpeg_bytes'] <= relaxed['options']['budget_bytes']


def test_page_protection_override_and_replan_comparison(workspace):
    app, client, call, collection, document, directory = workspace
    page_ids = document['page_ids']
    first = call('POST', '/documents/' + document['id'] + '/deliveries',
                 {'options': {'budget_bytes': 900 * 1024, 'profile': 'balanced'}}, status=201)
    assert first['status'] == 'ready'
    tight = call('POST', '/documents/' + document['id'] + '/deliveries',
                 {'options': {'budget_bytes': 700 * 1024, 'profile': 'balanced'}, 'compared_to': first['id']}, status=201)
    comparison = tight['comparison']
    assert comparison['available']
    assert comparison['budget']['changed']
    assert comparison['jpeg_bytes']['after'] <= 700 * 1024
    assert any(row['before_quality'] != row['after_quality'] for row in comparison['pages'])
    overridden = call('POST', '/documents/' + document['id'] + '/deliveries',
                      {'options': {'budget_bytes': 5 * 1024 * 1024, 'profile': 'balanced',
                                   'page_profiles': {page_ids[0]: {'profile': 'strict', 'floor_quality': 60}}}},
                      status=201)
    assert overridden['pages'][0]['profile'] == 'strict'
    assert overridden['pages'][0]['floor_quality'] == 60
    assert overridden['pages'][0]['selection']['quality'] >= 60
    # Ceiling cap that cannot meet the protection threshold is reported, not silently lowered.
    capped = call('POST', '/documents/' + document['id'] + '/deliveries',
                  {'options': {'budget_bytes': 5 * 1024 * 1024, 'profile': 'strict',
                               'page_profiles': {page_ids[0]: {'ceiling_quality': 48}}}}, status=201)
    if capped['status'] == 'infeasible_protection':
        assert capped['allocation']['unsatisfiable_pages']
        failures = {item['metric'] for item in capped['allocation']['unsatisfiable_pages'][0]['failures']}
        assert failures
        assert capped['allocation']['unsatisfiable_pages'][0]['alternatives']


def test_input_change_invalidates_plan_and_confirmation(workspace):
    app, client, call, collection, document, directory = workspace
    plan = call('POST', '/documents/' + document['id'] + '/deliveries',
                {'options': {'budget_bytes': 2 * 1024 * 1024, 'profile': 'balanced'}}, status=201)
    assert plan['status'] == 'ready'
    call('POST', f'/deliveries/{plan["id"]}/confirm')
    assert call('GET', f'/deliveries/{plan["id"]}')['download_ready']
    page_id = document['page_ids'][1]
    call('POST', f'/pages/{page_id}/process', {'operation': 'rotate', 'params': {'angle': 90}})
    refreshed = call('GET', f'/deliveries/{plan["id"]}')
    assert refreshed['input_changed'] and not refreshed['download_ready']
    assert refreshed['input_notice']
    response = client.get('/api/deliveries/' + plan['id'] + '/download')
    assert response.status_code == 409
    response = client.post('/api/deliveries/' + plan['id'] + '/confirm', headers=HEADERS)
    assert response.status_code == 409
    # Regenerating produces a fresh plan bound to the new inputs.
    fresh = call('POST', '/documents/' + document['id'] + '/deliveries',
                 {'options': {'budget_bytes': 2 * 1024 * 1024, 'profile': 'balanced'}, 'compared_to': plan['id']}, status=201)
    assert fresh['input_fingerprint'] != plan['input_fingerprint']
    assert fresh['comparison']['input_changed']
    assert not fresh['input_changed']


def test_delivery_leaves_sources_and_history_untouched(workspace):
    app, client, call, collection, document, directory = workspace
    store = app.extensions['store']
    before = store.snapshot()
    page_state = {page_id: (page['current'], page['revision'], page['ocr'], page['original'])
                  for page_id, page in before['pages'].items()}
    checksums = {}
    for page_id, page in before['pages'].items():
        path = store.resolve(page['current'])
        checksums[page_id] = (page['sha256'], path.stat().st_size)
    source_checksums = {source_id: source['sha256'] for source_id, source in before.get('sources', {}).items()}
    existing_exports = dict(before.get('exports', {}))
    plan = call('POST', '/documents/' + document['id'] + '/deliveries',
                {'options': {'budget_bytes': 3 * 1024 * 1024, 'profile': 'balanced'}}, status=201)
    call('POST', f'/deliveries/{plan["id"]}/confirm')
    client.get('/api/deliveries/' + plan['id'] + '/download')
    after = store.snapshot()
    for page_id, page in after['pages'].items():
        assert (page['current'], page['revision'], page['ocr'], page['original']) == page_state[page_id]
        assert (page['sha256'], store.resolve(page['current']).stat().st_size) == checksums[page_id]
    for source_id, checksum in source_checksums.items():
        assert after['sources'][source_id]['sha256'] == checksum
    assert after.get('exports', {}) == existing_exports
    assert call('GET', '/audit/integrity?deep=1')['valid']
    # Delivery pages are native size and colour.
    for page in plan['pages']:
        width, height = page['width'], page['height']
        assert page['selection']['sampling'] == '4:4:4'
        record = after['pages'][page['page_id']]
        assert (record['width'], record['height']) == (width, height)


def test_delivery_manifest_records_metrics_and_sampling(workspace):
    app, client, call, collection, document, directory = workspace
    plan = call('POST', '/documents/' + document['id'] + '/deliveries',
                {'options': {'budget_bytes': 700 * 1024, 'profile': 'balanced'}}, status=201)
    assert plan['status'] == 'ready'
    verification = call('GET', f'/deliveries/{plan["id"]}/verify')
    assert verification['valid']
    for page in verification['pages']:
        assert page['bytes'] > 0
        assert page['quality'] >= plan['options']['floor_quality']
        assert page['protection_failures'] == []
        assert page['metrics']['psnr'] is not None
    manifest_run = client.get('/api/deliveries/' + plan['id'] + '/download')
    assert manifest_run.status_code == 409  # still unconfirmed


def test_scenarios_expose_what_each_protection_level_needs(workspace):
    app, client, call, collection, document, directory = workspace
    plan = call('POST', '/documents/' + document['id'] + '/deliveries',
                {'options': {'budget_bytes': 900 * 1024, 'profile': 'balanced'}}, status=201)
    scenarios = plan['scenarios']
    assert set(scenarios) >= {'strict', 'balanced', 'compact'}
    for scenario in scenarios.values():
        assert scenario['floor_bytes'] is None or scenario['ceiling_bytes'] >= scenario['floor_bytes']
        assert scenario['fits_budget'] == (scenario['floor_bytes'] is not None and scenario['floor_bytes'] <= 900 * 1024)
    assert scenarios['strict']['floor_bytes'] >= scenarios['compact']['floor_bytes']


def test_missing_or_extra_pages_are_rejected(workspace):
    app, client, call, collection, document, directory = workspace
    short = document['page_ids'][:-1]
    response = client.post('/api/documents/' + document['id'] + '/deliveries',
                           json={'options': {'budget_bytes': 900 * 1024, 'page_ids': short}}, headers=HEADERS)
    assert response.status_code == 400
    assert '漏页' in response.json['error'] or '完整页序' in response.json['error']
    response = client.post('/api/documents/' + document['id'] + '/deliveries',
                           json={'options': {'budget_bytes': 1024}}, headers=HEADERS)
    assert response.status_code == 400
