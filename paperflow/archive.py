from __future__ import annotations

import copy
import json
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .core import DomainError, atomic_json, digest, identity, now, require, string
from .imaging import read_image, thumbnail, validate_image

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 5000


def safe_member(name):
    require(isinstance(name, str) and len(name) <= 400, '压缩包文件名无效')
    require('\\' not in name and '\x00' not in name and ':' not in name, '压缩包包含危险文件名')
    path = PurePosixPath(name)
    require(not path.is_absolute() and '..' not in path.parts, '压缩包包含越界路径')
    require(all(part not in ('', '.') for part in path.parts), '压缩包包含空路径')
    return path.as_posix()


def inspect_zip(path):
    require(Path(path).stat().st_size <= MAX_ARCHIVE_BYTES, '归档包不得超过512MB', 413)
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as error:
        raise DomainError('ZIP 文件损坏：' + str(error)) from error
    try:
        members = archive.infolist()
        require(len(members) <= MAX_ARCHIVE_MEMBERS, '归档文件数过多')
        size = 0
        names = set()
        for member in members:
            name = safe_member(member.filename)
            require(name not in names, '归档包含重名文件')
            names.add(name)
            require(not member.flag_bits & 1, '不支持加密归档')
            mode = member.external_attr >> 16
            require(mode & 0o170000 != 0o120000, '归档不得包含符号链接')
            size += member.file_size
            require(size <= MAX_ARCHIVE_BYTES, '归档解压总大小超过512MB')
            if member.file_size > 10 * 1024 * 1024:
                require(member.file_size / max(1, member.compress_size) < 1000, '归档压缩比异常')
        require('metadata.json' in names, '归档缺少 metadata.json')
        metadata_info = archive.getinfo('metadata.json')
        require(metadata_info.file_size < 20 * 1024 * 1024, '归档元数据过大')
        try:
            metadata = json.loads(archive.read('metadata.json').decode('utf-8'))
        except (ValueError, UnicodeError) as error:
            raise DomainError('归档元数据不是有效 JSON') from error
        require(isinstance(metadata, dict), '归档元数据必须为对象')
        return {'metadata': metadata, 'members': names, 'uncompressed_bytes': size}
    finally:
        archive.close()


def validate_document_metadata(metadata, members):
    require(metadata.get('format') == 'paperflow-archive' and metadata.get('schema') == 1, '不是受支持的 PaperFlow 归档')
    document = metadata.get('document')
    pages = metadata.get('pages')
    order = metadata.get('order')
    require(isinstance(document, dict), '归档缺少文档记录')
    string(document.get('name'), '归档文档名', 160)
    require(isinstance(pages, list) and 0 < len(pages) <= 500, '归档页面数量无效')
    require(isinstance(order, list) and len(order) == len(pages), '归档页面顺序无效')
    page_ids = [page.get('id') for page in pages if isinstance(page, dict)]
    require(len(page_ids) == len(pages) and len(set(page_ids)) == len(page_ids), '归档页面标识重复或无效')
    require(set(order) == set(page_ids), '归档页面顺序不完整')
    paths = set()
    for page in pages:
        string(page.get('name'), '页面名称', 200)
        for field in ('original', 'current', 'thumbnail'):
            relative = safe_member(page.get(field))
            require(relative in members, '归档缺少图像：' + relative)
            paths.add(relative)
        history = page.get('history', [])
        require(isinstance(history, list) and len(history) <= 2000, '处理历史无效')
        for entry in history:
            require(isinstance(entry, dict), '处理历史条目无效')
            for field in ('before', 'after'):
                relative = safe_member(entry.get(field))
                require(relative in members, '归档缺少历史图像：' + relative)
                paths.add(relative)
        regions = page.get('regions', [])
        require(isinstance(regions, list) and len(regions) <= 1000, '区域列表无效')
        for region in regions:
            relative = safe_member(region.get('path'))
            require(relative in members, '归档缺少区域图像')
            paths.add(relative)
        result = page.get('ocr')
        if result:
            require(isinstance(result, dict), '识别结果无效')
            string(result.get('text', ''), '识别文本', 500000, True)
            require(isinstance(result.get('boxes'), list) and len(result['boxes']) <= 20000, '识别框无效')
        mono = page.get('mono')
        if mono:
            require(isinstance(mono, dict), '黑白结果记录无效')
            if mono.get('path'):
                relative = safe_member(mono['path'])
                require(relative in members, '归档缺少已确认黑白文件')
                paths.add(relative)
            if mono.get('source'):
                relative = safe_member(mono['source'])
                require(relative in members, '归档缺少黑白连续源稿')
                paths.add(relative)
    for source in metadata.get('sources', {}).values():
        relative = safe_member(source.get('path'))
        require(relative in members, '归档缺少上传源文件')
        require(isinstance(source.get('sha256'), str) and len(source['sha256']) == 64, '源文件校验和无效')
        paths.add(relative)
    return paths


class Archives:
    def __init__(self, store, documents):
        self.store = store
        self.documents = documents

    def preview(self, path):
        inspection = inspect_zip(path)
        metadata = inspection['metadata']
        paths = validate_document_metadata(metadata, inspection['members'])
        return {
            'name': metadata['document']['name'],
            'category': metadata['document'].get('category', '归档导入'),
            'page_count': len(metadata['pages']),
            'ocr_pages': sum(bool(page.get('ocr')) for page in metadata['pages']),
            'file_count': len(paths),
            'uncompressed_bytes': inspection['uncompressed_bytes'],
            'exported_at': metadata.get('exported_at'),
        }

    def import_document(self, path, collection_id, name=None):
        self.store.get('collections', collection_id)
        inspection = inspect_zip(path)
        metadata = inspection['metadata']
        paths = validate_document_metadata(metadata, inspection['members'])
        new_document_id = identity('document')
        name = string(name or metadata['document']['name'], '导入文档名称', 160)
        staged = {}
        created_files = []
        try:
            with tempfile.TemporaryDirectory(prefix='paperflow-archive-') as temporary:
                temporary = Path(temporary)
                with zipfile.ZipFile(path) as archive:
                    for relative in sorted(paths):
                        extracted = temporary / (identity('file') + Path(relative).suffix)
                        with archive.open(relative) as source, extracted.open('wb') as target:
                            shutil.copyfileobj(source, target, 1024 * 1024)
                        category = 'originals'
                        if relative.startswith('sources/'):
                            category = 'sources'
                            from .documents import ALLOWED_EXTENSIONS
                            require(extracted.suffix.lower() in ALLOWED_EXTENSIONS, '源文件格式无效')
                        else:
                            image = read_image(extracted)
                            validate_image(image)
                        if relative.startswith('processed/'):
                            category = 'processed'
                        elif relative.startswith('regions/'):
                            category = 'regions'
                        elif relative.startswith('thumbs/'):
                            category = 'thumbs'
                        destination = self.store.new_file(category, Path(relative).suffix.lower())
                        shutil.copyfile(extracted, destination)
                        created_files.append(destination)
                        staged[relative] = self.store.relative(destination)
                source_mapping = {}
                imported_sources = {}
                for old_id, source in metadata.get('sources', {}).items():
                    source = copy.deepcopy(source)
                    source['id'] = identity('source')
                    source_mapping[old_id] = source['id']
                    source['path'] = staged[source['path']]
                    require(digest(self.store.resolve(source['path'])) == source['sha256'], '上传源文件校验失败')
                    imported_sources[source['id']] = source
                page_mapping = {}
                imported_pages = {}
                for source in metadata['pages']:
                    page = copy.deepcopy(source)
                    old_id = page['id']
                    page['id'] = identity('page')
                    page_mapping[old_id] = page['id']
                    page['document_id'] = new_document_id
                    if page.get('source_file_id') in source_mapping:
                        page['source_file_id'] = source_mapping[page['source_file_id']]
                    else:
                        page.pop('source_file_id', None)
                    page['original'] = staged[page['original']]
                    page['current'] = staged[page['current']]
                    page['thumbnail'] = staged[page['thumbnail']]
                    page['created_at'] = now()
                    page['updated_at'] = now()
                    original_image = read_image(self.store.resolve(page['current']))
                    page['width'] = original_image.shape[1]
                    page['height'] = original_image.shape[0]
                    page['sha256'] = digest(self.store.resolve(page['original']))
                    for entry in page['history']:
                        entry['id'] = identity('history')
                        entry['before'] = staged[entry['before']]
                        entry['after'] = staged[entry['after']]
                    for region in page.get('regions', []):
                        region['id'] = identity('region')
                        region['path'] = staged[region['path']]
                    mono = page.get('mono')
                    if mono:
                        if mono.get('path') and mono['path'] in staged:
                            mono['path'] = staged[mono['path']]
                        if mono.get('source') and mono['source'] in staged:
                            mono['source'] = staged[mono['source']]
                    imported_pages[page['id']] = page
                document = {
                    'id': new_document_id,
                    'collection_id': collection_id,
                    'name': name,
                    'category': string(metadata['document'].get('category', '归档导入'), '分类', 80),
                    'page_ids': [page_mapping[page_id] for page_id in metadata['order']],
                    'created_at': now(),
                    'updated_at': now(),
                }
                with self.store.transaction() as state:
                    require(collection_id in state['collections'], '集合已被删除', 409)
                    state['documents'][new_document_id] = document
                    state['pages'].update(imported_pages)
                    state.setdefault('sources', {}).update(imported_sources)
                    self.store.event(state, '导入归档文档', new_document_id, {'pages': len(imported_pages)})
                return self.documents.document(new_document_id)
        except Exception:
            for destination in created_files:
                destination.unlink(missing_ok=True)
            raise

    def backup(self):
        with self.store.lock:
            snapshot = self.store.snapshot()
            snapshot['exports'] = {}
            snapshot['backups'] = {}
            destination = self.store.new_file('exports', '.zip')
            files = set()
            for page in snapshot['pages'].values():
                files.update((page['original'], page['current'], page['thumbnail']))
                for history in page['history']:
                    files.update((history['before'], history['after']))
                for region in page.get('regions', []):
                    files.add(region['path'])
                mono = page.get('mono')
                if mono:
                    if mono.get('path'):
                        files.add(mono['path'])
                    if mono.get('source'):
                        files.add(mono['source'])
            for source in snapshot.get('sources', {}).values():
                files.add(source['path'])
            for checkpoint in snapshot.get('checkpoints', {}).values():
                for page in checkpoint['pages'].values():
                    files.update((page['original'], page['current'], page['thumbnail']))
                    for entry in page['history']:
                        files.update((entry['before'], entry['after']))
                    for region in page.get('regions', []):
                        files.add(region['path'])
            manifest = {}
            with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('workspace.json', json.dumps(snapshot, ensure_ascii=False, indent=2))
                for relative in sorted(files):
                    path = self.store.resolve(relative)
                    require(path.is_file(), '备份缺少文件：' + relative, 500)
                    archive.write(path, relative)
                    manifest[relative] = {'sha256': digest(path), 'bytes': path.stat().st_size}
                archive.writestr('backup-manifest.json', json.dumps({
                    'format': 'paperflow-workspace-backup',
                    'schema': 1,
                    'created_at': now(),
                    'revision': snapshot['revision'],
                    'files': manifest,
                }, ensure_ascii=False, indent=2))
        record = {
            'id': identity('backup'),
            'path': self.store.relative(destination),
            'bytes': destination.stat().st_size,
            'revision': snapshot['revision'],
            'file_count': len(files),
            'created_at': now(),
        }
        with self.store.transaction() as state:
            state.setdefault('backups', {})[record['id']] = record
            self.store.event(state, '创建工作区备份', record['id'])
        return record

    def verify_backup(self, backup_id):
        record = self.store.snapshot().get('backups', {}).get(backup_id)
        require(record is not None, '备份不存在', 404)
        path = self.store.resolve(record['path'])
        errors = []
        verified = 0
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read('backup-manifest.json'))
            require(manifest.get('format') == 'paperflow-workspace-backup', '备份格式错误')
            import hashlib
            for relative, expected in manifest['files'].items():
                safe_member(relative)
                try:
                    payload = archive.read(relative)
                    actual = hashlib.sha256(payload).hexdigest()
                    if actual != expected['sha256'] or len(payload) != expected['bytes']:
                        errors.append({'path': relative, 'error': '校验和或大小不一致'})
                    else:
                        verified += 1
                except (KeyError, zipfile.BadZipFile) as error:
                    errors.append({'path': relative, 'error': str(error)})
        return {'backup_id': backup_id, 'verified': verified, 'errors': errors, 'valid': not errors}
