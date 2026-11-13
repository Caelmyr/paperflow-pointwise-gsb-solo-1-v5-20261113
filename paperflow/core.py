from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'


class DomainError(Exception):
    def __init__(self, message, status=400, code='invalid_request'):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def identity(prefix):
    return prefix + '_' + uuid.uuid4().hex


def require(condition, message, status=400, code='invalid_request'):
    if not condition:
        raise DomainError(message, status, code)


def string(value, field, maximum=200, empty=False):
    require(isinstance(value, str), field + '必须是文本')
    value = value.strip()
    require(empty or bool(value), field + '不能为空')
    require(len(value) <= maximum, field + '过长')
    require(not any(ord(c) < 32 and c not in '\n\t' for c in value), field + '含控制字符')
    return value


def number(value, field, low, high, integer=False):
    require(isinstance(value, (int, float)) and not isinstance(value, bool), field + '必须是数值')
    require(low <= value <= high, field + f'范围为 {low}–{high}')
    if integer:
        require(int(value) == value, field + '必须为整数')
        return int(value)
    return float(value)


def choice(value, field, options):
    require(value in options, field + '无效')
    return value


def ids(value, field='页面', maximum=500):
    require(isinstance(value, list), field + '必须是列表')
    require(0 < len(value) <= maximum, field + '数量无效')
    require(all(isinstance(v, str) and re.fullmatch(r'[a-z]+_[0-9a-f]{32}', v) for v in value), field + '标识无效')
    require(len(set(value)) == len(value), field + '不能重复')
    return value


def digest(path):
    hasher = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with open(temporary, 'x', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Store:
    def __init__(self, root=DATA):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.path = self.root / 'workspace.json'
        for directory in ('originals', 'processed', 'thumbs', 'exports', 'regions', 'incoming', 'fonts'):
            (self.root / directory).mkdir(exist_ok=True)
        if not self.path.exists():
            atomic_json(self.path, {
                'schema': 1,
                'revision': 0,
                'created_at': now(),
                'collections': {},
                'documents': {},
                'pages': {},
                'tasks': {},
                'events': [],
            })
        self.state = self.read_disk()
        self.validate(self.state)
        interrupted = False
        for task in self.state['tasks'].values():
            if task['status'] in ('queued', 'running', 'cancelling'):
                task['status'] = 'interrupted'
                task['finished_at'] = now()
                task['error'] = '服务重启，任务已中断；已完成的页面已保存，可以重新提交剩余页面。'
                interrupted = True
        if interrupted:
            atomic_json(self.path, self.state)

    def read_disk(self):
        try:
            with self.path.open(encoding='utf-8') as handle:
                return json.load(handle)
        except (OSError, ValueError) as error:
            raise RuntimeError('工作区 JSON 无法读取，未覆盖原文件：' + str(error)) from error

    def validate(self, state):
        require(state.get('schema') == 1, '不支持的数据版本', 500)
        for key in ('collections', 'documents', 'pages', 'tasks'):
            require(isinstance(state.get(key), dict), '工作区结构损坏：' + key, 500)
        for document in state['documents'].values():
            require(document['collection_id'] in state['collections'], '文档集合关联损坏', 500)
            require(len(document['page_ids']) == len(set(document['page_ids'])), '文档页面重复', 500)
            for page_id in document['page_ids']:
                require(page_id in state['pages'], '文档页面关联损坏', 500)
                require(state['pages'][page_id]['document_id'] == document['id'], '页面所属文档不一致', 500)
        for page in state['pages'].values():
            require(page['document_id'] in state['documents'], '页面缺少所属文档', 500)
            require(page['id'] in state['documents'][page['document_id']]['page_ids'], '孤立页面', 500)
            for key in ('original', 'current', 'thumbnail'):
                self.resolve(page[key])

    def resolve(self, relative):
        require(isinstance(relative, str), '文件引用无效')
        path = Path(relative)
        require(not path.is_absolute() and '..' not in path.parts and '\\' not in relative, '禁止越界文件引用')
        resolved = (self.root / path).resolve()
        require(resolved.is_relative_to(self.root.resolve()), '文件引用越界')
        return resolved

    def relative(self, path):
        return Path(path).resolve().relative_to(self.root.resolve()).as_posix()

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    @contextmanager
    def transaction(self):
        with self.lock:
            candidate = copy.deepcopy(self.state)
            yield candidate
            self.validate(candidate)
            candidate['revision'] += 1
            candidate['updated_at'] = now()
            atomic_json(self.path, candidate)
            self.state = candidate

    def get(self, kind, identifier):
        with self.lock:
            result = self.state[kind].get(identifier)
            require(result is not None, '未找到记录', 404, 'not_found')
            return copy.deepcopy(result)

    def event(self, state, action, target, detail=None):
        state['events'].append({
            'id': identity('event'),
            'time': now(),
            'action': action,
            'target': target,
            'detail': detail or {},
        })
        state['events'] = state['events'][-2000:]

    def new_file(self, category, suffix='.png'):
        return self.root / category / (uuid.uuid4().hex + suffix)

    def public(self):
        state = self.snapshot()
        collections = list(state['collections'].values())
        documents = list(state['documents'].values())
        for document in documents:
            document['page_count'] = len(document['page_ids'])
        return {
            'revision': state['revision'],
            'collections': collections,
            'documents': documents,
            'page_count': len(state['pages']),
            'task_count': sum(t['status'] in ('queued', 'running') for t in state['tasks'].values()),
        }

    def create_collection(self, name, category='未分类'):
        record = {
            'id': identity('collection'),
            'name': string(name, '集合名称', 100),
            'category': string(category, '分类', 80),
            'created_at': now(),
            'updated_at': now(),
        }
        with self.transaction() as state:
            state['collections'][record['id']] = record
            self.event(state, '创建集合', record['id'])
        return record

    def update_collection(self, identifier, values):
        self.get('collections', identifier)
        with self.transaction() as state:
            record = state['collections'][identifier]
            for field in ('name', 'category'):
                if field in values:
                    record[field] = string(values[field], field, 100)
            record['updated_at'] = now()
            self.event(state, '修改集合', identifier)
        return self.get('collections', identifier)

    def create_document(self, collection_id, name, category='未分类'):
        self.get('collections', collection_id)
        record = {
            'id': identity('document'),
            'collection_id': collection_id,
            'name': string(name, '文档名称', 160),
            'category': string(category, '分类', 80),
            'page_ids': [],
            'created_at': now(),
            'updated_at': now(),
        }
        with self.transaction() as state:
            state['documents'][record['id']] = record
            self.event(state, '创建文档', record['id'])
        return record

    def update_document(self, identifier, values):
        self.get('documents', identifier)
        with self.transaction() as state:
            record = state['documents'][identifier]
            for field in ('name', 'category'):
                if field in values:
                    record[field] = string(values[field], field, 160)
            if 'collection_id' in values:
                require(values['collection_id'] in state['collections'], '目标集合不存在', 404)
                record['collection_id'] = values['collection_id']
            record['updated_at'] = now()
            self.event(state, '修改文档', identifier)
        return self.get('documents', identifier)

    def delete_document(self, identifier):
        self.get('documents', identifier)
        with self.transaction() as state:
            record = state['documents'].pop(identifier)
            for page_id in record['page_ids']:
                state['pages'].pop(page_id)
            self.event(state, '删除文档', identifier, {'name': record['name']})

    def delete_collection(self, identifier):
        self.get('collections', identifier)
        with self.transaction() as state:
            require(not any(d['collection_id'] == identifier for d in state['documents'].values()), '请先移动或删除集合内文档', 409)
            state['collections'].pop(identifier)
            self.event(state, '删除空集合', identifier)
