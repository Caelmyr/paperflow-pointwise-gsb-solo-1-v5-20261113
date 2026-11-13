from __future__ import annotations

import copy
import io
import shutil
import threading
from pathlib import Path

import cv2
import fitz
import numpy as np
from PIL import Image, ImageOps

from . import imaging, ocr
from .mono import is_binary as binary_image, is_active as mono_active, save_binary_crop as _save_binary_crop
from .core import DomainError, digest, identity, ids, now, number, require, string

MAX_FILE_BYTES = 60 * 1024 * 1024
MAX_IMPORT_PAGES = 200
ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp', '.pdf'}


class Documents:
    def __init__(self, store):
        self.store = store
        self.locks = {}
        self.guard = threading.Lock()

    def page_lock(self, page_id):
        with self.guard:
            return self.locks.setdefault(page_id, threading.RLock())

    def document(self, document_id):
        document = self.store.get('documents', document_id)
        state = self.store.snapshot()
        document['pages'] = [state['pages'][page_id] for page_id in document['page_ids']]
        return document

    def add_image(self, document_id, image, name, source=None, position=None):
        imaging.validate_image(image)
        self.store.get('documents', document_id)
        original_path = self.store.new_file('originals')
        thumb_path = self.store.new_file('thumbs', '.jpg')
        imaging.save_image(original_path, image)
        imaging.thumbnail(thumb_path, image)
        page_id = identity('page')
        original = self.store.relative(original_path)
        record = {
            'id': page_id,
            'document_id': document_id,
            'name': string(name, '页面名称', 200),
            'original': original,
            'current': original,
            'thumbnail': self.store.relative(thumb_path),
            'source': source,
            'sha256': digest(original_path),
            'width': image.shape[1],
            'height': image.shape[0],
            'revision': 0,
            'created_at': now(),
            'updated_at': now(),
            'ocr': None,
            'ocr_versions': [],
            'history': [],
            'regions': [],
            'quality': imaging.quality(image),
        }
        try:
            with self.store.transaction() as state:
                document = state['documents'].get(document_id)
                require(document is not None, '导入期间目标文档被删除', 409)
                state['pages'][page_id] = record
                if position is None:
                    document['page_ids'].append(page_id)
                else:
                    position = number(position, '插入位置', 0, len(document['page_ids']), True)
                    document['page_ids'].insert(position, page_id)
                document['updated_at'] = now()
                self.store.event(state, '导入页面', page_id, {'name': name})
        except Exception:
            original_path.unlink(missing_ok=True)
            thumb_path.unlink(missing_ok=True)
            raise
        return self.store.get('pages', page_id)

    def save_upload(self, uploaded):
        filename = Path(uploaded.filename or '').name
        require(filename and len(filename) <= 240, '上传文件名无效')
        extension = Path(filename).suffix.lower()
        require(extension in ALLOWED_EXTENSIONS, '仅支持 PNG/JPEG/TIFF/BMP/WebP/PDF')
        destination = self.store.new_file('incoming', extension)
        size = 0
        try:
            with destination.open('xb') as output:
                while True:
                    block = uploaded.stream.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    require(size <= MAX_FILE_BYTES, '单个文件不得超过60MB', 413)
                    output.write(block)
            require(size > 0, '上传文件为空')
            return destination, filename
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def import_path(self, document_id, path, name=None, position=None, cancelled=None):
        path = Path(path)
        name = name or path.name
        require(path.stat().st_size <= MAX_FILE_BYTES, '单个文件不得超过60MB', 413)
        from .sources import Sources
        sources = Sources(self.store)
        retained = sources.retain(path, name)
        imported = []
        if path.suffix.lower() == '.pdf':
            try:
                pdf = fitz.open(path)
            except Exception as error:
                raise DomainError('PDF 无法打开：' + str(error)) from error
            try:
                require(not pdf.is_encrypted, '不支持加密 PDF')
                require(0 < len(pdf) <= MAX_IMPORT_PAGES, 'PDF 必须为1–200页')
                for index, pdf_page in enumerate(pdf):
                    if cancelled and cancelled():
                        break
                    require(pdf_page.rect.width > 0 and pdf_page.rect.height > 0, 'PDF 页面尺寸无效')
                    scale = min(2, (imaging.MAX_PIXELS / (pdf_page.rect.width * pdf_page.rect.height)) ** 0.5)
                    pixmap = pdf_page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
                    image = np.frombuffer(pixmap.samples, np.uint8).reshape(pixmap.height, pixmap.width, 3)
                    image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
                    source = {'name': name, 'type': 'pdf', 'page': index + 1}
                    insertion = None if position is None else position + len(imported)
                    record = self.add_image(document_id, image, f'{Path(name).stem} · {index + 1}', source, insertion)
                    imported.append(record['id'])
                    sources.attach(retained['id'], [record['id']])
            finally:
                pdf.close()
        elif path.suffix.lower() in ('.tif', '.tiff'):
            try:
                with Image.open(path) as tiff:
                    require(tiff.n_frames <= MAX_IMPORT_PAGES, 'TIFF 最多200帧')
                    for frame in range(tiff.n_frames):
                        if cancelled and cancelled():
                            break
                        tiff.seek(frame)
                        require(tiff.width * tiff.height <= imaging.MAX_PIXELS, 'TIFF 页面过大')
                        rgb = np.asarray(ImageOps.exif_transpose(tiff).convert('RGB'))
                        image = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                        insertion = None if position is None else position + len(imported)
                        record = self.add_image(document_id, image, f'{Path(name).stem} · {frame + 1}', {'name': name, 'type': 'tiff', 'page': frame + 1}, insertion)
                        imported.append(record['id'])
                        sources.attach(retained['id'], [record['id']])
            except (OSError, ValueError) as error:
                raise DomainError('TIFF 读取失败：' + str(error)) from error
        else:
            image = imaging.read_image(path)
            record = self.add_image(document_id, image, Path(name).stem, {'name': name, 'type': 'image'}, position)
            imported.append(record['id'])
            sources.attach(retained['id'], [record['id']])
        return imported

    def apply(self, page_id, operation, params, expected_revision=None):
        if operation == 'mono':
            from .parameters import validate_operation
            params = validate_operation(operation, params)
            if expected_revision is not None:
                page = self.store.get('pages', page_id)
                require(expected_revision == page['revision'], '页面已变化，请刷新后重试', 409, 'revision_conflict')
            from .mono import Monochrome
            settings = {key: params[key] for key in ('scale', 'cell', 'style', 'assumed_dpi', 'keep_scale') if key in params}
            return Monochrome(self.store, self).commit(page_id, {'settings': settings, 'zones': params.get('zones')})
        with self.page_lock(page_id):
            page = self.store.get('pages', page_id)
            if expected_revision is not None:
                require(expected_revision == page['revision'], '页面已变化，请刷新后重试', 409, 'revision_conflict')
            from .parameters import validate_operation
            params = validate_operation(operation, params)
            if page.get('mono') and page['mono'].get('signature'):
                raise DomainError('此页已确认黑白结果，继续处理会把旧结果再次二值化；请先恢复连续稿或从历史回看', 409, 'mono_committed')
            image = imaging.read_image(self.store.resolve(page['current']))
            result, metadata = imaging.process(image, operation, params)
            path = self.store.new_file('processed')
            thumb_path = self.store.new_file('thumbs', '.jpg')
            imaging.save_image(path, result)
            imaging.thumbnail(thumb_path, result)
            with self.store.transaction() as state:
                current = state['pages'].get(page_id)
                require(current is not None, '页面已被删除', 409)
                require(current['revision'] == page['revision'], '页面版本冲突', 409)
                current['history'].append({
                    'id': identity('history'),
                    'time': now(),
                    'operation': operation,
                    'params': params,
                    'metadata': metadata,
                    'before': current['current'],
                    'after': self.store.relative(path),
                    'before_width': current['width'],
                    'before_height': current['height'],
                })
                current['current'] = self.store.relative(path)
                current['thumbnail'] = self.store.relative(thumb_path)
                current['width'] = result.shape[1]
                current['height'] = result.shape[0]
                current['quality'] = imaging.quality(result)
                current['revision'] += 1
                current['updated_at'] = now()
                # 图像被其他操作改变后，旧的“已确认黑白结果”不再对应当前图。
                current['mono'] = None
                if current['ocr']:
                    current['ocr']['stale'] = True
                self.store.event(state, '图像处理', page_id, {'operation': operation})
            return self.store.get('pages', page_id)

    def restore(self, page_id, history_id=None):
        with self.page_lock(page_id):
            page = self.store.get('pages', page_id)
            if history_id:
                entries = [entry for entry in page['history'] if entry['id'] == history_id]
                require(bool(entries), '历史版本不存在', 404)
                target = entries[0]['after']
                mono_state = copy.deepcopy((entries[0].get('metadata') or {}).get('mono'))
            else:
                target = page['original']
                mono_state = None
            image = imaging.read_image(self.store.resolve(target))
            thumb_path = self.store.new_file('thumbs', '.jpg')
            imaging.thumbnail(thumb_path, image)
            with self.store.transaction() as state:
                current = state['pages'][page_id]
                current['history'].append({
                    'id': identity('history'),
                    'time': now(),
                    'operation': 'restore',
                    'params': {'history_id': history_id},
                    'metadata': {},
                    'before': current['current'],
                    'after': target,
                    'before_width': current['width'],
                    'before_height': current['height'],
                })
                current['current'] = target
                current['thumbnail'] = self.store.relative(thumb_path)
                current['width'] = image.shape[1]
                current['height'] = image.shape[0]
                current['quality'] = imaging.quality(image)
                current['revision'] += 1
                current['updated_at'] = now()
                # 恢复到黑白版本时同步恢复其确认状态；恢复到连续稿则清除旧的黑白记录。
                current['mono'] = mono_state
                if current['ocr']:
                    current['ocr']['stale'] = True
                self.store.event(state, '恢复图像', page_id)
            return self.store.get('pages', page_id)

    def recognize(self, page_id, params):
        with self.page_lock(page_id):
            page = self.store.get('pages', page_id)
            image = imaging.read_image(self.store.resolve(page['current']))
            result = ocr.recognize(image, params.get('language', 'chi_sim+eng'), params.get('psm', 3), params.get('minimum', 0))
            result['image_revision'] = page['revision']
            with self.store.transaction() as state:
                current = state['pages'].get(page_id)
                require(current is not None and current['revision'] == page['revision'], 'OCR 期间页面发生变化', 409)
                if current['ocr']:
                    current['ocr_versions'].append(current['ocr'])
                    current['ocr_versions'] = current['ocr_versions'][-20:]
                current['ocr'] = result
                current['updated_at'] = now()
                self.store.event(state, '文字识别', page_id, {'words': len(result['boxes']), 'confidence': result['confidence']})
            return self.store.get('pages', page_id)

    def edit_ocr(self, page_id, values):
        with self.page_lock(page_id):
            with self.store.transaction() as state:
                current = state['pages'].get(page_id)
                require(current is not None, '页面不存在', 404)
                if current['ocr']:
                    current['ocr_versions'].append(copy.deepcopy(current['ocr']))
                    current['ocr_versions'] = current['ocr_versions'][-20:]
                current['ocr'] = ocr.edit_result(current['ocr'], values)
                current['updated_at'] = now()
                self.store.event(state, '校订文字', page_id)
            return self.store.get('pages', page_id)

    def reorder(self, document_id, page_ids):
        ids(page_ids)
        with self.store.transaction() as state:
            document = state['documents'].get(document_id)
            require(document is not None, '文档不存在', 404)
            require(set(page_ids) == set(document['page_ids']), '页序必须完整包含文档当前页面', 409)
            document['page_ids'] = page_ids
            document['updated_at'] = now()
            self.store.event(state, '调整页序', document_id)
        return self.document(document_id)

    def copy_pages(self, page_ids, target_id, position=None):
        ids(page_ids)
        target = self.store.get('documents', target_id)
        if position is None:
            position = len(target['page_ids'])
        position = number(position, '插入位置', 0, len(target['page_ids']), True)
        with self.store.transaction() as state:
            require(all(page_id in state['pages'] for page_id in page_ids), '待复制页面不存在', 404)
            created = []
            for page_id in page_ids:
                page = copy.deepcopy(state['pages'][page_id])
                page['id'] = identity('page')
                page['document_id'] = target_id
                page['name'] += ' · 副本'
                page['created_at'] = now()
                page['updated_at'] = now()
                state['pages'][page['id']] = page
                created.append(page['id'])
            state['documents'][target_id]['page_ids'][position:position] = created
            state['documents'][target_id]['updated_at'] = now()
            self.store.event(state, '复制页面', target_id, {'source': page_ids, 'created': created})
        return created

    def delete_pages(self, page_ids):
        ids(page_ids)
        with self.store.transaction() as state:
            require(all(page_id in state['pages'] for page_id in page_ids), '待删除页面不存在', 404)
            for page_id in page_ids:
                page = state['pages'].pop(page_id)
                document = state['documents'][page['document_id']]
                document['page_ids'].remove(page_id)
                document['updated_at'] = now()
            self.store.event(state, '删除页面', ','.join(page_ids))

    def move_pages(self, page_ids, target_id, position=None):
        ids(page_ids)
        self.store.get('documents', target_id)
        with self.store.transaction() as state:
            require(all(page_id in state['pages'] for page_id in page_ids), '页面不存在', 404)
            target = state['documents'][target_id]
            for page_id in page_ids:
                page = state['pages'][page_id]
                source = state['documents'][page['document_id']]
                source['page_ids'].remove(page_id)
                source['updated_at'] = now()
                page['document_id'] = target_id
            if position is None:
                position = len(target['page_ids'])
            position = number(position, '插入位置', 0, len(target['page_ids']), True)
            target['page_ids'][position:position] = page_ids
            target['updated_at'] = now()
            self.store.event(state, '移动页面', target_id, {'pages': page_ids})
        return self.document(target_id)

    def split(self, document_id, page_ids, name):
        ids(page_ids)
        source = self.store.get('documents', document_id)
        require(set(page_ids).issubset(source['page_ids']), '拆分页面不属于该文档')
        name = string(name, '新文档名称', 160)
        new_id = identity('document')
        with self.store.transaction() as state:
            source = state['documents'][document_id]
            require(set(page_ids).issubset(source['page_ids']), '文档在拆分期间发生变化', 409)
            ordered = [page_id for page_id in source['page_ids'] if page_id in page_ids]
            state['documents'][new_id] = {
                'id': new_id,
                'collection_id': source['collection_id'],
                'name': name,
                'category': source['category'],
                'page_ids': ordered,
                'created_at': now(),
                'updated_at': now(),
            }
            source['page_ids'] = [page_id for page_id in source['page_ids'] if page_id not in page_ids]
            source['updated_at'] = now()
            for page_id in ordered:
                state['pages'][page_id]['document_id'] = new_id
            self.store.event(state, '拆分文档', document_id, {'created': new_id, 'pages': ordered})
        return self.document(new_id)

    def merge(self, document_ids, name, collection_id=None, consume=False):
        ids(document_ids, '文档')
        state = self.store.snapshot()
        require(all(identifier in state['documents'] for identifier in document_ids), '合并文档不存在', 404)
        first = state['documents'][document_ids[0]]
        collection_id = collection_id or first['collection_id']
        self.store.get('collections', collection_id)
        new_id = identity('document')
        name = string(name, '合并名称', 160)
        with self.store.transaction() as state:
            new_document = {
                'id': new_id,
                'collection_id': collection_id,
                'name': name,
                'category': first['category'],
                'page_ids': [],
                'created_at': now(),
                'updated_at': now(),
            }
            state['documents'][new_id] = new_document
            for document_id in document_ids:
                document = state['documents'][document_id]
                for page_id in document['page_ids']:
                    if consume:
                        page = state['pages'][page_id]
                    else:
                        page = copy.deepcopy(state['pages'][page_id])
                        page['id'] = identity('page')
                        page['created_at'] = now()
                        state['pages'][page['id']] = page
                    page['document_id'] = new_id
                    new_document['page_ids'].append(page['id'])
                if consume:
                    state['documents'].pop(document_id)
            self.store.event(state, '合并文档', new_id, {'source': document_ids, 'consume': consume})
        return self.document(new_id)

    def extract_region(self, page_id, values):
        with self.page_lock(page_id):
            page = self.store.get('pages', page_id)
            image = imaging.read_image(self.store.resolve(page['current']))
            x, y, w, h = imaging.rectangle(values.get('region'), image)
            path = self.store.new_file('regions')
            if mono_active(page) and binary_image(image):
                # 从已确认的黑白结果提取，保持 1 位 PNG，不做任何灰度重采样。
                _save_binary_crop(path, image[y:y + h, x:x + w])
            else:
                imaging.save_image(path, image[y:y + h, x:x + w])
            record = {
                'id': identity('region'),
                'name': string(values.get('name', '提取区域'), '区域名称', 100),
                'kind': values.get('kind', 'illustration'),
                'rect': [x, y, w, h],
                'path': self.store.relative(path),
                'image_revision': page['revision'],
                'created_at': now(),
            }
            if mono_active(page):
                record['mono'] = {
                    'signature': page['mono']['signature'],
                    'image_revision': page['revision'],
                    'two_level': True,
                }
            require(record['kind'] in ('illustration', 'text', 'other'), '区域类型无效')
            with self.store.transaction() as state:
                state['pages'][page_id]['regions'].append(record)
                self.store.event(state, '提取区域', page_id, {'region_id': record['id']})
            return record

    def rename_page(self, page_id, name):
        name = string(name, '页面名称', 200)
        with self.store.transaction() as state:
            require(page_id in state['pages'], '页面不存在', 404)
            state['pages'][page_id]['name'] = name
            self.store.event(state, '重命名页面', page_id)
        return self.store.get('pages', page_id)
