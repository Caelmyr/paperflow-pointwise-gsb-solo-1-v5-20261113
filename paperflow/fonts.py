"""项目字体库与字形覆盖检查。

设计原则：
- 字体随项目携带：内置 Noto Sans CJK SC（从随项目 TTC 中提取的 SC 子字体），
  用户上传的字体保存在工作区 fonts/ 目录并在 workspace.json 登记。
- 不做近似字替换：任何字符在整条回退链中都找不到字形时，如实报告为
  unsupported，由调用方决定阻断导出或标注人工核对。
- 覆盖判断基于字体 cmap（has_glyph），组合附加符号按独立码位检查。
"""
from __future__ import annotations

import io
import threading
from dataclasses import dataclass, field

from .core import ROOT, DomainError, identity, now, require

BUNDLED_TTC = ROOT / 'resources' / 'NotoSansCJK-Regular.ttc'
BUNDLED_FONT_ID = 'font_bundled_noto_sans_cjk_sc'
BUNDLED_FONT_NAME = 'Noto Sans CJK SC（随项目携带）'
MAX_FONT_BYTES = 40 * 1024 * 1024
ALLOWED_FONT_EXTENSIONS = {'.ttf', '.otf', '.ttc'}


@dataclass
class LoadedFont:
    record: dict
    buffer: bytes
    fitz_font: object
    subfont_index: int = 0
    missing_cache: set = field(default_factory=set)

    def has_glyph(self, codepoint):
        return self.fitz_font.has_glyph(codepoint)


class FontLibrary:
    """加载内置与项目字体，并提供字形覆盖诊断。"""

    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._cache = {}

    # ---------- 记录管理 ----------
    def bundled_record(self):
        return {
            'id': BUNDLED_FONT_ID,
            'name': BUNDLED_FONT_NAME,
            'kind': 'bundled',
            'path': 'resources/NotoSansCJK-Regular.ttc',
            'subfont_index': 2,
            'format': 'otf',
            'embedded_with_project': True,
            'uploaded_at': None,
        }

    def list_records(self):
        state = self.store.snapshot()
        records = [self.bundled_record()]
        records.extend(state.get('fonts', {}).values())
        return records

    def get_record(self, font_id):
        if font_id == BUNDLED_FONT_ID:
            return self.bundled_record()
        record = self.store.snapshot().get('fonts', {}).get(font_id)
        require(record is not None, '字体不存在', 404)
        return record

    def upload(self, uploaded):
        filename = (uploaded.filename or '').strip()
        require(bool(filename), '字体文件名为空')
        extension = '.' + filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        require(extension in ALLOWED_FONT_EXTENSIONS, '仅支持 TTF / OTF / TTC 字体文件')
        destination = self.store.new_file('fonts', extension)
        size = 0
        try:
            with destination.open('xb') as output:
                while True:
                    block = uploaded.stream.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    require(size <= MAX_FONT_BYTES, '单个字体不得超过40MB', 413)
                    output.write(block)
            require(size > 0, '字体文件为空')
            record = self._probe(self.store.relative(destination), extension, size)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        record['id'] = identity('font')
        record['name'] = filename.rsplit('.', 1)[0][:120]
        record['kind'] = 'project'
        record['path'] = self.store.relative(destination)
        record['embedded_with_project'] = True
        record['uploaded_at'] = now()
        with self.store.transaction() as state:
            state.setdefault('fonts', {})[record['id']] = record
            self.store.event(state, '上传项目字体', record['id'], {'name': record['name']})
        return record

    def _probe(self, relative, extension, size):
        """验证字体可被 PyMuPDF 打开并读取名称；TTC 取第 0 个子字体。"""
        import fitz
        try:
            if extension == '.ttc':
                buffer = self._extract_ttc(self.store.resolve(relative), 0)
                probe = fitz.Font(fontbuffer=buffer)
            else:
                probe = fitz.Font(fontfile=str(self.store.resolve(relative)))
        except DomainError:
            raise
        except Exception as error:  # PyMuPDF/fontTools 对损坏字体会抛出各自的异常类型
            raise DomainError('字体文件无法识别或已损坏：' + str(error), 400,
                              'invalid_font') from error
        return {
            'format': extension.lstrip('.'),
            'subfont_index': 0,
            'bytes': size,
            'family': probe.name,
        }

    def delete(self, font_id):
        require(font_id != BUNDLED_FONT_ID, '随项目携带的内置字体不能删除', 409)
        with self.store.transaction() as state:
            record = state.get('fonts', {}).pop(font_id, None)
            require(record is not None, '字体不存在', 404)
            self.store.event(state, '删除项目字体', font_id)
        path = self.store.resolve(record['path'])
        path.unlink(missing_ok=True)
        with self._lock:
            self._cache.pop(font_id, None)
        return {'deleted': font_id}

    # ---------- 加载 ----------
    def _load_bundled(self):
        return self._extract_ttc(BUNDLED_TTC, 2)

    @staticmethod
    def _extract_ttc(path, subfont_index):
        """从 TTC 中抽取指定子字体为独立 OTF/TTF 字节流。"""
        from fontTools.ttLib import TTCollection
        collection = TTCollection(str(path))
        require(0 <= subfont_index < len(collection.fonts), 'TTC 子字体索引无效')
        buffer = io.BytesIO()
        collection.fonts[subfont_index].save(buffer)
        return buffer.getvalue()

    def load(self, font_id):
        with self._lock:
            if font_id in self._cache:
                return self._cache[font_id]
            record = self.get_record(font_id)
            import fitz
            if record['kind'] == 'bundled':
                buffer = self._load_bundled()
            elif record['format'] == 'ttc':
                buffer = self._extract_ttc(self.store.resolve(record['path']), record.get('subfont_index', 0))
            else:
                buffer = self.store.resolve(record['path']).read_bytes()
            fitz_font = fitz.Font(fontbuffer=buffer)
            loaded = LoadedFont(record=record, buffer=buffer, fitz_font=fitz_font,
                                subfont_index=record.get('subfont_index', 0))
            self._cache[font_id] = loaded
            return loaded

    def resolve_chain(self, font_ids):
        """把字体 ID 列表解析为 LoadedFont 回退链；空列表回退到内置字体。"""
        if not font_ids:
            font_ids = [BUNDLED_FONT_ID]
        chain = []
        seen = set()
        for font_id in font_ids:
            if font_id in seen:
                continue
            seen.add(font_id)
            chain.append(self.load(font_id))
        return chain

    def subset_buffer(self, loaded, characters):
        """用 fontTools 按实际使用字符子集化，返回 (字节流, 旧GID→新GID 映射)。

        保留 CID 键控 CFF / TrueType 轮廓与 cmap；PyMuPDF 再嵌入时 GID 以
        子集字体为准，调用方必须用返回的映射重写内容流与 ToUnicode。
        """
        from fontTools.subset import Options, Subsetter
        from fontTools.ttLib import TTFont

        if not characters:
            return loaded.buffer, {}
        if loaded.record['kind'] == 'bundled':
            font = TTFont(str(BUNDLED_TTC), fontNumber=loaded.subfont_index)
        elif loaded.record['format'] == 'ttc':
            font = TTFont(str(self.store.resolve(loaded.record['path'])),
                          fontNumber=loaded.subfont_index)
        else:
            font = TTFont(str(self.store.resolve(loaded.record['path'])))
        options = Options()
        options.layout_features = ['*']
        options.name_IDs = ['*']
        options.notdef_outline = True
        options.recalc_bounds = True
        options.glyph_names = True
        options.drop_tables = ['FFTM']
        subsetter = Subsetter(options=options)
        # 只保留实际出现的码位，去重
        subsetter.populate(text=''.join(sorted(set(characters))))
        subsetter.subset(font)
        buffer = io.BytesIO()
        font.save(buffer)
        subset_data = buffer.getvalue()
        # 计算旧 GID -> 新 GID：通过共同码位在两版 cmap 中的字形编号对照
        import fitz as _fitz
        subset_font = _fitz.Font(fontbuffer=subset_data)
        gid_map = {}
        for codepoint in set(ord(char) for char in characters):
            old_gid = loaded.fitz_font.has_glyph(codepoint)
            new_gid = subset_font.has_glyph(codepoint)
            if old_gid and new_gid:
                gid_map[old_gid] = new_gid
        return subset_data, gid_map

    # ---------- 覆盖诊断 ----------
    def resolve_glyph(self, chain, codepoint):
        """按回退链找字形，返回 (LoadedFont, gid)，全部缺失返回 (None, 0)。"""
        for loaded in chain:
            gid = loaded.has_glyph(codepoint)
            if gid:
                return loaded, gid
        return None, 0

    def coverage(self, text, font_ids):
        """逐字符覆盖报告；不修改文本，不做任何近似替换。"""
        chain = self.resolve_chain(font_ids)
        used = {}
        unsupported = []
        total = 0
        for index, char in enumerate(text):
            if char in '\n\r\t':
                continue
            total += 1
            loaded, gid = self.resolve_glyph(chain, ord(char))
            if loaded is None:
                unsupported.append({
                    'index': index,
                    'character': char,
                    'codepoint': 'U+%04X' % ord(char),
                    'font_id': None,
                })
                continue
            used.setdefault(loaded.record['id'], {'font_id': loaded.record['id'],
                                                   'font_name': loaded.record['name'],
                                                   'characters': set()})
            used[loaded.record['id']]['characters'].add(char)
        fonts_used = []
        for item in used.values():
            chars = item.pop('characters')
            item['character_count'] = len(chars)
            fonts_used.append(item)
        return {
            'total_characters': total,
            'supported': total - len(unsupported),
            'unsupported': unsupported,
            'unsupported_characters': sorted({entry['character'] for entry in unsupported}),
            'complete': not unsupported,
            'fonts_used': fonts_used,
            'chain': [{'font_id': loaded.record['id'], 'font_name': loaded.record['name']}
                      for loaded in chain],
        }
