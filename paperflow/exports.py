from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import fitz
from PIL import Image

from .core import DomainError, choice, identity, now, number, require


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')
    return value[:100] or 'PaperFlow'


class Exporter:
    def __init__(self, store):
        self.store = store

    def select(self, document_id, page_ids=None):
        state = self.store.snapshot()
        document = state['documents'].get(document_id)
        require(document is not None, '文档不存在', 404)
        if page_ids is None:
            page_ids = document['page_ids']
        require(isinstance(page_ids, list) and len(page_ids) > 0, '没有可导出的页面')
        require(len(page_ids) == len(set(page_ids)), '导出页面重复')
        require(set(page_ids).issubset(document['page_ids']), '导出页面不属于该文档')
        pages = [state['pages'][page_id] for page_id in page_ids]
        return document, pages

    def text(self, pages):
        chunks = []
        for index, page in enumerate(pages):
            chunks.append(f'===== 第 {index + 1} 页 · {page["name"]} =====\n')
            chunks.append(page['ocr']['text'] if page.get('ocr') else '[此页尚未识别]')
            chunks.append('\n\n')
        return ''.join(chunks).encode('utf-8-sig')

    def pdf(self, pages, searchable=False, dpi=150):
        dpi = number(dpi, 'PDF 分辨率', 72, 600)
        document = fitz.open()
        try:
            for source in pages:
                width = source['width'] * 72 / dpi
                height = source['height'] * 72 / dpi
                page = document.new_page(width=width, height=height)
                current = self.store.resolve(source['current'])
                mono = source.get('mono')
                if mono and mono.get('signature') and self.store.resolve(mono['path']).is_file():
                    page.insert_image(page.rect, filename=str(self.store.resolve(mono['path'])))
                else:
                    page.insert_image(page.rect, filename=str(current))
                if searchable:
                    result = source.get('ocr')
                    require(result is not None, '可搜索 PDF 要求所有页面先完成 OCR', 409)
                    require(not result.get('stale'), '页面图像已变化，请更新 OCR 后导出可搜索 PDF', 409)
                    self.insert_text(page, source, dpi)
            document.set_metadata({
                'title': 'PaperFlow 数字化文档',
                'author': 'PaperFlow',
                'creator': 'PaperFlow local document workbench',
                'subject': '本地离线扫描文档',
            })
            return document.tobytes(garbage=4, deflate=True)
        finally:
            document.close()

    def insert_text(self, pdf_page, source, dpi):
        result = source['ocr']
        scale = 72 / dpi
        if result.get('text_override'):
            lines = result['text'].splitlines() or ['']
            box = fitz.Rect(12, 12, pdf_page.rect.width - 12, pdf_page.rect.height - 12)
            font_size = min(11, max(3, box.height / (len(lines) * 1.5)))
            text = '\n'.join(lines)
            for _ in range(12):
                used = pdf_page.insert_textbox(box, text, fontname='china-s', fontsize=font_size, render_mode=3)
                if used >= 0:
                    return
                font_size *= 0.85
            raise DomainError('人工文本过长，无法完整写入此页文字层；请减少文本或编辑文字框')
        for word in result['boxes']:
            text = word['text']
            if not text:
                continue
            x = word['x'] * scale
            y = word['y'] * scale
            width = max(word['width'] * scale, 1)
            height = max(word['height'] * scale, 1)
            font_size = max(1, height * 0.83)
            estimated = fitz.get_text_length(text, fontname='china-s', fontsize=font_size)
            if estimated > width:
                font_size *= width / estimated
            pdf_page.insert_text((x, y + height * 0.85), text, fontname='china-s', fontsize=font_size, render_mode=3)

    def image_archive(self, pages, image_format='png', quality=95):
        choice(image_format, '图片格式', ('png', 'jpeg', 'tiff'))
        quality = number(quality, 'JPEG 质量', 30, 100, True)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for index, page in enumerate(pages):
                mono = page.get('mono')
                source_path = self.store.resolve(mono['path']) if mono and mono.get('signature') and self.store.resolve(mono['path']).is_file() else self.store.resolve(page['current'])
                with Image.open(source_path) as image:
                    stream = io.BytesIO()
                    two_level = bool(mono and mono.get('signature'))
                    if two_level:
                        # 已确认的黑白页保持 1 位导出，不重采样成灰度。
                        image = image.convert('1')
                        if image_format == 'png':
                            image.save(stream, format='PNG')
                        elif image_format == 'tiff':
                            image.save(stream, format='TIFF', compression='group4')
                        else:
                            image.convert('L').save(stream, format='JPEG', quality=quality)
                    else:
                        image = image.convert('RGB')
                        image.save(stream, format=image_format.upper(), quality=quality)
                    extension = 'jpg' if image_format == 'jpeg' else image_format
                    archive.writestr(f'{index + 1:04d}_{safe_name(page["name"])}.{extension}', stream.getvalue())
        return buffer.getvalue()

    def metadata_archive(self, document, pages, include_originals=True):
        buffer = io.BytesIO()
        metadata = {
            'format': 'paperflow-archive',
            'schema': 1,
            'exported_at': now(),
            'document': document,
            'pages': pages,
            'order': [page['id'] for page in pages],
            'sources': {},
        }
        state = self.store.snapshot()
        source_ids = {page.get('source_file_id') for page in pages}
        metadata['sources'] = {key: value for key, value in state.get('sources', {}).items() if key in source_ids}
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            paths = {source['path'] for source in metadata['sources'].values()}
            for page in pages:
                paths.update((page['current'], page['thumbnail']))
                if page.get('mono') and page['mono'].get('path'):
                    paths.add(page['mono']['path'])
                if page.get('mono') and page['mono'].get('source'):
                    paths.add(page['mono']['source'])
                if include_originals:
                    paths.add(page['original'])
                    for entry in page['history']:
                        paths.update((entry['before'], entry['after']))
                paths.update(region['path'] for region in page.get('regions', []))
            for relative in sorted(paths):
                path = self.store.resolve(relative)
                require(path.is_file(), '文件缺失：' + relative, 500)
                archive.write(path, relative)
            archive.writestr('metadata.json', json.dumps(metadata, ensure_ascii=False, indent=2))
            archive.writestr('recognized.txt', self.text(pages))
            archive.writestr('README.txt', 'PaperFlow 归档\nmetadata.json 中所有二进制文件引用均相对此 ZIP 根目录。\n文档页面顺序见 order；原图与处理历史单独保留。\n')
        return buffer.getvalue()

    def export(self, document_id, kind, options=None):
        options = options or {}
        choice(kind, '导出类型', ('images', 'txt', 'pdf', 'searchable_pdf', 'zip'))
        document, pages = self.select(document_id, options.get('page_ids'))
        suffix = '.zip'
        mime = 'application/zip'
        if kind == 'txt':
            data = self.text(pages)
            suffix, mime = '.txt', 'text/plain; charset=utf-8'
        elif kind in ('pdf', 'searchable_pdf'):
            data = self.pdf(pages, searchable=kind == 'searchable_pdf', dpi=options.get('dpi', 150))
            suffix, mime = '.pdf', 'application/pdf'
        elif kind == 'images':
            data = self.image_archive(pages, options.get('image_format', 'png'), options.get('quality', 95))
        else:
            data = self.metadata_archive(document, pages, options.get('include_originals', True))
        path = self.store.new_file('exports', suffix)
        path.write_bytes(data)
        record = {
            'id': identity('export'),
            'document_id': document_id,
            'kind': kind,
            'path': self.store.relative(path),
            'filename': safe_name(document['name']) + ('_可搜索' if kind == 'searchable_pdf' else '') + suffix,
            'mime': mime,
            'bytes': len(data),
            'page_ids': [page['id'] for page in pages],
            'created_at': now(),
        }
        with self.store.transaction() as state:
            state.setdefault('exports', {})[record['id']] = record
            self.store.event(state, '导出文档', document_id, {'export_id': record['id'], 'kind': kind})
        return record
