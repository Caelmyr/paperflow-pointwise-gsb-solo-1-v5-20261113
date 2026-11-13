from __future__ import annotations

import io
import json
import re
import zipfile

import fitz
from PIL import Image

from .core import DomainError, choice, identity, now, number, require
from .fonts import BUNDLED_FONT_ID, FontLibrary
from .textlayer import TextLayerBuilder, verify_roundtrip


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')
    return value[:100] or 'PaperFlow'


class Exporter:
    def __init__(self, store):
        self.store = store
        self.fonts = FontLibrary(store)
        self.layer_builder = TextLayerBuilder(self.fonts)

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

    def _resolve_font_ids(self, options):
        font_ids = options.get('font_ids')
        if font_ids is None:
            preference = self.store.snapshot().get('preferences', {}).get('text_layer_fonts')
            font_ids = preference or [BUNDLED_FONT_ID]
        require(isinstance(font_ids, list) and 0 < len(font_ids) <= 8, '字体回退链无效（1–8 个字体）')
        # 提前校验所有字体可加载，失败时直接报错，不产生半成品。
        for font_id in font_ids:
            self.fonts.get_record(font_id)
        return font_ids

    def pdf(self, pages, searchable=False, dpi=150, options=None):
        options = options or {}
        dpi = number(dpi, 'PDF 分辨率', 72, 600)
        font_ids = self._resolve_font_ids(options) if searchable else None
        document = fitz.open()
        plans = []
        try:
            scale = 72 / dpi
            for source in pages:
                width = source['width'] * 72 / dpi
                height = source['height'] * 72 / dpi
                page = document.new_page(width=width, height=height)
                page.insert_image(page.rect, filename=str(self.store.resolve(source['current'])))
                if searchable:
                    result = source.get('ocr')
                    require(result is not None, '可搜索 PDF 要求所有页面先完成 OCR', 409)
                    require(not result.get('stale'),
                            '页面图像已变化，请更新 OCR 后导出可搜索 PDF', 409)
                    plan = self.layer_builder.plan_page(source, font_ids, scale)
                    plans.append(plan)
            if searchable:
                unsupported = [entry for plan in plans for entry in plan.unsupported]
                if unsupported:
                    # 缺字体可靠支持的字符：明确阻断，绝不留下宣称完整的成果。
                    raise DomainError(
                        self._unsupported_message(unsupported), 422, 'glyph_unsupported')
                chain = self.fonts.resolve_chain(font_ids)
                expected_texts = []
                for source, plan in zip(pages, plans):
                    expected_texts.append(source['ocr'].get('text', ''))
                # 整份文档统一子集化嵌入，逐字真实定位并写入自定义 ToUnicode。
                self.layer_builder.write_document(document, plans, chain)
                cmap_conflicts = [entry for plan in plans for entry in plan.cmap_conflicts]
                if cmap_conflicts:
                    # GID 与码位不能一一对应时，回读文本无法保证忠实，按生成失败处理。
                    raise DomainError(
                        '文字层 ToUnicode 映射存在字形冲突，已取消导出：'
                        + json.dumps(cmap_conflicts[:20], ensure_ascii=False),
                        500, 'cmap_conflict')
                verification = verify_roundtrip(document, expected_texts)
                if not verification['ok']:
                    raise DomainError(
                        '文字层回读校验失败，已取消导出：' + json.dumps(verification['failures'],
                                                                       ensure_ascii=False),
                        500, 'text_layer_roundtrip_failed')
            document.set_metadata({
                'title': 'PaperFlow 数字化文档',
                'author': 'PaperFlow',
                'creator': 'PaperFlow local document workbench',
                'subject': '本地离线扫描文档',
            })
            return document.tobytes(garbage=4, deflate=True), plans
        finally:
            document.close()

    def _unsupported_message(self, unsupported):
        unique = {}
        for entry in unsupported:
            unique.setdefault(entry['codepoint'], entry['character'])
        samples = '、'.join(f'{ch}（{cp}）' for cp, ch in list(unique.items())[:12])
        locations = sorted({f"第 {entry['page_id']} 页文字框 {entry['word_id']}"
                            for entry in unsupported[:30]})
        detail = '；'.join(locations[:6])
        return (f'所选字体缺少 {len(unique)} 个字符的可靠字形：{samples}。'
                f'位置示例：{detail}。请在字体库补充字体或人工核对，'
                '不会以近似字替换或写入空白字形。')

    def text_layer_preview(self, document_id, options=None):
        """交付前预览：逐页给出实际写入字符、选取范围、缺字与溢出诊断。"""
        options = options or {}
        _, pages = self.select(document_id, options.get('page_ids'))
        font_ids = self._resolve_font_ids(options)
        dpi = number(options.get('dpi', 150), 'PDF 分辨率', 72, 600)
        scale = 72 / dpi
        chain = self.fonts.resolve_chain(font_ids)
        page_reports = []
        for source in pages:
            result = source.get('ocr')
            if not result:
                page_reports.append({
                    'page_id': source['id'], 'name': source['name'],
                    'error': {'code': 'missing_ocr', 'message': '此页尚未识别文字'},
                    'placements': [], 'unsupported': [], 'overflow': [],
                })
                continue
            if result.get('stale'):
                page_reports.append({
                    'page_id': source['id'], 'name': source['name'],
                    'error': {'code': 'stale_ocr', 'message': 'OCR 已过期，请重新识别后再预览'},
                    'placements': [], 'unsupported': [], 'overflow': [],
                })
                continue
            plan = self.layer_builder.plan_page(source, font_ids, scale)
            coverage = self.fonts.coverage(result.get('text', ''), font_ids)
            page_reports.append({
                'page_id': source['id'],
                'name': source['name'],
                'width': plan.width,
                'height': plan.height,
                'placements': [self._placement_payload(item, scale) for item in plan.placements],
                'unsupported': plan.unsupported,
                'overflow': plan.overflow,
                'coverage': coverage,
                'warnings': plan.warnings,
            })
        all_unsupported = [item for report in page_reports for item in report['unsupported']]
        all_overflow = [item for report in page_reports for item in report['overflow']]
        return {
            'document_id': document_id,
            'dpi': dpi,
            'scale': scale,
            'font_chain': [{'font_id': loaded.record['id'], 'font_name': loaded.record['name']}
                           for loaded in chain],
            'pages': page_reports,
            'complete': not all_unsupported,
            'unsupported_count': len(all_unsupported),
            'overflow_count': len(all_overflow),
            'needs_manual_review': bool(all_unsupported or all_overflow),
            'checked_at': now(),
        }

    @staticmethod
    def _placement_payload(placement, scale):
        x0, y0, x1, y1 = placement.rect
        return {
            'word_id': placement.word_id,
            'text': placement.text,
            'rect_pdf': [x0, y0, x1, y1],
            'rect_image': [round(x0 / scale, 2), round(y0 / scale, 2),
                           round(x1 / scale, 2), round(y1 / scale, 2)],
            'font_size': round(placement.font_size, 3),
            'orientation': placement.orientation,
            'font_id': placement.font_id,
            'font_name': placement.font_name,
            'line': placement.line,
        }

    def preview_image(self, document_id, options=None):
        """渲染带颜色标注的文字层预览图（原图坐标），返回 PNG 字节与报告。"""
        from PIL import Image as PILImage, ImageDraw
        options = options or {}
        report = self.text_layer_preview(document_id, options)
        _, pages = self.select(document_id, options.get('page_ids'))
        page_index = number(options.get('page_index', 0), '预览页码', 0, max(0, len(pages) - 1), True)
        source = pages[page_index]
        base = PILImage.open(self.store.resolve(source['current'])).convert('RGBA')
        overlay = PILImage.new('RGBA', base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        page_report = report['pages'][page_index]
        for item in page_report['placements']:
            x0, y0, x1, y1 = item['rect_image']
            color = (61, 134, 91, 90) if item['orientation'] == 'h' else (46, 108, 176, 110)
            draw.rectangle([x0, y0, x1, y1], outline=color[:3] + (220,), width=2,
                           fill=color)
        for item in page_report['overflow']:
            x, y, w, h = item['box_rect']
            draw.rectangle([x, y, x + w, y + h], outline=(210, 120, 30, 255), width=3)
        for item in page_report['unsupported']:
            x, y, w, h = item['box_rect']
            draw.rectangle([x, y, x + w, y + h], outline=(200, 40, 40, 255), width=4)
        composed = PILImage.alpha_composite(base, overlay).convert('RGB')
        buffer = io.BytesIO()
        composed.save(buffer, format='PNG')
        return buffer.getvalue(), report, page_report

    def image_archive(self, pages, image_format='png', quality=95):
        choice(image_format, '图片格式', ('png', 'jpeg', 'tiff'))
        quality = number(quality, 'JPEG 质量', 30, 100, True)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for index, page in enumerate(pages):
                with Image.open(self.store.resolve(page['current'])) as image:
                    stream = io.BytesIO()
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
        text_layer_report = None
        chain = self.fonts.resolve_chain(self._resolve_font_ids(options)) if kind == 'searchable_pdf' else None
        if kind == 'txt':
            data = self.text(pages)
            suffix, mime = '.txt', 'text/plain; charset=utf-8'
        elif kind in ('pdf', 'searchable_pdf'):
            data, plans = self.pdf(
                pages,
                searchable=kind == 'searchable_pdf',
                dpi=options.get('dpi', 150),
                options=options,
            )
            suffix, mime = '.pdf', 'application/pdf'
            if kind == 'searchable_pdf':
                text_layer_report = {
                    'overflow': [entry for plan in plans for entry in plan.overflow],
                    'unsupported': [],
                    'cmap_conflicts': [entry for plan in plans
                                       for entry in plan.cmap_conflicts],
                    'font_chain': [
                        {'font_id': loaded.record['id'], 'font_name': loaded.record['name']}
                        for loaded in chain
                    ],
                }
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
        if text_layer_report is not None:
            record['text_layer'] = text_layer_report
        with self.store.transaction() as state:
            state.setdefault('exports', {})[record['id']] = record
            self.store.event(state, '导出文档', document_id,
                             {'export_id': record['id'], 'kind': kind,
                              'manual_review': len(text_layer_report['overflow']) if text_layer_report else 0})
        return record
