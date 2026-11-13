from __future__ import annotations

import copy
import os
from collections import Counter, defaultdict
from pathlib import Path

from .core import digest, now, number, require
from .imaging import read_image


class Auditor:
    def __init__(self, store):
        self.store = store

    def referenced_paths(self, state=None):
        state = state or self.store.snapshot()
        references = defaultdict(list)
        for page in state['pages'].values():
            for field in ('original', 'current', 'thumbnail'):
                references[page[field]].append({'kind': 'page', 'id': page['id'], 'field': field})
            for entry in page.get('history', []):
                for field in ('before', 'after'):
                    references[entry[field]].append({'kind': 'history', 'id': page['id'], 'field': field})
            for region in page.get('regions', []):
                references[region['path']].append({'kind': 'region', 'id': region['id'], 'page_id': page['id']})
        for record in state.get('exports', {}).values():
            references[record['path']].append({'kind': 'export', 'id': record['id']})
        for record in state.get('backups', {}).values():
            references[record['path']].append({'kind': 'backup', 'id': record['id']})
        for source in state.get('sources', {}).values():
            references[source['path']].append({'kind': 'source', 'id': source['id']})
        for checkpoint in state.get('checkpoints', {}).values():
            for page in checkpoint['pages'].values():
                for field in ('original', 'current', 'thumbnail'):
                    references[page[field]].append({'kind': 'checkpoint', 'id': checkpoint['id']})
                for entry in page.get('history', []):
                    for field in ('before', 'after'):
                        references[entry[field]].append({'kind': 'checkpoint', 'id': checkpoint['id']})
                for region in page.get('regions', []):
                    references[region['path']].append({'kind': 'checkpoint', 'id': checkpoint['id']})
        return references

    def integrity(self, deep=False):
        state = self.store.snapshot()
        references = self.referenced_paths(state)
        issues = []
        checked_files = 0
        checked_pages = 0
        bytes_total = 0
        for relative, owners in references.items():
            try:
                path = self.store.resolve(relative)
                if not path.is_file():
                    issues.append({'code': 'missing_file', 'path': relative, 'owners': owners})
                    continue
                if path.is_symlink():
                    issues.append({'code': 'symlink_file', 'path': relative, 'owners': owners})
                    continue
                checked_files += 1
                bytes_total += path.stat().st_size
            except Exception as error:
                issues.append({'code': 'invalid_path', 'path': relative, 'message': str(error)})
        for document in state['documents'].values():
            if document['collection_id'] not in state['collections']:
                issues.append({'code': 'missing_collection', 'document_id': document['id']})
            if len(set(document['page_ids'])) != len(document['page_ids']):
                issues.append({'code': 'duplicate_page_reference', 'document_id': document['id']})
            for index, page_id in enumerate(document['page_ids']):
                page = state['pages'].get(page_id)
                if not page:
                    issues.append({'code': 'missing_page', 'document_id': document['id'], 'page_id': page_id})
                    continue
                if page['document_id'] != document['id']:
                    issues.append({'code': 'incorrect_document', 'page_id': page_id})
        for page in state['pages'].values():
            checked_pages += 1
            document = state['documents'].get(page['document_id'])
            if not document or page['id'] not in document['page_ids']:
                issues.append({'code': 'orphan_page', 'page_id': page['id']})
            result = page.get('ocr')
            if result and not result.get('stale'):
                if result['width'] != page['width'] or result['height'] != page['height']:
                    issues.append({'code': 'ocr_size_mismatch', 'page_id': page['id']})
                for box in result['boxes']:
                    if box['x'] < 0 or box['y'] < 0 or box['x'] + box['width'] > page['width'] + 2 or box['y'] + box['height'] > page['height'] + 2:
                        issues.append({'code': 'ocr_box_outside', 'page_id': page['id'], 'word_id': box['id']})
            if deep:
                try:
                    original = self.store.resolve(page['original'])
                    if original.is_file() and digest(original) != page['sha256']:
                        issues.append({'code': 'original_modified', 'page_id': page['id'], 'path': page['original']})
                    image = read_image(self.store.resolve(page['current']))
                    if image.shape[1] != page['width'] or image.shape[0] != page['height']:
                        issues.append({'code': 'image_size_mismatch', 'page_id': page['id']})
                except Exception as error:
                    issues.append({'code': 'invalid_image', 'page_id': page['id'], 'message': str(error)})
        return {
            'checked_at': now(),
            'revision': state['revision'],
            'deep': deep,
            'valid': not issues,
            'checked_files': checked_files,
            'checked_pages': checked_pages,
            'bytes': bytes_total,
            'issues': issues,
        }

    def storage(self):
        state = self.store.snapshot()
        references = self.referenced_paths(state)
        categories = {}
        total_bytes = 0
        total_files = 0
        for category in ('sources', 'originals', 'processed', 'thumbs', 'regions', 'exports', 'incoming', 'fonts'):
            directory = self.store.root / category
            files = 0
            size = 0
            unreferenced = 0
            for path in directory.iterdir():
                if not path.is_file() or path.is_symlink():
                    continue
                files += 1
                file_size = path.stat().st_size
                size += file_size
                if self.store.relative(path) not in references:
                    unreferenced += file_size
            categories[category] = {'files': files, 'bytes': size, 'unreferenced_bytes': unreferenced}
            total_bytes += size
            total_files += files
        usage = __import__('shutil').disk_usage(self.store.root)
        return {
            'categories': categories,
            'total_bytes': total_bytes,
            'total_files': total_files,
            'disk_free_bytes': usage.free,
            'disk_total_bytes': usage.total,
            'json_bytes': self.store.path.stat().st_size,
            'collections': len(state['collections']),
            'documents': len(state['documents']),
            'pages': len(state['pages']),
        }

    def duplicates(self):
        state = self.store.snapshot()
        groups = defaultdict(list)
        for page in state['pages'].values():
            groups[page['sha256']].append(page)
        results = []
        for checksum, pages in groups.items():
            if len(pages) < 2:
                continue
            results.append({
                'sha256': checksum,
                'pages': [{
                    'id': page['id'],
                    'name': page['name'],
                    'document_id': page['document_id'],
                    'document_name': state['documents'][page['document_id']]['name'],
                } for page in pages],
            })
        return results

    def orphan_files(self, minimum_age_hours=24):
        minimum_age_hours = number(minimum_age_hours, '保留小时数', 1, 87600)
        import time
        cutoff = time.time() - minimum_age_hours * 3600
        references = self.referenced_paths()
        candidates = []
        for category in ('processed', 'thumbs', 'regions', 'exports', 'incoming'):
            for path in (self.store.root / category).iterdir():
                if not path.is_file() or path.is_symlink():
                    continue
                relative = self.store.relative(path)
                if relative in references or path.stat().st_mtime > cutoff:
                    continue
                candidates.append({
                    'path': relative,
                    'bytes': path.stat().st_size,
                    'modified_at': path.stat().st_mtime,
                })
        return candidates

    def cleanup(self, values):
        paths = values.get('paths')
        require(isinstance(paths, list) and len(paths) <= 5000, '清理文件列表无效')
        require(all(isinstance(path, str) for path in paths), '清理路径必须为文本')
        require(len(paths) == len(set(paths)), '清理路径重复')
        candidates = {item['path']: item for item in self.orphan_files(values.get('minimum_age_hours', 24))}
        require(set(paths).issubset(candidates), '只能清理预览中未引用且达到保留期的文件', 409)
        deleted = []
        bytes_freed = 0
        with self.store.lock:
            references = self.referenced_paths()
            for relative in paths:
                require(relative not in references, '文件已被引用，请重新预览', 409)
            for relative in paths:
                path = self.store.resolve(relative)
                if path.is_file() and not path.is_symlink():
                    size = path.stat().st_size
                    path.unlink()
                    deleted.append(relative)
                    bytes_freed += size
            with self.store.transaction() as state:
                self.store.event(state, '清理未引用结果文件', 'workspace', {'files': len(deleted), 'bytes': bytes_freed})
        return {'deleted': deleted, 'bytes_freed': bytes_freed}

    def events(self, values):
        events = self.store.snapshot()['events']
        action = values.get('action')
        target = values.get('target')
        after = values.get('after')
        before = values.get('before')
        limit = number(values.get('limit', 100), '日志数量', 1, 2000, True)
        if action:
            events = [event for event in events if action in event['action']]
        if target:
            events = [event for event in events if target == event['target']]
        if after:
            events = [event for event in events if event['time'] >= after]
        if before:
            events = [event for event in events if event['time'] <= before]
        return list(reversed(events[-limit:]))

    def summary(self):
        state = self.store.snapshot()
        operations = Counter()
        for page in state['pages'].values():
            operations.update(entry['operation'] for entry in page['history'])
        tasks = Counter(task['status'] for task in state['tasks'].values())
        recognized = [page['ocr'] for page in state['pages'].values() if page.get('ocr')]
        exports = Counter(item['kind'] for item in state.get('exports', {}).values())
        return {
            'created_at': state['created_at'],
            'revision': state['revision'],
            'collections': len(state['collections']),
            'documents': len(state['documents']),
            'pages': len(state['pages']),
            'recognized_pages': len(recognized),
            'edited_pages': sum(bool(result.get('edited')) for result in recognized),
            'stale_pages': sum(bool(result.get('stale')) for result in recognized),
            'text_characters': sum(len(result['text']) for result in recognized),
            'operations': dict(operations),
            'tasks': dict(tasks),
            'exports': dict(exports),
        }
