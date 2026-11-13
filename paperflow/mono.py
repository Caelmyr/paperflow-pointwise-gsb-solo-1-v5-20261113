"""黑白分区渲染的服务层：预览、确认与已确认结果的读取。

预览与确认都从连续稿（彩色/灰度）重新渲染，不基于上一份黑白结果；
确认后页面当前图替换为 1 位 PNG，并在页面记录中保留源稿引用、参数、
分区纠正、区域报告与签名，供局部提取、下载和 PDF 使用同一份结果。
"""
from __future__ import annotations

import copy
from pathlib import Path

import cv2
import numpy as np

from . import imaging, monochrome
from .core import DomainError, digest, identity, now, require


def _list_zones(values):
    require(isinstance(values, dict), '黑白参数必须为对象')
    zones_value = values.get('zones')
    if zones_value is None:
        nested = values.get('settings')
        if isinstance(nested, dict):
            zones_value = nested.get('zones')
    return zones_value


def mono_settings(values):
    """把 Operation 校验后的参数（含 zones）整理为渲染设置。"""
    require(isinstance(values, dict), '黑白参数必须为对象')
    values = dict(values)
    nested = values.pop('settings', None)
    if isinstance(nested, dict):
        values = {**nested, **values}
    values.pop('zones', None)
    values.pop('preview_id', None)
    require(isinstance(values.get('scale', 1), (int, float)), '输出比例必须为数值')
    return values


def is_binary(image):
    return monochrome.is_binary(image)


def is_active(page):
    return bool(page.get('mono')) and page['mono'].get('signature')


def save_binary_crop(path, image):
    values = np.unique(image)
    require(bool(np.all((values < 8) | (values > 247))), '黑白结果不是两色，拒绝导出灰度冒充', 500, 'not_two_level')
    ink = monochrome.gray(image) < 128
    Path(path).write_bytes(monochrome.encode_png(ink))


class Monochrome:
    def __init__(self, store, documents):
        self.store = store
        self.documents = documents

    def _source(self, page):
        """返回用于渲染的连续稿路径与来源说明。"""
        mono = page.get('mono')
        if mono and mono.get('source'):
            path = self.store.resolve(mono['source'])
            require(path.is_file(), '黑白结果的连续源稿已缺失，请恢复原图后重新预览', 409, 'mono_source_missing')
            return path, 'stored'
        path = self.store.resolve(page['current'])
        image = imaging.read_image(path)
        if monochrome.is_binary(image):
            raise DomainError('当前页已是黑白结果，不能重复二值化；请恢复原图或选择连续稿来源', 409, 'already_binary')
        return path, 'current'

    def _render(self, page, values):
        path, origin = self._source(page)
        with self.documents.page_lock(page['id']):
            image = imaging.read_image(path)
            config = monochrome.settings(mono_settings(values))
            zones_used = monochrome.zones(_list_zones(values), image.shape[1], image.shape[0])
            ink, report = monochrome.render(image, config, zones_used)
        signature = monochrome.signature(digest(path), config, zones_used, report)
        return {
            'ink': ink,
            'report': report,
            'signature': signature,
            'settings': config,
            'zones': zones_used,
            'source': self.store.relative(path),
            'source_origin': origin,
            'source_sha256': digest(path),
            'source_dimensions': [image.shape[1], image.shape[0]],
        }

    def preview(self, page_id, values):
        page = self.store.get('pages', page_id)
        rendered = self._render(page, values)
        path = self.store.new_file('previews', '.png')
        path.write_bytes(monochrome.encode_png(rendered['ink']))
        record = {
            'id': identity('preview'),
            'page_id': page_id,
            'path': self.store.relative(path),
            'signature': rendered['signature'],
            'settings': rendered['settings'],
            'zones': rendered['zones'],
            'source': rendered['source'],
            'source_sha256': rendered['source_sha256'],
            'source_origin': rendered['source_origin'],
            'dimensions': rendered['report']['dimensions'],
            'two_level': rendered['report']['two_level'],
            'created_at': now(),
        }
        victims = []
        with self.store.transaction() as state:
            previews = state.setdefault('mono_previews', {})
            previews[record['id']] = {**record, 'report': rendered['report']}
            owned = [item for item in previews.values() if item['page_id'] == page_id]
            owned.sort(key=lambda item: item['created_at'], reverse=True)
            for item in owned[8:]:
                previews.pop(item['id'], None)
                victims.append(item['path'])
            self.store.event(state, '生成黑白预览', page_id, {'signature': rendered['signature'][:12]})
        for relative in victims:
            self.store.resolve(relative).unlink(missing_ok=True)
        return {
            'preview_id': record['id'],
            'url': '/api/mono/previews/' + record['id'] + '/image',
            'signature': rendered['signature'],
            'report': rendered['report'],
            'settings': rendered['settings'],
            'zones': rendered['zones'],
            'source': {
                'path': rendered['source'],
                'origin': rendered['source_origin'],
                'sha256': rendered['source_sha256'],
                'dimensions': rendered['source_dimensions'],
            },
            'two_level': rendered['report']['two_level'],
            'committed': False,
        }

    def preview_meta(self, preview_id):
        record = self.store.snapshot().get('mono_previews', {}).get(preview_id)
        require(record is not None, '黑白预览不存在或已过期', 404)
        return record

    def preview_image(self, preview_id):
        record = self.preview_meta(preview_id)
        path = self.store.resolve(record['path'])
        require(path.is_file(), '黑白预览文件缺失', 404)
        return path

    def commit(self, page_id, values):
        with self.documents.page_lock(page_id):
            page = self.store.get('pages', page_id)
            rendered = self._render(page, values)
            preview_id = values.get('preview_id')
            if preview_id:
                preview = self.store.snapshot().get('mono_previews', {}).get(preview_id)
                require(preview is not None, '预览不存在或已过期，请重新预览', 409, 'preview_missing')
                require(preview['page_id'] == page_id, '预览不属于此页面', 409, 'preview_mismatch')
                require(preview['signature'] == rendered['signature'], '预览内容与当前参数或源稿不一致，请重新预览', 409, 'preview_stale')
            path = self.store.new_file('processed')
            path.write_bytes(monochrome.encode_png(rendered['ink']))
            thumb_path = self.store.new_file('thumbs', '.jpg')
            color = cv2.cvtColor(np.where(rendered['ink'], 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
            imaging.thumbnail(thumb_path, color)
            mono_state = {
                'source': rendered['source'],
                'source_sha256': rendered['source_sha256'],
                'source_revision': page['revision'],
                'source_dimensions': rendered['source_dimensions'],
                'settings': rendered['settings'],
                'zones': rendered['zones'],
                'report': rendered['report'],
                'pattern': rendered['report']['pattern'],
                'signature': rendered['signature'],
                'path': self.store.relative(path),
                'output_sha256': digest(path),
                'dimensions': rendered['report']['dimensions'],
                'committed_at': now(),
            }
            with self.store.transaction() as state:
                current = state['pages'].get(page_id)
                require(current is not None, '页面已被删除', 409)
                require(current['revision'] == page['revision'], '页面版本冲突，请刷新后重试', 409, 'revision_conflict')
                current['history'].append({
                    'id': identity('history'),
                    'time': now(),
                    'operation': 'mono',
                    'params': {'settings': rendered['settings'], 'zones': rendered['zones']},
                    'metadata': {'report': rendered['report'], 'mono': copy.deepcopy(mono_state)},
                    'before': current['current'],
                    'after': self.store.relative(path),
                    'before_width': current['width'],
                    'before_height': current['height'],
                })
                current['current'] = self.store.relative(path)
                current['thumbnail'] = self.store.relative(thumb_path)
                current['width'] = rendered['report']['dimensions'][0]
                current['height'] = rendered['report']['dimensions'][1]
                current['quality'] = imaging.quality(color)
                current['revision'] += 1
                current['updated_at'] = now()
                current['mono'] = mono_state
                if current['ocr']:
                    current['ocr']['stale'] = True
                self.store.event(state, '确认黑白结果', page_id, {
                    'signature': rendered['signature'][:12],
                    'two_level': rendered['report']['two_level'],
                    'dimensions': rendered['report']['dimensions'],
                })
            return self.store.get('pages', page_id)

    def status(self, page_id):
        page = self.store.get('pages', page_id)
        mono = page.get('mono')
        if not mono:
            return {'committed': False, 'signature': None, 'report': None}
        path = self.store.resolve(mono['path'])
        exists = path.is_file()
        return {
            'committed': True,
            'signature': mono['signature'],
            'report': mono['report'],
            'settings': mono['settings'],
            'zones': mono['zones'],
            'path': mono['path'],
            'exists': exists,
            'output_sha256': mono.get('output_sha256'),
            'source': mono.get('source'),
            'source_exists': bool(mono.get('source')) and self.store.resolve(mono['source']).is_file(),
            'committed_at': mono.get('committed_at'),
            'two_level': mono['report'].get('two_level'),
        }

    def clears_on_restore(self, entry):
        """从历史恢复时同步恢复对应黑白状态，恢复到连续稿则清除。"""
        if entry.get('operation') == 'mono':
            metadata = entry.get('metadata') or {}
            if metadata.get('mono'):
                return copy.deepcopy(metadata['mono'])
        return None
