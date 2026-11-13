"""可搜索 PDF 文字层构建：真实定位、真实字符、真实字宽。

与旧实现的区别：
- 嵌入随项目/用户选择的字体（Type0 + Identity-H），逐字按回退链选字体，
  找不到字形的字符如实上报，绝不写入 .notdef 或以近似字替代。
- 手工生成 ToUnicode CMap（双 codespace：BMP 两字节 + 辅助平面四字节），
  修正 PyMuPDF 内置接口对 U+FFFF 以上码位的映射截断，并保证空格回读为 U+0020。
- 字号只依据文字框真实高度确定，不因框窄而任意缩小；超出已有框范围的内容
  生成 overflow 标记，交人工核对，不叠字、不挪位。
- 支持局部竖排文字框（orientation='v'），字符按原框自上而下逐字定位。
- 组合字符序列（基础字符 + 组合附加符号）整体保留，按各自真实字宽排布。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import fitz

from .core import require

# PDF 文本渲染模式 3 = invisible（仅文字层，不显示字形）
RENDER_MODE_INVISIBLE = 3
MAX_CMAP_ENTRIES = 100
OVERFLOW_TOLERANCE = 1.5  # pt，小于该值的测量误差不算溢出


@dataclass
class Placement:
    page_id: str
    word_id: str | None
    text: str
    rect: tuple  # (x0, y0, x1, y1) in PDF points
    font_size: float
    orientation: str
    font_id: str
    font_name: str
    line: str | None = None


@dataclass
class PagePlan:
    page_id: str
    width: float
    height: float
    placements: list = field(default_factory=list)
    unsupported: list = field(default_factory=list)
    overflow: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    cmap_conflicts: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.unsupported


def _escape_pdf_text_hex(text):
    return text.encode('utf-16-be').hex().upper()


def build_tounicode(mapping):
    """根据 {gid: 字符或字符序列} 生成符合规范的 ToUnicode CMap。"""
    bmp = {gid: value for gid, value in mapping.items() if gid <= 0xFFFF}
    supplementary = {gid: value for gid, value in mapping.items() if gid > 0xFFFF}
    lines = [
        '/CIDInit /ProcSet findresource begin',
        '12 dict begin',
        'begincmap',
        '/CIDSystemInfo <</Registry (Adobe)/Ordering (UCS)/Supplement 0>> def',
        '/CMapName /PaperFlow-Identity-UCS def',
        '/CMapType 2 def',
        '2 begincodespacerange',
        '<0000> <FFFF>',
        '<00010000> <FFFFFFFF>',
        'endcodespacerange',
    ]

    def emit(group, width):
        cids = sorted(group)
        for start in range(0, len(cids), MAX_CMAP_ENTRIES):
            chunk = cids[start:start + MAX_CMAP_ENTRIES]
            lines.append(f'{len(chunk)} beginbfchar')
            for gid in chunk:
                value = group[gid]
                lines.append(f'<{gid:0{width}X}> <{_escape_pdf_text_hex(value)}>')
            lines.append('endbfchar')

    emit(bmp, 4)
    emit(supplementary, 8)
    lines += [
        'endcmap',
        'CMapName currentdict /CMap defineresource pop',
        'end',
        'end',
    ]
    return '\n'.join(lines).encode('ascii')


class TextLayerBuilder:
    """把一页 OCR 文字框规划并写入 PDF 页面的不可见文字层。"""

    def __init__(self, font_library):
        self.font_library = font_library

    # ---------- 规划 ----------
    def plan_page(self, page_source, font_ids, scale):
        result = page_source.get('ocr')
        require(result is not None, '可搜索 PDF 要求所有页面先完成 OCR', 409)
        require(not result.get('stale'), '页面图像已变化，请更新 OCR 后导出可搜索 PDF', 409)
        chain = self.font_library.resolve_chain(font_ids)
        page_width = page_source['width'] * scale
        page_height = page_source['height'] * scale
        plan = PagePlan(page_id=page_source['id'], width=page_width, height=page_height)

        if result.get('text_override'):
            self._plan_override(plan, result, chain, scale)
        else:
            for word in result['boxes']:
                self._plan_word(plan, word, chain, scale)
        return plan

    def _plan_word(self, plan, word, chain, scale):
        text = word['text']
        if not text:
            return
        orientation = word.get('orientation', 'h')
        require(orientation in ('h', 'v'), '文字方向无效，仅支持 h（横排）或 v（竖排）')
        x = word['x'] * scale
        y = word['y'] * scale
        box_width = max(word['width'] * scale, 1)
        box_height = max(word['height'] * scale, 1)
        if orientation == 'h':
            # 横排：字号只来自框高，保持与扫描字迹一致的物理尺寸，不做适配性缩小。
            font_size = max(0.5, box_height * 0.83)
        else:
            # 竖排：框宽对应单字字号，框高是整列文字的排布高度。
            font_size = max(0.5, box_width * 0.9)

        segments, missing = self._segment(text, chain)
        for entry in missing:
            entry.update({'page_id': plan.page_id, 'word_id': word['id'],
                          'box_rect': [word['x'], word['y'], word['width'], word['height']],
                          'line': word.get('line')})
            plan.unsupported.append(entry)
        if not segments:
            return

        if orientation == 'h':
            self._place_horizontal(plan, word, segments, x, y, box_width, box_height,
                                   font_size, text)
        else:
            self._place_vertical(plan, word, segments, x, y, box_width, box_height,
                                 font_size, text)

    def _segment(self, text, chain):
        """按可用字体把字符串切成 (loaded, gid_list, substring) 段，并收集缺字。"""
        segments = []
        missing = []
        current_font = None
        current_gids = []
        current_chars = []
        for char in text:
            loaded, gid = self.font_library.resolve_glyph(chain, ord(char))
            if loaded is None:
                if current_chars:
                    segments.append((current_font, current_gids, ''.join(current_chars)))
                    current_font, current_gids, current_chars = None, [], []
                missing.append({'character': char, 'codepoint': 'U+%04X' % ord(char)})
                continue
            if current_font is not None and loaded.record['id'] != current_font.record['id']:
                segments.append((current_font, current_gids, ''.join(current_chars)))
                current_font, current_gids, current_chars = None, [], []
            current_font = loaded
            current_gids.append(gid)
            current_chars.append(char)
        if current_chars:
            segments.append((current_font, current_gids, ''.join(current_chars)))
        return segments, missing

    def _place_horizontal(self, plan, word, segments, x, y, box_width, box_height,
                          font_size, full_text):
        baseline = y + box_height * 0.85
        cursor = x
        measured_width = 0.0
        for loaded, gids, substring in segments:
            fitz_font = loaded.fitz_font
            advance = sum(fitz_font.glyph_advance(gid) for gid in gids) * font_size
            rect = (round(cursor, 3), round(y, 3),
                    round(cursor + advance, 3), round(y + box_height, 3))
            plan.placements.append(Placement(
                page_id=plan.page_id, word_id=word['id'], text=substring, rect=rect,
                font_size=font_size, orientation='h',
                font_id=loaded.record['id'], font_name=loaded.record['name'],
                line=word.get('line'),
            ))
            cursor += advance
            measured_width += advance
        if measured_width > box_width + OVERFLOW_TOLERANCE:
            plan.overflow.append({
                'page_id': plan.page_id,
                'word_id': word['id'],
                'text': full_text,
                'box_rect': [round(x, 2), round(y, 2), round(box_width, 2), round(box_height, 2)],
                'measured_width': round(measured_width, 2),
                'box_width': round(box_width, 2),
                'overflow_width': round(measured_width - box_width, 2),
                'orientation': 'h',
                'message': '真实字宽超出原文字框，未缩小字号，请人工核对选取范围',
            })

    def _place_vertical(self, plan, word, segments, x, y, box_width, box_height,
                        font_size, full_text):
        # 竖排：以框内左上角为起点，逐字自上而下排布；每字占据一个字号高度的步距，
        # 水平方向按真实字宽居中于原框，保留各字真实定位依据。
        step = font_size * 1.12
        cursor_y = y
        measured_height = 0.0
        for loaded, gids, substring in segments:
            fitz_font = loaded.fitz_font
            for gid, char in zip(gids, substring):
                advance = fitz_font.glyph_advance(gid) * font_size
                origin_x = x + max(0.0, (box_width - advance) / 2)
                rect = (round(origin_x, 3), round(cursor_y, 3),
                        round(origin_x + advance, 3), round(cursor_y + font_size, 3))
                plan.placements.append(Placement(
                    page_id=plan.page_id, word_id=word['id'], text=char, rect=rect,
                    font_size=font_size, orientation='v',
                    font_id=loaded.record['id'], font_name=loaded.record['name'],
                    line=word.get('line'),
                ))
                cursor_y += step
                measured_height += step
        if measured_height > box_height + OVERFLOW_TOLERANCE:
            plan.overflow.append({
                'page_id': plan.page_id,
                'word_id': word['id'],
                'text': full_text,
                'box_rect': [round(x, 2), round(y, 2), round(box_width, 2), round(box_height, 2)],
                'measured_height': round(measured_height, 2),
                'box_height': round(box_height, 2),
                'overflow_height': round(measured_height - box_height, 2),
                'orientation': 'v',
                'message': '竖排文字真实高度超出原文字框，未压缩字距，请人工核对选取范围',
            })
    def _plan_override(self, plan, result, chain, scale):
        """人工整段文本：保持旧版页内边距框，但逐字真实排布并做覆盖/溢出诊断。"""
        lines = result['text'].splitlines() or ['']
        page_width, page_height = plan.width, plan.height
        margin = 12
        box = fitz.Rect(margin, margin, page_width - margin, page_height - margin)
        line_height = box.height / max(1, len(lines) * 1.5)
        font_size = min(11, max(3, line_height))
        synthetic_id = 0
        for line_index, line in enumerate(lines):
            if not line:
                continue
            synthetic_id += 1
            word = {
                'id': None,
                'text': line,
                'x': margin / scale,
                'y': (margin + line_index * line_height * 1.5) / scale,
                'width': (box.width) / scale,
                'height': font_size / scale / 0.83,
                'orientation': 'h',
                'line': f'override:{line_index + 1}',
            }
            self._plan_word(plan, word, chain, scale)

    # ---------- 写入 PDF ----------
    def prepare_subsets(self, plans, chain):
        """按全部页面实际用到的字符对每个字体做一次子集化。

        返回 {font_id: {'buffer', 'gid_map'(旧GID->新GID), 'chars', 'resource'}}。
        """
        chars_by_font = {loaded.record['id']: set() for loaded in chain}
        for plan in plans:
            for placement in plan.placements:
                if placement.font_id in chars_by_font:
                    chars_by_font[placement.font_id].update(placement.text)
        prepared = {}
        for index, loaded in enumerate(chain):
            characters = chars_by_font[loaded.record['id']]
            if not characters:
                continue
            buffer, gid_map = self.font_library.subset_buffer(loaded, characters)
            prepared[loaded.record['id']] = {
                'buffer': buffer,
                'gid_map': gid_map,
                'chars': characters,
                'resource': f'PF{index}',
                'loaded': loaded,
            }
        return prepared

    def write_document(self, pdf_document, plans, chain):
        """整份文档统一嵌入子集字体并写入各页文字层。"""
        prepared = self.prepare_subsets(plans, chain)
        # ToUnicode 是字体级（文档级）映射：子集 GID 在所有页面含义一致，
        # 因此先汇总全部页面的 新GID->字符 映射，再逐页写入同一份 CMap。
        document_maps = {font_id: {} for font_id in prepared}
        for plan in plans:
            self._collect_tounicode(plan, prepared, document_maps)
        for pdf_page, plan in zip(pdf_document, plans):
            self.write_page(pdf_page, plan, chain, prepared, document_maps)
        return prepared

    def write_page(self, pdf_page, plan, chain, prepared=None, tounicode_maps=None):
        if prepared is None:
            # 单页预览/独立使用场景：按需即时子集化
            prepared = self.prepare_subsets([plan], chain)
        font_resource = self._embed_fonts(pdf_page, prepared)
        content = self._build_content(plan, chain, prepared)
        if tounicode_maps is None:
            tounicode_maps = {font_id: {} for font_id in prepared}
        xref = pdf_page.parent.get_new_xref()
        pdf_page.parent.update_object(xref, '<< /Length 0 >>')
        pdf_page.parent.update_stream(xref, content, compress=0)
        pdf_page.set_contents(xref)
        for font_id, entry in prepared.items():
            mapping = tounicode_maps.get(font_id, {})
            if not mapping:
                continue
            type0_xref = font_resource[font_id]
            tu_match = re.findall(r'\d+',
                                  pdf_page.parent.xref_get_key(type0_xref, 'ToUnicode')[1])
            require(tu_match, '字体缺少 ToUnicode 引用，文字层无法保证可检索', 500)
            tu_xref = int(tu_match[0])
            # 映射表只含 int 型 GID 键，冲突信息单独存放，绝不污染 CMap。
            pdf_page.parent.update_stream(
                tu_xref, build_tounicode({gid: value for gid, value in mapping.items()
                                          if isinstance(gid, int)}), compress=0)

    def _embed_fonts(self, pdf_page, prepared):
        resource = {}
        for font_id, entry in prepared.items():
            pdf_page.insert_font(fontname=entry['resource'], fontbuffer=entry['buffer'])
            xref = pdf_page.get_fonts(full=True)[-1][0]
            resource[font_id] = xref
        return resource

    def _collect_tounicode(self, plan, prepared, document_maps):
        """汇总一页内全部 子集GID->字符 的字体级映射。"""
        for placement in plan.placements:
            entry = prepared.get(placement.font_id)
            require(entry is not None, '文字层引用了未嵌入的字体：' + placement.font_id,
                    500, 'font_not_embedded')
            loaded = entry['loaded']
            target = document_maps[placement.font_id]
            for char in placement.text:
                old_gid = loaded.has_glyph(ord(char))
                require(old_gid, f'字体 {loaded.record["name"]} 缺少字形 U+{ord(char):04X}', 500)
                new_gid = entry['gid_map'].get(old_gid)
                require(new_gid is not None,
                        f'子集字体丢失字形 U+{ord(char):04X}，文字层生成失败', 500)
                previous = target.get(new_gid)
                if previous is not None and previous != char:
                    # 同一 GID 被多个码位复用：保留首次映射，冲突单独记录给人工核对，
                    # 绝不静默改写用户文本或写入非法 CMap 键。
                    plan.cmap_conflicts.append({
                        'page_id': plan.page_id, 'word_id': placement.word_id,
                        'gid': new_gid, 'first': previous, 'later': char,
                        'font_id': placement.font_id,
                    })
                else:
                    target[new_gid] = char

    def _build_content(self, plan, chain, prepared):
        parts = ['BT']
        last_resource = None
        for placement in plan.placements:
            entry = prepared.get(placement.font_id)
            require(entry is not None, '文字层引用了未嵌入的字体：' + placement.font_id,
                    500, 'font_not_embedded')
            loaded = entry['loaded']
            resource_name = entry['resource']
            gid_map = entry['gid_map']
            x0, y0, x1, y1 = placement.rect
            if placement.orientation == 'h':
                # rect 高度即原文字框高度；基线相对 rect 顶部保持 0.85 比例
                baseline = y0 + (y1 - y0) * 0.85
                if last_resource != resource_name:
                    parts.append(f'/{resource_name} {placement.font_size:.3f} Tf '
                                 f'{RENDER_MODE_INVISIBLE} Tr')
                    last_resource = resource_name
                parts.append(f'1 0 0 1 {x0:.3f} {baseline:.3f} Tm')
                parts.append(self._hex_show(loaded, placement.text, gid_map))
            else:
                if last_resource != resource_name:
                    parts.append(f'/{resource_name} {placement.font_size:.3f} Tf '
                                 f'{RENDER_MODE_INVISIBLE} Tr')
                    last_resource = resource_name
                # 竖排单字：rect 以 em 方框给出（y1=y0+字号），基线按字体 ascender
                # 落在 em 方框内；逐字独立 Tm，选取框与定位一一对应。
                baseline = y0 + loaded.fitz_font.ascender * placement.font_size
                parts.append(f'1 0 0 1 {x0:.3f} {baseline:.3f} Tm')
                parts.append(self._hex_show(loaded, placement.text, gid_map))
        parts.append('ET')
        return '\n'.join(parts).encode('ascii')

    def _hex_show(self, loaded, text, gid_map):
        gids = []
        for char in text:
            old_gid = loaded.has_glyph(ord(char))
            require(old_gid, f'字体 {loaded.record["name"]} 缺少字形 U+{ord(char):04X}', 500)
            new_gid = gid_map.get(old_gid)
            require(new_gid is not None,
                    f'子集字体丢失字形 U+{ord(char):04X}，文字层生成失败', 500)
            gids.append(new_gid)
        return '<' + ''.join(f'{gid:04X}' for gid in gids) + '> Tj'


def verify_roundtrip(pdf_document, expected_texts):
    """导出后回读校验：每页提取文本必须与校订文本逐字一致（忽略换行差异）。"""
    require(len(pdf_document) == len(expected_texts), 'PDF 页数与待导出页面不一致', 500)
    failures = []
    for index, page in enumerate(pdf_document):
        expected = expected_texts[index] or ''
        actual = page.get_text() or ''
        expected_flat = expected.replace('\r', '').replace('\n', '').replace(' ', '')
        actual_flat = actual.replace('\r', '').replace('\n', '').replace(' ', '').replace('\xa0', '')
        if expected_flat != actual_flat:
            failures.append({
                'page_number': index + 1,
                'expected_characters': len(expected_flat),
                'actual_characters': len(actual_flat),
                'first_difference': _first_difference(expected_flat, actual_flat),
            })
    return {'ok': not failures, 'failures': failures}


def _first_difference(expected, actual):
    for index, (a, b) in enumerate(zip(expected, actual)):
        if a != b:
            return {'index': index, 'expected': a, 'actual': b,
                    'expected_codepoint': 'U+%04X' % ord(a),
                    'actual_codepoint': 'U+%04X' % ord(b)}
    if len(expected) != len(actual):
        index = min(len(expected), len(actual))
        leftover = expected[index:] if len(expected) > len(actual) else actual[index:]
        return {'index': index, 'expected': expected[index:index + 1] or None,
                'actual': actual[index:index + 1] or None,
                'remainder': leftover[:20]}
    return None
