"""文字层字体与排布专项验收。

不依赖 Tesseract：OCR 结果以手工构造的文字框注入，专门验证
- 生僻字 / 辅助平面字符 / 组合字符序列的忠实写入与回读；
- 随项目字体与项目字体回退链、缺字阻断（不近似替换）；
- 真实字宽排布、窄框溢出标注（不缩小字号、不叠字）；
- 局部竖排文字的逐字定位；
- 交付前文字层预览与导出后逐字校验；
- 失败时不产生宣称完整的导出记录。
"""
import io
import sys
from pathlib import Path

import fitz
import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paperflow.core import DomainError, Store
from paperflow.documents import Documents
from paperflow.exports import Exporter
from paperflow.fonts import BUNDLED_FONT_ID, FontLibrary
from paperflow.ocr import text_from_boxes
from paperflow.textlayer import TextLayerBuilder, build_tounicode, verify_roundtrip
from paperflow.web import create_app

HEADERS = {'X-PaperFlow': '1'}
DEJAVU = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'


def make_ocr(boxes, width=900, height=1200):
    return {
        'text': text_from_boxes(boxes),
        'boxes': boxes,
        'language': 'chi_sim+eng',
        'psm': 3,
        'confidence': 92.0,
        'width': width,
        'height': height,
        'recognized_at': '2026-11-13T00:00:00.000+00:00',
        'edited_at': None,
        'edited': False,
        'engine': 'manual',
        'stale': False,
    }


def word(word_id, text, x, y, width, height, orientation='h', line='l'):
    return {
        'id': word_id, 'text': text, 'confidence': 92.0,
        'x': x, 'y': y, 'width': width, 'height': height,
        'line': line, 'orientation': orientation, 'edited': False,
    }


@pytest.fixture()
def workspace(tmp_path):
    app = create_app(tmp_path / 'ws', demo=False)
    store = app.extensions['store']
    documents = app.extensions['documents']
    exporter = app.extensions['exporter']
    collection = store.create_collection('集合', '分类')
    yield app, store, documents, exporter, collection['id']
    app.extensions['scheduler'].shutdown()
    app.extensions['pipelines'].executor.shutdown(wait=True)


def add_page(documents, store, document_id, boxes, size=(900, 1200)):
    image = np.full((size[1], size[0], 3), 255, dtype=np.uint8)
    page = documents.add_image(document_id, image, '页面')
    with store.transaction() as state:
        state['pages'][page['id']]['ocr'] = make_ocr(boxes, size[0], size[1])
    return store.get('pages', page['id'])


def flat(text):
    return text.replace('\r', '').replace('\n', '').replace(' ', '').replace('\xa0', '')


class FontUpload:
    def __init__(self, path, filename):
        self.filename = filename
        self.stream = open(path, 'rb')


def test_bundled_font_covers_rare_and_supplementary_chars(workspace):
    _, _, _, exporter, _ = workspace
    library = exporter.fonts
    loaded = library.load(BUNDLED_FONT_ID)
    # BMP 扩展 A 区生僻字与辅助平面 B 区字符都有可靠字形
    for codepoint in (0x4DAE, 0x3F06, 0x2CC56):
        assert loaded.has_glyph(codepoint), hex(codepoint)


def test_coverage_reports_missing_combining_marks_without_replacement(workspace):
    _, _, _, exporter, _ = workspace
    report = exporter.fonts.coverage('b̂ ǎ', [BUNDLED_FONT_ID])
    assert not report['complete']
    assert {item['codepoint'] for item in report['unsupported']} == {'U+0302'}
    # 不修改原文：返回结构只做诊断
    assert report['supported'] >= 3


def test_rare_name_and_combining_sequence_roundtrip(workspace):
    _, store, documents, exporter, collection_id = workspace
    document = store.create_document(collection_id, '生僻字文档')
    boxes = [
        word('w1', '普通汉字文档', 80, 100, 300, 48),
        word('w2', '䶮𬱖㼆生僻姓名', 80, 200, 300, 48),
        word('w3', 'Café résumé b̂ ǎ', 80, 300, 380, 40),
    ]
    page = add_page(documents, store, document['id'], boxes)
    # 仅内置字体时组合附加符号 U+0302 无支持，导出必须阻断
    with pytest.raises(DomainError) as exc:
        exporter.export(document['id'], 'searchable_pdf',
                        {'font_ids': [BUNDLED_FONT_ID]})
    assert exc.value.code == 'glyph_unsupported'
    assert 'U+0302' in exc.value.message
    # 阻断后没有留下任何导出记录或文件
    assert not store.snapshot().get('exports')
    assert not list((store.root / 'exports').iterdir())

    # 上传 DejaVu 作为回退字体后可完整导出
    upload = FontUpload(DEJAVU, 'DejaVuSans.ttf')
    try:
        deja = exporter.fonts.upload(upload)
    finally:
        upload.stream.close()
    record = exporter.export(
        document['id'], 'searchable_pdf',
        {'font_ids': [BUNDLED_FONT_ID, deja['id']]})
    pdf = fitz.open(store.resolve(record['path']))
    try:
        assert len(pdf) == 1
        extracted = pdf[0].get_text()
        assert flat(extracted) == flat(page['ocr']['text'])
        for probe in ('䶮', '𬱖', '㼆', '生僻姓名', 'Café', 'résumé', 'b̂', 'ǎ'):
            assert pdf[0].search_for(probe), probe
        # 空格必须回读为 U+0020 而非 NBSP
        assert '\xa0' not in extracted
    finally:
        pdf.close()


def test_narrow_box_long_word_is_flagged_not_shrunk(workspace):
    _, store, documents, exporter, collection_id = workspace
    document = store.create_document(collection_id, '窄框长词')
    long_word = 'Internationalization'
    boxes = [word('w1', long_word, 80, 400, 60, 40)]
    page = add_page(documents, store, document['id'], boxes)
    report = exporter.text_layer_preview(document['id'])
    assert report['complete']
    overflow = report['pages'][0]['overflow']
    assert len(overflow) == 1
    assert overflow[0]['word_id'] == 'w1'
    assert overflow[0]['measured_width'] > overflow[0]['box_width']
    # 实际写入字号仍由框高决定，没有被缩成一小团
    placement = next(item for item in report['pages'][0]['placements']
                     if item['text'].startswith('International'))
    assert placement['font_size'] == pytest.approx(40 * 72 / 150 * 0.83, rel=0.01)
    record = exporter.export(document['id'], 'searchable_pdf')
    # 溢出是人工核对警告而非错误：导出成功且记录中保留标记
    assert record['text_layer']['overflow']
    pdf = fitz.open(store.resolve(record['path']))
    try:
        assert pdf[0].search_for(long_word)
        assert flat(pdf[0].get_text()) == flat(long_word)
    finally:
        pdf.close()


def test_vertical_text_keeps_per_glyph_positioning(workspace):
    _, store, documents, exporter, collection_id = workspace
    document = store.create_document(collection_id, '竖排文档')
    boxes = [
        word('w1', '横排标题', 80, 100, 240, 44, 'h'),
        word('w2', '竖排文字测试', 700, 100, 44, 300, 'v'),
    ]
    page = add_page(documents, store, document['id'], boxes)
    report = exporter.text_layer_preview(document['id'])
    vertical = [item for item in report['pages'][0]['placements']
                if item['orientation'] == 'v']
    assert [item['text'] for item in vertical] == list('竖排文字测试')
    # 每字独立定位，y 坐标严格自上而下递增
    ys = [item['rect_pdf'][1] for item in vertical]
    assert ys == sorted(ys) and len(set(ys)) == len(ys)
    # 竖排字号依据框宽而非整列框高
    assert all(item['font_size'] < 30 for item in vertical)

    record = exporter.export(document['id'], 'searchable_pdf')
    pdf = fitz.open(store.resolve(record['path']))
    try:
        assert flat(pdf[0].get_text()) == flat(page['ocr']['text'])
        for char in '竖排文字测试':
            assert pdf[0].search_for(char)
    finally:
        pdf.close()


def test_mixed_width_advances_are_distinct(workspace):
    _, store, documents, exporter, collection_id = workspace
    document = store.create_document(collection_id, '混排字宽')
    boxes = [word('w1', '中英混排PaperFlow宽度', 80, 100, 500, 44)]
    add_page(documents, store, document['id'], boxes)
    upload = FontUpload(DEJAVU, 'DejaVuSans.ttf')
    try:
        deja = exporter.fonts.upload(upload)
    finally:
        upload.stream.close()
    # 拉丁字体在回退链首位时负责拉丁段，CJK 回退到 Noto，形成真实分段
    report = exporter.text_layer_preview(
        document['id'], {'font_ids': [deja['id'], BUNDLED_FONT_ID]})
    placements = report['pages'][0]['placements']
    assert len(placements) >= 2
    texts = ''.join(item['text'] for item in placements)
    assert texts == '中英混排PaperFlow宽度'
    cjk = next(item for item in placements if any('一' <= ch <= '鿿' for ch in item['text']))
    latin = next(item for item in placements if 'PaperFlow' in item['text'])
    cjk_unit = (cjk['rect_pdf'][2] - cjk['rect_pdf'][0]) / len(cjk['text'])
    latin_unit = (latin['rect_pdf'][2] - latin['rect_pdf'][0]) / len(latin['text'])
    assert latin_unit < cjk_unit
    # 各段首尾相接，按真实 advance 连续排布而非重叠
    for earlier, later in zip(placements, placements[1:]):
        assert later['rect_pdf'][0] >= earlier['rect_pdf'][2] - 0.01


def test_multi_page_document_level_gid_mapping(workspace):
    _, store, documents, exporter, collection_id = workspace
    document = store.create_document(collection_id, '多页')
    expected = []
    for index in range(3):
        boxes = [word(f'w{index}', f'第{index + 1}页䶮𬱖 PaperFlow{index}',
                      60, 80, 420, 40)]
        page = add_page(documents, store, document['id'], boxes)
        expected.append(page['ocr']['text'])
    record = exporter.export(document['id'], 'searchable_pdf')
    pdf = fitz.open(store.resolve(record['path']))
    try:
        assert len(pdf) == 3
        for index, text in enumerate(expected):
            assert flat(pdf[index].get_text()) == flat(text)
            assert pdf[index].search_for(f'PaperFlow{index}')
            assert len(pdf[index].get_images()) == 1
    finally:
        pdf.close()


def test_export_verify_detects_exact_text(workspace):
    _, store, documents, exporter, collection_id = workspace
    from paperflow.preflight import Preflight
    document = store.create_document(collection_id, '校验')
    boxes = [word('w1', '原样复制 Verified 2026 䶮', 60, 80, 400, 40)]
    add_page(documents, store, document['id'], boxes)
    record = exporter.export(document['id'], 'searchable_pdf')
    verification = Preflight(store, exporter).export(record['id'])
    assert verification['valid']
    assert verification['details']['pages'][0]['character_coverage'] == 1.0
    assert verification['details']['pages'][0]['embedded_fonts']


def test_text_override_manual_text_roundtrip(workspace):
    _, store, documents, exporter, collection_id = workspace
    document = store.create_document(collection_id, '整段校订')
    image = np.full((1200, 900, 3), 255, dtype=np.uint8)
    page = documents.add_image(document['id'], image, '页面')
    ocr = make_ocr([], 900, 1200)
    ocr['text'] = '人工修改已保存 PaperFlow Verified 2026\n含生僻字䶮𬱖'
    ocr['text_override'] = True
    ocr['edited'] = True
    with store.transaction() as state:
        state['pages'][page['id']]['ocr'] = ocr
    record = exporter.export(document['id'], 'searchable_pdf')
    pdf = fitz.open(store.resolve(record['path']))
    try:
        assert flat(pdf[0].get_text()) == flat(ocr['text'])
        assert pdf[0].search_for('Verified')
    finally:
        pdf.close()


def test_font_upload_list_coverage_and_delete(workspace):
    app, store, _, exporter, _ = workspace
    client = app.test_client()
    with open(DEJAVU, 'rb') as handle:
        response = client.post('/api/fonts',
                               data={'file': (handle, 'DejaVuSans.ttf')},
                               headers=HEADERS)
    assert response.status_code == 201
    font_id = response.json['id']
    listing = client.get('/api/fonts', headers=HEADERS).json
    assert {item['id'] for item in listing} >= {BUNDLED_FONT_ID, font_id}
    # 覆盖检查接口
    response = client.post('/api/fonts/coverage', headers=HEADERS, json={
        'text': 'b̂', 'font_ids': [BUNDLED_FONT_ID, font_id]})
    assert response.json['complete']
    # 内置字体受保护不可删除
    blocked = client.delete('/api/fonts/' + BUNDLED_FONT_ID, headers=HEADERS)
    assert blocked.status_code == 409
    deleted = client.delete('/api/fonts/' + font_id, headers=HEADERS)
    assert deleted.status_code == 200
    assert not (store.root / 'fonts').glob('*') or all(
        path.suffix != '.ttf' for path in (store.root / 'fonts').iterdir())


def test_preview_image_and_preflight_routes(workspace):
    app, store, documents, exporter, collection_id = workspace
    document = store.create_document(collection_id, '预览路由')
    boxes = [word('w1', '预览文字䶮', 60, 80, 300, 40)]
    add_page(documents, store, document['id'], boxes)
    client = app.test_client()
    preview = client.get(f'/api/documents/{document["id"]}/text-layer/preview',
                         headers=HEADERS)
    assert preview.status_code == 200
    assert preview.json['complete']
    image = client.get(f'/api/documents/{document["id"]}/text-layer/preview-image',
                       headers=HEADERS)
    assert image.status_code == 200 and image.mimetype == 'image/png'
    Image.open(io.BytesIO(image.data)).verify()
    preflight = client.get(f'/api/documents/{document["id"]}/preflight',
                           headers=HEADERS)
    assert preflight.status_code == 200
    assert preflight.json['text_layer']['complete']


def test_corrupt_font_upload_is_rejected(workspace):
    app, _, _, _, _ = workspace
    client = app.test_client()

    class BadUpload:
        filename = 'broken.ttf'
        stream = io.BytesIO(b'this is not a real font file')

    response = client.post('/api/fonts',
                           data={'file': (BadUpload.stream, 'broken.ttf')},
                           headers=HEADERS)
    assert response.status_code == 400
    assert response.json['code'] == 'invalid_font'
    # 失败不留字体文件
    assert not list((app.extensions['store'].root / 'fonts').glob('*'))


def test_tounicode_cmap_handles_supplementary_plane():
    mapping = {20036: '文', 61773: '𬱖', 9473: '䶮', 32: ' '}
    stream = build_tounicode(mapping).decode('ascii')
    # 辅助平面使用四字节 codespace 与 UTF-16 代理对
    assert '<00010000> <FFFFFFFF>' in stream
    surrogate = '𬱖'.encode('utf-16-be').hex().upper()
    assert surrogate in stream
    # BMP 字符仍走两字节段
    assert '<4E44> <6587>' in stream


def test_roundtrip_check_reports_first_difference():
    document = fitz.open()
    page = document.new_page()
    result = verify_roundtrip(document, ['abc'])
    assert not result['ok']
    assert result['failures'][0]['first_difference']['index'] == 0
    document.close()


def test_stale_and_missing_ocr_block_searchable_export(workspace):
    _, store, documents, exporter, collection_id = workspace
    document = store.create_document(collection_id, '过期OCR')
    boxes = [word('w1', '文字', 60, 80, 200, 40)]
    page = add_page(documents, store, document['id'], boxes)
    with store.transaction() as state:
        state['pages'][page['id']]['ocr']['stale'] = True
    with pytest.raises(DomainError) as exc:
        exporter.export(document['id'], 'searchable_pdf')
    assert exc.value.status == 409
    assert not store.snapshot().get('exports')
