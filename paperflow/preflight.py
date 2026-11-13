from __future__ import annotations

import io
import json
import re
import zipfile

import fitz
from PIL import Image

from .core import digest, now, require


def normalize_text(text):
    return re.sub(r'\s+', '', text).casefold()


class Preflight:
    def __init__(self, store, exporter):
        self.store = store
        self.exporter = exporter

    def document(self, document_id, kind='searchable_pdf', font_ids=None):
        document, pages = self.exporter.select(document_id)
        checks = []
        for index, page in enumerate(pages):
            issues = []
            for field in ('original', 'current', 'thumbnail'):
                path = self.store.resolve(page[field])
                if not path.is_file():
                    issues.append({'code': 'missing_file', 'message': field + '文件缺失'})
            if kind == 'searchable_pdf':
                result = page.get('ocr')
                if not result:
                    issues.append({'code': 'missing_ocr', 'message': '尚未识别文字'})
                elif result.get('stale'):
                    issues.append({'code': 'stale_ocr', 'message': '识别结果与图像版本不一致'})
                elif not result['text'].strip():
                    issues.append({'code': 'empty_text', 'message': '识别文本为空，此页不会有可检索内容', 'warning': True})
            current = self.store.resolve(page['current'])
            if current.is_file():
                try:
                    with Image.open(current) as image:
                        if image.size != (page['width'], page['height']):
                            issues.append({'code': 'size_mismatch', 'message': '记录尺寸与实际图像不一致'})
                        image.verify()
                except Exception as error:
                    issues.append({'code': 'corrupt_image', 'message': str(error)})
            checks.append({
                'page_id': page['id'],
                'number': index + 1,
                'issues': issues,
                'ready': not any(not issue.get('warning') for issue in issues),
            })
        text_layer = None
        if kind == 'searchable_pdf' and all(check['ready'] for check in checks):
            options = {'font_ids': font_ids} if font_ids else {}
            text_layer = self.exporter.text_layer_preview(document_id, options)
            for page_report in text_layer['pages']:
                check = next(item for item in checks if item['page_id'] == page_report['page_id'])
                for entry in page_report['unsupported']:
                    check['issues'].append({
                        'code': 'glyph_unsupported',
                        'message': f"字符 {entry['character']}（{entry['codepoint']}）缺少可靠字形",
                    })
                    check['ready'] = False
                for entry in page_report['overflow']:
                    check['issues'].append({
                        'code': 'text_overflow',
                        'warning': True,
                        'message': f"文字框 {entry['word_id']} 真实字宽超出原框，需人工核对选取范围",
                    })
        return {
            'document_id': document_id,
            'kind': kind,
            'checked_at': now(),
            'page_count': len(pages),
            'ready': all(check['ready'] for check in checks),
            'pages': checks,
            'text_layer': text_layer,
        }

    def export(self, export_id):
        state = self.store.snapshot()
        record = state.get('exports', {}).get(export_id)
        require(record is not None, '导出记录不存在', 404)
        path = self.store.resolve(record['path'])
        require(path.is_file(), '导出文件缺失', 404)
        errors = []
        warnings = []
        details = {}
        if path.stat().st_size != record['bytes']:
            errors.append('导出文件大小与记录不一致')
        if record['kind'] in ('pdf', 'searchable_pdf'):
            details = self.pdf(path, record, state, errors, warnings)
        elif record['kind'] in ('zip', 'images'):
            details = self.archive(path, record, state, errors, warnings)
        elif record['kind'] == 'txt':
            details = self.text(path, record, state, errors, warnings)
        else:
            details = {'format': record['kind'], 'bytes': path.stat().st_size}
        return {
            'export_id': export_id,
            'kind': record['kind'],
            'checked_at': now(),
            'valid': not errors,
            'errors': errors,
            'warnings': warnings,
            'sha256': digest(path),
            'details': details,
        }

    def pdf(self, path, record, state, errors, warnings):
        document = fitz.open(path)
        pages = []
        try:
            if len(document) != len(record['page_ids']):
                errors.append('PDF 页数与导出页序记录不一致')
            for index, pdf_page in enumerate(document):
                if index >= len(record['page_ids']):
                    break
                page_id = record['page_ids'][index]
                source = state['pages'].get(page_id)
                text = pdf_page.get_text()
                image_count = len(pdf_page.get_images())
                if image_count == 0:
                    errors.append(f'第{index + 1}页缺少扫描图像')
                item = {
                    'number': index + 1,
                    'page_id': page_id,
                    'width_points': round(pdf_page.rect.width, 2),
                    'height_points': round(pdf_page.rect.height, 2),
                    'images': image_count,
                    'copiable_characters': len(text),
                }
                if record['kind'] == 'searchable_pdf' and source and source.get('ocr'):
                    expected = normalize_text(source['ocr']['text'])
                    actual = normalize_text(text)
                    if expected and not actual:
                        errors.append(f'第{index + 1}页文字层为空')
                    if expected and actual:
                        # 逐字精确比对：确认后的 PDF 必须原样复制已校订文字。
                        if expected != actual:
                            difference = self._first_difference(expected, actual)
                            errors.append(
                                f'第{index + 1}页文字层与校订文本不一致：'
                                f"位置 {difference['index']}，期望 {difference.get('expected') or '（无）'}，"
                                f"实际 {difference.get('actual') or '（无）'}")
                        item['character_coverage'] = round(
                            sum(character in actual for character in expected) / len(expected), 4)
                    item['embedded_fonts'] = [
                        {'name': font[3], 'resource': font[4], 'encoding': font[5]}
                        for font in pdf_page.get_fonts(full=True)
                    ]
                    sample = source['ocr']['text'].split()
                    searchable = []
                    for word in sample:
                        if len(word) < 2:
                            continue
                        matches = pdf_page.search_for(word)
                        if matches:
                            searchable.append({'text': word, 'matches': len(matches)})
                        if len(searchable) >= 5:
                            break
                    item['search_samples'] = searchable
                if record.get('text_layer', {}).get('overflow'):
                    overflow = [entry for entry in record['text_layer']['overflow']
                                if entry['page_id'] == page_id]
                    if overflow:
                        warnings.append(
                            f'第{index + 1}页有 {len(overflow)} 处文字超出原文字框，已在导出记录中标注人工核对')
                pages.append(item)
            return {'page_count': len(document), 'pages': pages}
        finally:
            document.close()

    @staticmethod
    def _first_difference(expected, actual):
        for index, (a, b) in enumerate(zip(expected, actual)):
            if a != b:
                return {'index': index, 'expected': a, 'actual': b}
        return {'index': min(len(expected), len(actual)),
                'expected': expected[len(actual):len(actual) + 1] or None,
                'actual': actual[len(expected):len(expected) + 1] or None}

    def archive(self, path, record, state, errors, warnings):
        details = {}
        try:
            with zipfile.ZipFile(path) as archive:
                failed = archive.testzip()
                if failed:
                    errors.append('ZIP CRC 校验失败：' + failed)
                names = archive.namelist()
                details['file_count'] = len(names)
                details['uncompressed_bytes'] = sum(info.file_size for info in archive.infolist())
                if record['kind'] == 'zip':
                    if 'metadata.json' not in names:
                        errors.append('ZIP 缺少 JSON 元数据')
                        return details
                    metadata = json.loads(archive.read('metadata.json'))
                    if metadata.get('order') != record['page_ids']:
                        errors.append('ZIP 元数据页序与导出记录不一致')
                    metadata_pages = metadata.get('pages', [])
                    details['page_count'] = len(metadata_pages)
                    details['order'] = metadata.get('order')
                    for page in metadata_pages:
                        for field in ('original', 'current', 'thumbnail'):
                            if page[field] not in names:
                                warnings.append('ZIP 不包含引用文件：' + page[field])
                        if page['current'] in names:
                            with Image.open(io.BytesIO(archive.read(page['current']))) as image:
                                image.verify()
                else:
                    if len(names) != len(record['page_ids']):
                        errors.append('图片 ZIP 文件数与页数不一致')
                    expected_prefixes = [f'{index + 1:04d}_' for index in range(len(record['page_ids']))]
                    for prefix, name in zip(expected_prefixes, names):
                        if not name.startswith(prefix):
                            errors.append('图片 ZIP 命名页序不连续')
                        with Image.open(io.BytesIO(archive.read(name))) as image:
                            image.verify()
                    details['ordered_names'] = names
        except (zipfile.BadZipFile, OSError, ValueError) as error:
            errors.append('压缩包校验失败：' + str(error))
        return details

    def text(self, path, record, state, errors, warnings):
        try:
            text = path.read_text(encoding='utf-8-sig')
        except UnicodeError:
            errors.append('文本不是有效的 UTF-8')
            return {}
        markers = re.findall(r'===== 第 (\d+) 页 · (.*?) =====', text)
        if len(markers) != len(record['page_ids']):
            errors.append('文本页码分隔数量不一致')
        for index, marker in enumerate(markers):
            if int(marker[0]) != index + 1:
                errors.append('文本页码顺序不连续')
        return {'characters': len(text), 'page_markers': len(markers)}

    def export_history(self, document_id):
        state = self.store.snapshot()
        require(document_id in state['documents'], '文档不存在', 404)
        records = [record for record in state.get('exports', {}).values() if record['document_id'] == document_id]
        records.sort(key=lambda record: record['created_at'], reverse=True)
        results = []
        current_order = state['documents'][document_id]['page_ids']
        for record in records:
            path = self.store.resolve(record['path'])
            results.append({
                'id': record['id'],
                'kind': record['kind'],
                'filename': record['filename'],
                'created_at': record['created_at'],
                'bytes': record['bytes'],
                'page_count': len(record['page_ids']),
                'same_current_order': record['page_ids'] == current_order,
                'exists': path.is_file(),
            })
        return results
