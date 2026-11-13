"""黑白分区渲染测试：纯两色输出、细字/表格线连贯、反白与插图层次。"""
import io
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paperflow import monochrome


def _font(size):
    path = ROOT / 'resources' / 'NotoSansCJK-Regular.ttc'
    return ImageFont.truetype(str(path), size)


@pytest.fixture(scope='module')
def mixed():
    width, height = 1200, 1600
    canvas = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(canvas)
    thin = _font(14)
    body = _font(24)
    for index in range(10):
        draw.text((60, 90 + index * 30), f'细字第{index + 1}行，标点、句号。ABCDEFG 0123456789 (brackets)', font=thin, fill=(25, 25, 25))
    for row in range(6):
        draw.line((60, 440 + row * 42, 1140, 440 + row * 42), fill=(15, 15, 15), width=2)
    for column in range(5):
        draw.line((60 + column * 216, 440, 60 + column * 216, 650), fill=(15, 15, 15), width=2)
    draw.text((70, 455), '表头1', font=body, fill=(15, 15, 15))
    draw.rectangle((60, 700, 1140, 800), fill=(12, 12, 12))
    draw.text((110, 725), '反白标题 REVERSED', font=_font(42), fill='white')
    illustration = Image.new('L', (480, 360), 255)
    gradient = ImageDraw.Draw(illustration)
    for x in range(480):
        gradient.line((x, 0, x, 360), fill=int(248 - x * 230 / 479))
    gradient.ellipse((200, 60, 420, 260), fill=95)
    gradient.rectangle((220, 280, 460, 345), fill=195)
    canvas.paste(illustration.convert('RGB'), (80, 860))
    draw.text((80, 1240), '连续层次插图', font=body, fill=(30, 30, 30))
    art = Image.new('RGB', (520, 200), 'white')
    pen = ImageDraw.Draw(art)
    for index in range(8):
        pen.arc((20 + index * 5, 20 + index * 4, 500 - index * 5, 180 - index * 4), 200, 340, fill=(10, 10, 10), width=2)
    pen.line((20, 180, 500, 20), fill=(10, 10, 10), width=2)
    canvas.paste(art, (80, 1290))
    draw.text((60, 1540), '脚注：小号细字仍应连贯可辨。PaperFlow 2026', font=thin, fill=(20, 20, 20))
    array = np.asarray(canvas).astype(np.float32)
    shade = np.linspace(0.93, 1.0, width, dtype=np.float32)[None, :, None]
    noisy = np.clip(array * shade + np.random.default_rng(3).normal(0, 2, array.shape), 0, 255).astype(np.uint8)
    return cv2.cvtColor(noisy, cv2.COLOR_RGB2BGR)


def test_output_is_only_two_levels(mixed):
    ink, report = monochrome.render(mixed, {'scale': 1, 'cell': 8, 'assumed_dpi': 300})
    assert ink.dtype == np.bool_
    assert report['two_level'] is True
    png = monochrome.encode_png(ink)
    with Image.open(io.BytesIO(png)) as image:
        assert image.mode == '1'
        assert image.convert('L').getextrema() == (0, 255)


def test_blocked_from_rebinarising_an_already_binary_page(mixed):
    ink, _ = monochrome.render(mixed, {'scale': 1})
    binary = np.where(ink[:, :, None], 0, 255).repeat(3, axis=2).astype(np.uint8)
    with pytest.raises(Exception) as caught:
        monochrome.render(binary, {'scale': 1})
    assert '二值化' in str(caught.value)


def test_thin_text_and_table_lines_stay_connected(mixed):
    ink, report = monochrome.render(mixed, {'scale': 1, 'cell': 8, 'assumed_dpi': 300})
    source = monochrome.gray(mixed)
    # 表格横线：每一行都应有连续墨迹，不能断成碎点。
    for y in (440, 482, 524, 566, 608, 650):
        line = ink[y, 60:1140]
        assert line.mean() > 0.95, (y, line.mean())
        transitions = np.count_nonzero(np.diff(line.astype(np.int8)))
        assert transitions <= 4, (y, transitions)
    # 细字：行内应保留笔画，且碎点不占多数。
    text_band = ink[85:385, 60:900]
    components = cv2.connectedComponentsWithStats(text_band.astype(np.uint8), 8)[2][1:, cv2.CC_STAT_AREA]
    assert components.size > 100
    assert np.mean(components <= 2) < 0.05, np.mean(components <= 2)


def test_reversed_heading_keeps_white_strokes(mixed):
    ink, report = monochrome.render(mixed, {'scale': 1, 'cell': 8, 'assumed_dpi': 300})
    bar = ink[700:800, 60:1140]
    assert bar.mean() > 0.85              # 大面积保持实黑
    white = 1 - float(bar.mean())
    assert white > 0.02                   # 白色笔画仍被挖出
    reverse = [region for region in report['regions'] if region['kind'] == 'reverse']
    assert reverse and all(region['preserved'] for region in reverse)


def test_illustration_keeps_continuous_levels(mixed):
    ink, report = monochrome.render(mixed, {'scale': 1, 'cell': 8, 'assumed_dpi': 300})
    illustration = [region for region in report['regions'] if region['kind'] == 'illustration']
    assert illustration, '应识别出连续层次插图区'
    region = max(illustration, key=lambda item: item['rect'][2] * item['rect'][3])
    assert region['tone_levels'] >= 4
    assert region['preserved'] is True
    # 深端更密、亮端更稀，层次方向正确。
    assert region['dark_end_density'] > region['light_end_density']


def test_output_scaling_is_rechecked_not_rebinarised(mixed):
    one, report_one = monochrome.render(mixed, {'scale': 1, 'cell': 8, 'assumed_dpi': 300})
    half, report_half = monochrome.render(mixed, {'scale': 0.5, 'cell': 8, 'assumed_dpi': 300})
    assert half.shape == (mixed.shape[0] // 2, mixed.shape[1] // 2)
    assert report_half['pattern']['cell_px'] == 4      # 保持网点物理尺寸
    # 缩小后应主动给出可读性警告，而不是静默降质。
    assert any('像素' in warning or '碎点' in warning for warning in report_half['warnings'])
    # 放大后仍然只有两色，且网点尺度随之变化。
    double, report_double = monochrome.render(mixed, {'scale': 2, 'cell': 8, 'assumed_dpi': 300})
    assert double.shape == (mixed.shape[0] * 2, mixed.shape[1] * 2)
    assert report_double['pattern']['cell_px'] == 16
    assert np.unique(double).size == 2


def test_report_exposes_regions_and_losses(mixed):
    ink, report = monochrome.render(mixed, {'scale': 1, 'cell': 8, 'assumed_dpi': 300})
    assert report['regions']
    assert all({'kind', 'rect', 'preserved'} <= set(region) for region in report['regions'])
    assert report['settings']['zones'] == []
    assert report['pattern']['cell_mm'] > 0.1


def test_manual_zone_override_changes_rendering(mixed):
    values = {'scale': 1, 'cell': 8, 'assumed_dpi': 300}
    base, base_report = monochrome.render(mixed, values)
    text_zone = {'kind': 'text', 'rect': [80, 860, 480, 360]}
    forced, report = monochrome.render(mixed, {**values, 'zones': [text_zone]})
    assert report['zone_overrides'] == 1
    assert report['settings']['zones'] == [text_zone]
    # 把插图改判为正文后，原网点区应变为笔画式阈值结果。
    assert abs(int(forced.sum()) - int(base.sum())) > 0
    illustration = [region for region in report['regions'] if region['kind'] == 'illustration']
    assert all(region['source'] == 'manual' or region['rect'] != text_zone['rect'] for region in illustration)


def test_full_page_report_has_no_blank_noise(mixed):
    ink, report = monochrome.render(mixed, {'scale': 1, 'cell': 8, 'assumed_dpi': 300})
    source = monochrome.gray(mixed)
    paper = source > 240
    assert float(ink[paper].mean()) < 0.005
    assert report['boundary']['stable'] is True


@pytest.fixture(scope='module')
def app_client(tmp_path_factory):
    from paperflow.web import create_app
    directory = tmp_path_factory.mktemp('mono-service')
    app = create_app(directory, demo=False)
    client = app.test_client()
    yield app, client, directory
    app.extensions['scheduler'].shutdown()
    app.extensions['pipelines'].executor.shutdown()


def _request(client, method, path, values=None):
    response = client.open('/api' + path, method=method, json=values, headers={'X-PaperFlow': '1'})
    return response


def test_service_preview_commit_share_one_result(mixed, app_client):
    app, client, directory = app_client
    collection = _request(client, 'POST', '/collections', {'name': '黑白验收', 'category': '测试'}).json
    document = _request(client, 'POST', '/documents', {'collection_id': collection['id'], 'name': '混合页'}).json
    payload = _encode_png_bytes(mixed)
    response = client.post('/api/documents/' + document['id'] + '/import', data={'files': (io.BytesIO(payload), 'mixed.png')}, headers={'X-PaperFlow': '1'})
    page_id = response.json['page_ids'][0]
    settings = {'scale': 1, 'cell': 8, 'style': 'cluster', 'assumed_dpi': 300}
    preview = _request(client, 'POST', '/pages/' + page_id + '/mono-preview', settings).json
    assert preview['two_level'] is True
    assert preview['report']['regions']
    image = Image.open(io.BytesIO(client.get(preview['url']).data))
    assert image.mode == '1'
    committed = _request(client, 'POST', '/pages/' + page_id + '/mono-commit', {'preview_id': preview['preview_id'], **settings}).json
    assert committed['mono']['signature'] == preview['signature']
    assert committed['mono']['report']['two_level'] is True
    status = _request(client, 'GET', '/pages/' + page_id + '/mono').json
    assert status['committed'] and status['two_level']
    current = Image.open(io.BytesIO(client.get('/api/pages/' + page_id + '/image').data))
    assert current.mode == '1' and current.convert('L').getextrema() == (0, 255)
    # 已确认页不得被其他滤镜二次二值化。
    blocked = _request(client, 'POST', '/pages/' + page_id + '/process', {'operation': 'contrast', 'params': {'contrast': 1.1}})
    assert blocked.status_code == 409 and blocked.json['code'] == 'mono_committed'
    # 预览与确认参数不一致时拒绝确认。
    stale = _request(client, 'POST', '/pages/' + page_id + '/mono-commit', {'preview_id': preview['preview_id'], **settings, 'cell': 16})
    assert stale.status_code == 409 and stale.json['code'] == 'preview_stale'


def test_regions_and_exports_use_committed_result(mixed, app_client):
    import fitz
    import zipfile
    app, client, directory = app_client
    collection = _request(client, 'POST', '/collections', {'name': '导出验收', 'category': '测试'}).json
    document = _request(client, 'POST', '/documents', {'collection_id': collection['id'], 'name': '导出页'}).json
    response = client.post('/api/documents/' + document['id'] + '/import', data={'files': (io.BytesIO(_encode_png_bytes(mixed)), 'mixed.png')}, headers={'X-PaperFlow': '1'})
    page_id = response.json['page_ids'][0]
    settings = {'scale': 1, 'cell': 8, 'assumed_dpi': 300}
    preview = _request(client, 'POST', '/pages/' + page_id + '/mono-preview', settings).json
    _request(client, 'POST', '/pages/' + page_id + '/mono-commit', {'preview_id': preview['preview_id'], **settings})
    region = _request(client, 'POST', '/pages/' + page_id + '/regions', {'name': '插图', 'kind': 'illustration', 'region': [80, 860, 480, 360]}).json
    assert region['mono']['two_level'] is True
    downloaded = client.get('/api/pages/' + page_id + '/regions/' + region['id'])
    cropped = Image.open(io.BytesIO(downloaded.data))
    assert cropped.mode == '1'
    for kind, options in [('pdf', {'dpi': 150}), ('images', {'image_format': 'png'}), ('zip', {'include_originals': True})]:
        record = _request(client, 'POST', '/documents/' + document['id'] + '/export', {'kind': kind, 'options': options}).json
        data = client.get('/api/exports/' + record['id'] + '/download').data
        verify = _request(client, 'GET', '/exports/' + record['id'] + '/verify').json
        assert verify['valid'], verify
        if kind == 'pdf':
            pdf = fitz.open(stream=data, filetype='pdf')
            pixmaps = [fitz.Pixmap(pdf, image[0]) for page in pdf for image in page.get_images(full=True)]
            assert all(pix.n == 1 for pix in pixmaps)
            pdf.close()
        elif kind == 'images':
            archive = zipfile.ZipFile(io.BytesIO(data))
            sample = Image.open(io.BytesIO(archive.read(archive.namelist()[0])))
            assert sample.mode == '1'
        else:
            archive = zipfile.ZipFile(io.BytesIO(data))
            metadata = json.loads(archive.read('metadata.json'))
            assert metadata['pages'][0]['mono']['signature'] == committed_signature(client, page_id)


def committed_signature(client, page_id):
    return _request(client, 'GET', '/pages/' + page_id + '/mono').json['signature']


def _encode_png_bytes(bgr):
    success, encoded = cv2.imencode('.png', bgr)
    assert success
    return encoded.tobytes()
