from __future__ import annotations

import copy

from .core import choice, now, number, require, string


class Preferences:
    def __init__(self, store):
        self.store = store

    def defaults(self):
        return {
            'default_language': 'chi_sim+eng',
            'default_psm': 3,
            'export_dpi': 150,
            'image_format': 'png',
            'jpeg_quality': 95,
            'confirm_delete': True,
            'show_ocr_boxes': True,
            'default_category': '未分类',
            'thumbnail_size': 240,
            'reviewer': '本机用户',
            'preserve_ocr_versions': 20,
            'text_layer_fonts': ['font_bundled_noto_sans_cjk_sc'],
        }

    def get(self):
        result = self.defaults()
        result.update(self.store.snapshot().get('preferences', {}))
        return result

    def update(self, values):
        require(isinstance(values, dict), '设置必须为对象')
        require(set(values).issubset(self.defaults()), '包含不支持的设置')
        result = self.get()
        for key, value in values.items():
            if key == 'default_language':
                result[key] = choice(value, '默认语言', ('chi_sim+eng', 'chi_sim', 'eng'))
            elif key == 'default_psm':
                result[key] = number(value, '版面模式', 3, 13, True)
            elif key == 'export_dpi':
                result[key] = number(value, '导出分辨率', 72, 600, True)
            elif key == 'image_format':
                result[key] = choice(value, '图片格式', ('png', 'jpeg', 'tiff'))
            elif key == 'jpeg_quality':
                result[key] = number(value, '图片质量', 30, 100, True)
            elif key in ('confirm_delete', 'show_ocr_boxes'):
                require(isinstance(value, bool), key + '必须为布尔值')
                result[key] = value
            elif key == 'default_category':
                result[key] = string(value, '默认分类', 80)
            elif key == 'thumbnail_size':
                result[key] = number(value, '缩略图边长', 120, 600, True)
            elif key == 'reviewer':
                result[key] = string(value, '审核人', 80)
            elif key == 'preserve_ocr_versions':
                result[key] = number(value, 'OCR 版本数', 1, 100, True)
            elif key == 'text_layer_fonts':
                require(isinstance(value, list) and 0 < len(value) <= 8, '文字层字体回退链必须包含1–8个字体')
                require(all(isinstance(item, str) for item in value), '字体标识无效')
                from .fonts import BUNDLED_FONT_ID
                available = {BUNDLED_FONT_ID, *self.store.snapshot().get('fonts', {})}
                require(all(item in available for item in value), '字体回退链包含不存在的字体')
                result[key] = value
        with self.store.transaction() as state:
            state['preferences'] = result
            self.store.event(state, '修改用户设置', 'workspace', {'fields': list(values)})
        return result

    def reset(self):
        with self.store.transaction() as state:
            state['preferences'] = self.defaults()
            self.store.event(state, '恢复默认设置', 'workspace')
        return self.get()

    def save_session(self, values):
        permitted = {'document_id', 'page_id', 'panel', 'zoom', 'selected_pages', 'view'}
        require(isinstance(values, dict) and set(values).issubset(permitted), '会话字段无效')
        with self.store.transaction() as state:
            session = state.setdefault('session', {})
            document_id = values.get('document_id', session.get('document_id'))
            if document_id:
                require(document_id in state['documents'], '会话文档不存在', 404)
                session['document_id'] = document_id
            if 'page_id' in values:
                page_id = values['page_id']
                require(page_id in state['pages'], '会话页面不存在', 404)
                require(state['pages'][page_id]['document_id'] == document_id, '会话页面不属于文档')
                session['page_id'] = page_id
            if 'panel' in values:
                session['panel'] = choice(values['panel'], '面板', ('tools', 'ocr', 'history'))
            if 'view' in values:
                session['view'] = choice(values['view'], '预览', ('current', 'original', 'compare'))
            if 'zoom' in values:
                session['zoom'] = number(values['zoom'], '缩放', 0.3, 2)
            if 'selected_pages' in values:
                selected = values['selected_pages']
                require(isinstance(selected, list) and len(selected) <= 500, '选中页面无效')
                require(document_id in state['documents'], '请先选择文档')
                require(set(selected).issubset(state['documents'][document_id]['page_ids']), '选中页面已变化')
                session['selected_pages'] = selected
            session['updated_at'] = now()
            result = copy.deepcopy(session)
        return result

    def session(self):
        state = self.store.snapshot()
        session = state.get('session', {})
        document_id = session.get('document_id')
        if document_id not in state['documents']:
            return {}
        pages = state['documents'][document_id]['page_ids']
        if session.get('page_id') not in pages:
            session.pop('page_id', None)
        session['selected_pages'] = [page_id for page_id in session.get('selected_pages', []) if page_id in pages]
        return session
