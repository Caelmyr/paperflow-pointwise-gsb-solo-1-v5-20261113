"""黑白（1 位）分区渲染。

一页混合资料中同时存在细字、标点、表格线、反白（黑底白字）标题和连续层次的插图。
本模块先在连续灰度源图上按内容连通域分区：

* 结构区（文字、标点、表格线、线稿）用局部自适应阈值，保持笔画和线段连贯；
* 连续层次区用有序网点表达深浅，保留插图的层次；
* 反白面板保留深色底并挖出白色笔画，避免整块消失；
* 其余纸面保持纯白。

最终只输出纯黑与纯白两种像素（1 位 PNG）。渲染始终从原始连续稿重新开始：
改变输出尺寸、调整参数或重新确认都不会对旧的黑白结果反复二值化。
分区归属可由人工区域覆盖纠正，预览、确认、区域提取、下载图片和 PDF 都对应同一份确认结果。
"""
from __future__ import annotations

import hashlib
import io
import json

import cv2
import numpy as np
from PIL import Image

from .core import choice, number, require
from .imaging import gray, validate_image

BLANK, TEXT, LINE, TONE, REVERSE, SOLID = 0, 1, 2, 3, 4, 5
KIND_NAMES = {
    BLANK: 'blank', TEXT: 'text', LINE: 'line', TONE: 'illustration',
    REVERSE: 'reverse', SOLID: 'solid',
}
ZONE_KINDS = ('auto', 'text', 'line', 'illustration', 'reverse', 'blank')
DEFAULTS = {'scale': 1.0, 'cell': 8, 'style': 'cluster', 'assumed_dpi': 300, 'keep_scale': True}

BAYER8 = np.array([
    [0, 48, 12, 60, 3, 51, 15, 63],
    [32, 16, 44, 28, 35, 19, 47, 31],
    [8, 56, 4, 52, 11, 59, 7, 55],
    [40, 24, 36, 20, 43, 27, 39, 23],
    [2, 50, 14, 62, 1, 49, 13, 61],
    [34, 18, 46, 30, 33, 17, 45, 29],
    [10, 58, 6, 54, 9, 57, 5, 53],
    [42, 26, 38, 22, 41, 25, 37, 21],
], np.float32) / 63.0


def _cluster_matrix(size=8):
    half = (size - 1) / 2
    items = []
    for y in range(size):
        for x in range(size):
            items.append(((x - half) ** 2 + (y - half) ** 2, y, x))
    items.sort()
    matrix = np.empty((size, size), np.float32)
    for rank, (_, y, x) in enumerate(items):
        matrix[y, x] = rank
    return matrix / float(size * size - 1)


CLUSTER8 = _cluster_matrix(8)
MATRICES = {'cluster': CLUSTER8, 'ordered': BAYER8}


def settings(values=None):
    values = values or {}
    require(isinstance(values, dict), '黑白参数必须为对象')
    unknown = set(values) - set(DEFAULTS)
    require(not unknown, '未知黑白参数：' + ', '.join(sorted(unknown)))
    scale = number(values.get('scale', DEFAULTS['scale']), '输出比例', 0.1, 4)
    cell = number(values.get('cell', DEFAULTS['cell']), '网点单元', 2, 24, True)
    style = choice(values.get('style', DEFAULTS['style']), '网点类型', ('cluster', 'ordered'))
    dpi = number(values.get('assumed_dpi', DEFAULTS['assumed_dpi']), '源稿分辨率', 72, 1200, True)
    keep = values.get('keep_scale', DEFAULTS['keep_scale'])
    require(isinstance(keep, bool), '保持网点尺度必须为布尔值')
    return {'scale': round(scale, 4), 'cell': int(cell), 'style': style, 'assumed_dpi': int(dpi), 'keep_scale': keep}


def pattern(config):
    scale = config['scale']
    output_dpi = config['assumed_dpi'] * scale
    cell_px = config['cell'] * scale if config['keep_scale'] else float(config['cell'])
    cell_px = max(2, int(round(cell_px)))
    return {
        'cell_px': cell_px,
        'cell_mm': round(cell_px / max(output_dpi, 1) * 25.4, 3),
        'style': config['style'],
        'assumed_dpi': config['assumed_dpi'],
        'output_dpi': round(output_dpi, 1),
        'keep_scale': config['keep_scale'],
        'levels': 64,
    }


def zones(values, width, height):
    require(values is None or isinstance(values, list), '分区纠正必须是列表')
    require(values is None or len(values) <= 200, '分区纠正最多200条')
    result = []
    for item in values or []:
        require(isinstance(item, dict), '分区纠正格式无效')
        kind = choice(item.get('kind'), '分区归属', ZONE_KINDS)
        rect = item.get('rect')
        require(isinstance(rect, (list, tuple)) and len(rect) == 4, '分区需要[x,y,宽,高]')
        x = number(rect[0], '分区x', 0, width, True)
        y = number(rect[1], '分区y', 0, height, True)
        w = number(rect[2], '分区宽', 8, width, True)
        h = number(rect[3], '分区高', 8, height, True)
        require(x + w <= width and y + h <= height, '分区越出图片')
        result.append({'rect': [int(x), int(y), int(w), int(h)], 'kind': kind})
    return result


def is_binary(image):
    values = np.unique(gray(image))
    return values.size <= 4 and bool(np.all((values < 8) | (values > 247)))


def _odd(value, minimum=3):
    value = max(minimum, int(round(value)))
    return value if value % 2 == 1 else value + 1


def _paper_field(luminance):
    """只用亮而平滑的像素估计纸张底色，再归一化填补暗区。

    在 ~800px 的缩略图上估计后放大：既快，也避免把大块插图当作纸色。
    """
    height, width = luminance.shape
    ratio = min(1.0, 800 / max(height, width))
    small = cv2.resize(luminance, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA) if ratio < 1 else luminance
    smooth = cv2.GaussianBlur(small.astype(np.float32), (5, 5), 0)
    local = cv2.boxFilter(smooth, -1, (9, 9), borderType=cv2.BORDER_REFLECT)
    square = cv2.boxFilter(smooth ** 2, -1, (9, 9), borderType=cv2.BORDER_REFLECT)
    deviation = np.sqrt(np.maximum(square - local ** 2, 0))
    valid = ((smooth > 195) & (deviation < 14)).astype(np.float32)
    sigma = max(3.0, min(small.shape) / 25.0)
    if valid.mean() < 0.01:
        valid[:] = 1.0
    total = cv2.GaussianBlur(smooth * valid, (0, 0), sigma)
    divisor = cv2.GaussianBlur(valid, (0, 0), sigma)
    fallback = float(np.percentile(smooth[valid > 0.5], 90)) if np.any(valid > 0.5) else 245.0
    paper = np.where(divisor > 0.04, total / np.maximum(divisor, 1e-4), fallback)
    paper = cv2.GaussianBlur(paper, (0, 0), sigma)
    paper = np.clip(paper, 80, 255)
    if ratio < 1:
        paper = cv2.resize(paper, (width, height), interpolation=cv2.INTER_CUBIC)
    return paper


def normalized(luminance):
    return np.clip(luminance.astype(np.float32) / np.maximum(_paper_field(luminance), 1) * 255, 0, 255)


def _screen_tile(shape, config):
    height, width = shape
    period = pattern(config)['cell_px']
    base = cv2.resize(MATRICES[config['style']], (period, period), interpolation=cv2.INTER_NEAREST)
    tile = np.tile(base, ((height + period - 1) // period, (width + period - 1) // period))[:height, :width]
    return tile


def _clean_mask(mask, minimum):
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    keep = np.zeros(count, bool)
    keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= minimum
    return keep[labels]


def _component_records(content, normalized_image, cell):
    """按连通域统计内容块，给出粗分类。"""
    height, width = content.shape
    depth = cv2.distanceTransform(content.astype(np.uint8), cv2.DIST_L2, 5)
    split = max(2.5, min(height, width) * 0.0016)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(content.astype(np.uint8), 8)
    records = []
    limit = min(count, 20000)
    for index in range(1, limit):
        x, y, w, h, area = stats[index]
        if area < 4:
            continue
        patch = labels[y:y + h, x:x + w] == index
        interior = float(((depth[y:y + h, x:x + w] > split) & patch).sum() / area)
        values = normalized_image[y:y + h, x:x + w][patch]
        mean = float(values.mean())
        deviation = float(values.std())
        solidity = area / float(w * h)
        thin = (w >= h * 8 or h >= w * 8) and max(w, h) > 30 and interior < 0.25
        record = {
            'index': index,
            'rect': [int(x), int(y), int(w), int(h)],
            'area': int(area),
            'coverage': round(float(area / (height * width)), 4),
            'interior': round(interior, 4),
            'mean': round(mean, 2),
            'std': round(deviation, 2),
            'solidity': round(solidity, 4),
        }
        if interior > 0.45 and min(w, h) >= 12 and solidity > 0.35:
            record['kind'] = 'block'
        elif thin:
            record['kind'] = 'line'
        else:
            record['kind'] = 'text'
        records.append(record)
    return records, labels, depth


def _silhouette(patch):
    """用最外层轮廓填充得到实心面板形状，字母/镂空会被算作面板内部的洞。"""
    source = patch.astype(np.uint8)
    contours, _ = cv2.findContours(source, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    largest = max(contours, key=cv2.contourArea)
    filled = np.zeros_like(source)
    cv2.drawContours(filled, [largest], -1, 1, thickness=cv2.FILLED)
    silhouette = filled > 0
    if silhouette.mean() < 0.25:
        return None
    return silhouette


def _hole_statistics(mask, patch, closing):
    silhouette = _silhouette(patch)
    if silhouette is None:
        return None
    holes = silhouette & ~patch
    if holes.sum() < 6:
        return None
    count, _, stats, _ = cv2.connectedComponentsWithStats(holes.astype(np.uint8), 8)
    areas, heights = [], []
    for index in range(1, count):
        area = stats[index, cv2.CC_STAT_AREA]
        if area >= 2:
            areas.append(area)
            heights.append(stats[index, cv2.CC_STAT_HEIGHT])
    if not areas:
        return None
    return {
        'silhouette': silhouette,
        'holes': holes,
        'count': len(areas),
        'share': float(holes.sum() / max(1, silhouette.sum())),
        'median_height': float(np.median(heights)),
        'largest_share': float(max(areas) / max(1, silhouette.sum())),
    }


def _panel_kind(hole, rect):
    w, h = rect[2], rect[3]
    if hole is None:
        return 'solid'
    if not 0.003 <= hole['share'] <= 0.55:
        return 'solid'
    if hole['count'] < 2:
        return 'solid'
    if hole['largest_share'] > 0.32:
        return 'solid'
    if hole['median_height'] > max(4.0, h * 0.45):
        return 'solid'
    return 'reverse'


def _category_map(shape, records, dark, cell):
    height, width = shape
    categories = np.zeros((height, width), np.uint8)
    panels = []
    # 大面积连通块先分类，避免反白面板内部的字母内孔被当成独立插图。
    for record in sorted(records, key=lambda item: -item['area']):
        x, y, w, h = record['rect']
        if record['kind'] != 'block':
            continue
        if any(_contains(panel['rect'], record['rect']) for panel in panels):
            record['kind'] = 'panel'
            continue
        patch = dark[y:y + h, x:x + w]
        if min(w, h) >= 16 and _is_continuous(record, patch):
            # 连续层次：整块矩形按网点表达，浅端也保留为可印刷的淡层。
            categories[y:y + h, x:x + w] = TONE
            record['kind'] = 'tone'
            continue
        silhouette = _silhouette(patch)
        if silhouette is None:
            categories[y:y + h, x:x + w] = SOLID
            record['kind'] = 'solid'
            continue
        glyph = silhouette & ~patch
        hole = _summarise_holes(glyph, silhouette, record['rect'])
        kind = _panel_kind(hole, record['rect'])
        record['kind'] = kind
        interior = categories[y:y + h, x:x + w]
        interior[silhouette] = REVERSE if kind == 'reverse' else SOLID
        categories[y:y + h, x:x + w] = interior
        panels.append({
            'kind': kind,
            'rect': list(record['rect']),
            'silhouette': silhouette,
            'glyph': glyph if kind == 'reverse' else None,
        })
    structure = dark & (categories == 0)
    categories[structure] = np.where(categories[structure] == LINE, LINE, TEXT)
    return categories, panels


def _is_continuous(record, patch):
    """判断连通块内部是否有可印刷的灰阶变化。

    纯色块（色条、实黑底、反白底）和整页纸面（阴影渐变）不属于插图：
    前者按实黑/反白处理，后者按结构区与空白处理。
    """
    if record['mean'] < 30:
        return False
    if record['std'] < 12:
        return False
    # 整页或近乎整页的连通块多是纸面阴影或扫描底色，不是插图。
    if record['coverage'] >= 0.35:
        return False
    # 形状规整又缺少内部细节的多为色条/色块，按实黑处理。
    if record['solidity'] >= 0.9 and record['interior'] >= 0.9 and record['std'] < 24:
        return False
    return True


def _contains(outer, inner, share=0.8):
    x, y, w, h = inner
    area = w * h
    if area <= 0:
        return False
    left = max(outer[0], x)
    top = max(outer[1], y)
    right = min(outer[0] + outer[2], x + w)
    bottom = min(outer[1] + outer[3], y + h)
    if right <= left or bottom <= top:
        return False
    return (right - left) * (bottom - top) >= share * area


def _summarise_holes(glyph, silhouette, rect):
    if glyph.sum() < 6:
        return None
    count, _, stats, _ = cv2.connectedComponentsWithStats(glyph.astype(np.uint8), 8)
    areas, heights = [], []
    for index in range(1, count):
        area = stats[index, cv2.CC_STAT_AREA]
        if area >= 2:
            areas.append(area)
            heights.append(stats[index, cv2.CC_STAT_HEIGHT])
    if not areas:
        return None
    return {
        'count': len(areas),
        'share': float(glyph.sum() / max(1, silhouette.sum())),
        'median_height': float(np.median(heights)),
        'largest_share': float(max(areas) / max(1, silhouette.sum())),
    }


def _mark_lines(categories, records):
    for record in records:
        if record['kind'] != 'line':
            continue
        x, y, w, h = record['rect']
        categories[y:y + h, x:x + w][categories[y:y + h, x:x + w] == TEXT] = LINE
    return categories


def _zone_mask(rect, shape):
    height, width = shape
    x, y, w, h = rect
    mask = np.zeros((height, width), bool)
    mask[y:y + h, x:x + w] = True
    return mask


def _apply_zones(categories, panels, zones_used, shape, dark):
    manual_panels = []
    for zone in zones_used:
        kind = zone['kind']
        if kind == 'auto':
            continue
        mask = _zone_mask(zone['rect'], shape)
        categories[mask] = {
            'text': TEXT, 'line': LINE, 'illustration': TONE, 'reverse': REVERSE, 'blank': BLANK,
        }[kind]
        if kind == 'reverse':
            x, y, w, h = zone['rect']
            patch = dark[y:y + h, x:x + w]
            closing = _odd(max(3, min(w, h) // 26), 3)
            hole = _hole_statistics(patch, patch, closing)
            if hole is None:
                manual_panels.append({
                    'kind': 'reverse', 'rect': list(zone['rect']),
                    'silhouette': np.ones_like(patch), 'glyph': ~patch, 'manual': True,
                })
            else:
                manual_panels.append({
                    'kind': 'reverse', 'rect': list(zone['rect']),
                    'silhouette': hole['silhouette'], 'glyph': hole['holes'], 'manual': True,
                })
    return manual_panels


def _render_structure(normalized_image, categories):
    height, width = normalized_image.shape
    window = _odd(max(15, round(min(height, width) / 55)))
    local_mean = cv2.boxFilter(normalized_image, -1, (window, window), borderType=cv2.BORDER_REFLECT)
    local_square = cv2.boxFilter(normalized_image ** 2, -1, (window, window), borderType=cv2.BORDER_REFLECT)
    local_std = np.sqrt(np.maximum(local_square - local_mean ** 2, 0))
    threshold = np.clip(local_mean * (1 + 0.2 * (local_std / 128.0 - 1)), 1, 254)
    structure = (categories == TEXT) | (categories == LINE)
    return (normalized_image < threshold) & structure


def _render_tone(normalized_image, categories, config):
    tile = _screen_tile(normalized_image.shape, config)
    threshold = 255 * (1 - tile)
    tone = categories == TONE
    return (normalized_image < threshold) & tone


def _render_panels(panels, shape):
    ink = np.zeros(shape, bool)
    for panel in panels:
        x, y, w, h = panel['rect']
        x1, y1 = min(shape[1], x + w), min(shape[0], y + h)
        if x1 <= x or y1 <= y:
            continue
        silhouette = np.asarray(panel['silhouette'], bool)
        if silhouette.shape != (y1 - y, x1 - x):
            silhouette = cv2.resize(silhouette.astype(np.uint8), (x1 - x, y1 - y), interpolation=cv2.INTER_NEAREST) > 0
        block = ink[y:y1, x:x1]
        if panel['kind'] == 'reverse' and panel.get('glyph') is not None:
            glyph = np.asarray(panel['glyph'], bool)
            if glyph.shape != (y1 - y, x1 - x):
                glyph = cv2.resize(glyph.astype(np.uint8), (x1 - x, y1 - y), interpolation=cv2.INTER_NEAREST) > 0
            block |= silhouette & ~glyph
        else:
            block |= silhouette
    return ink


def _finish(ink, categories, dark, cell):
    structure = (categories == TEXT) | (categories == LINE)
    mask = (ink & structure).astype(np.uint8)
    length = max(18, int(round(ink.shape[1] / 40)))
    horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (length, 1)))
    vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, length)))
    gap = max(2, int(round(cell / 8)))
    horizontal = cv2.morphologyEx(horizontal, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (gap, 1)))
    vertical = cv2.morphologyEx(vertical, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, gap)))
    ink = ink | ((horizontal | vertical) > 0)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), 8)
    quiet = ~dark & (categories == BLANK)
    result = ink.copy()
    for index in range(1, count):
        if stats[index, cv2.CC_STAT_AREA] > 2:
            continue
        component = labels == index
        if quiet[component].mean() > 0.85:
            result[component] = False
    return result


def _stroke_width(mask):
    if not mask.any():
        return 0.0
    distance = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 5)
    values = distance[distance > 0]
    return round(float(values.min() * 2), 2) if values.size else 0.0


def tone_levels(values):
    """按块密度统计网点可表达的灰阶级数（0–8 档）。"""
    values = np.asarray(values, np.float32).ravel()
    if values.size < 16:
        return 0
    blocks = values[: values.size // 16 * 16].reshape(-1, 16).mean(axis=1)
    if blocks.size < 4:
        return 0
    return int(len(set(np.minimum(7, (blocks * 8).astype(np.int32)).tolist())))


def _count(mask, minimum=1):
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    return sum(1 for index in range(1, count) if stats[index, cv2.CC_STAT_AREA] >= minimum)


def _cluster_boxes(mask, cell, limit=40):
    grown = cv2.dilate(mask.astype(np.uint8), np.ones((cell, cell), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(grown, 8)
    boxes = []
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if area < 4:
            continue
        boxes.append([int(x), int(y), int(w), int(h)])
    boxes.sort(key=lambda box: (box[1], box[0]))
    return boxes[:limit]


def boundary_check(ink, normalized_image, categories, cell):
    """测量不同内容交接处纸面的底色密度跳变与重复描边。"""
    height, width = ink.shape
    paper = normalized_image > 240
    boundary = np.zeros((height, width), bool)
    boundary[1:, :] |= categories[1:, :] != categories[:-1, :]
    boundary[:, 1:] |= categories[:, 1:] != categories[:, :-1]
    band = cv2.dilate(boundary.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    paper_band = band & paper
    if not np.any(paper_band):
        return {'band_ink_ratio': 0.0, 'boundary_pixels': int(boundary.sum()), 'stable': True}
    density = float(ink[paper_band].mean())
    # 交界处若出现横向双描边（重复边），墨点占比会明显高于周围纸面。
    near = cv2.dilate(paper_band.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    interior = paper & near & ~paper_band
    baseline = float(ink[interior].mean()) if np.any(interior) else 0.0
    return {
        'band_ink_ratio': round(density, 4),
        'near_ink_ratio': round(baseline, 4),
        'boundary_pixels': int(boundary.sum()),
        'stable': density <= max(0.02, baseline + 0.012),
    }


def _region_stats(kind, rect, ink, source, categories, scale):
    x, y, w, h = rect
    allowed = {
        'text': (TEXT,), 'line': (LINE,), 'illustration': (TONE,),
        'reverse': (REVERSE,), 'solid': (SOLID,), 'blank': (BLANK,),
    }[kind]
    patch_category = np.isin(categories[y:y + h, x:x + w], allowed)
    ink_patch = ink[y:y + h, x:x + w]
    source_patch = source[y:y + h, x:x + w]
    entry = {'kind': kind, 'rect': [int(x), int(y), int(w), int(h)], 'ink_ratio': 0.0}
    if not patch_category.any():
        entry['preserved'] = True
        entry['loss'] = ''
        return entry
    patch_ink = ink_patch[patch_category]
    entry['ink_ratio'] = round(float(patch_ink.mean()), 4)
    dark_source = source_patch < 160
    if kind in ('text', 'line'):
        coverage = float((ink_patch & dark_source).sum() / max(1, dark_source.sum())) if dark_source.any() else 1.0
        output_components = _count(ink_patch, 1)
        source_components = _count(dark_source, max(1, round(2 * scale * scale)))
        entry.update({
            'components': output_components,
            'source_components': source_components,
            'coverage': round(coverage, 4),
            'stroke_min_px': _stroke_width(ink_patch),
        })
        preserved = bool(coverage >= 0.9 and output_components <= max(4, source_components * 2.5))
        entry['preserved'] = preserved
        entry['loss'] = '' if preserved else '笔画可能断裂或出现碎点，细字、标点或线段不连贯'
    elif kind == 'illustration':
        source_values = source_patch[patch_category]
        output_values = patch_ink.astype(np.float32)
        levels = tone_levels(output_values)
        # 按源灰度分位检查层次是否单调保留，并找出被压死的端点。
        order = np.argsort(source_values)
        ordered_output = output_values[order]
        count = ordered_output.size
        dark_ten = ordered_output[:max(1, count // 10)].mean()
        light_ten = ordered_output[-max(1, count // 10):].mean()
        entry.update({
            'tone_levels': levels,
            'mean_density': round(float(patch_ink.mean()), 4),
            'source_mid_ratio': round(float(((source_values >= 40) & (source_values <= 215)).mean()), 4),
            'dark_end_density': round(float(dark_ten), 4),
            'light_end_density': round(float(light_ten), 4),
            'range': round(float(dark_ten - light_ten), 4),
        })
        # 没有中间调的纯色区本来就没有连续层次可丢，不应误报为压平。
        applicable = entry['source_mid_ratio'] >= 0.06
        # 只要求单调保留层次：暗端明显比亮端密，且两端都没有被压死。
        preserved = bool(levels >= 3 and entry['range'] >= 0.1 and light_ten <= 0.75) if applicable else True
        entry['tone_applicable'] = bool(applicable)
        entry['preserved'] = preserved
        entry['loss'] = '' if preserved or not applicable else '连续层次被压成实黑或空白，网点不足'
    elif kind == 'reverse':
        glyph = ~ink_patch
        glyph_ratio = float(glyph[patch_category].mean()) if patch_category.any() else 0.0
        entry.update({'glyph_ratio': round(glyph_ratio, 4), 'glyph_components': _count(glyph & patch_category, 2)})
        preserved = bool(glyph_ratio > 0.004 and entry['glyph_components'] >= 2)
        entry['preserved'] = preserved
        entry['loss'] = '' if preserved else '反白笔画消失或整块变成实黑'
    elif kind == 'solid':
        # 纯色块只需要保持实黑，本来就没有可丢失的层次或笔画。
        fill = float(patch_ink.mean()) if patch_ink.size else 1.0
        entry['fill_ratio'] = round(fill, 4)
        preserved = fill >= 0.95
        entry['preserved'] = preserved
        entry['loss'] = '' if preserved else '纯色块出现白缝或抖动'
    else:
        preserved = bool(float(patch_ink.mean()) < 0.005)
        entry['preserved'] = preserved
        entry['loss'] = '' if preserved else '空白区出现墨点'
    return entry


def _regions(categories, panels, zones_used, ink, source, cell, scale):
    structure = (categories == TEXT) | (categories == LINE)
    results = []
    for box in _cluster_boxes(structure, max(8, cell // 2)):
        piece = categories[box[1]:box[1] + box[3], box[0]:box[0] + box[2]]
        if np.any(piece == LINE) and not np.any(piece == TEXT):
            kind = 'line'
        else:
            kind = 'text'
        results.append(_region_stats(kind, box, ink, source, categories, scale))
    tone = categories == TONE
    if tone.any():
        for box in _cluster_boxes(tone, max(8, cell // 2), limit=20):
            results.append(_region_stats('illustration', box, ink, source, categories, scale))
    for panel in panels:
        results.append(_region_stats(panel['kind'], panel['rect'], ink, source, categories, scale))
    manual = []
    for zone in zones_used:
        if zone['kind'] == 'auto':
            continue
        entry = _region_stats(zone['kind'], zone['rect'], ink, source, categories, scale)
        entry['source'] = 'manual'
        manual.append(entry)
    auto = [entry for entry in results if entry['rect'] not in [zone['rect'] for zone in manual]]
    for entry in auto:
        entry['source'] = entry.get('source', 'auto')
    return _dedupe(manual + auto)


def _dedupe(entries):
    """同种区域且几乎完全重合时只保留一条，避免清单里出现重复边。"""
    kept = []
    for entry in entries:
        duplicate = False
        for existing in kept:
            if existing['kind'] != entry['kind']:
                continue
            if _overlap_rect(existing['rect'], entry['rect']) > 0.85 * entry['rect'][2] * entry['rect'][3]:
                duplicate = True
                if entry.get('source') == 'manual' and existing.get('source') != 'manual':
                    kept[kept.index(existing)] = entry
                break
        if not duplicate:
            kept.append(entry)
    return kept


def _overlap_rect(first, second):
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    return max(0, right - left) * max(0, bottom - top)


def _warnings(metrics, boundary, regions, config, pattern_info):
    warnings = []
    if metrics.get('structure_specks', 0) > max(4, metrics.get('structure_components', 1) * 0.2):
        warnings.append('文字/线稿区碎点较多，细字、标点或表格线可能断开；可降低输出比例或把该区改判为插图。')
    if metrics.get('structure_median_height_px', 99) < 6:
        warnings.append('文字平均高度不足 6 像素，输出尺寸偏小，标点与细笔难以辨认；请提高输出比例。')
    if metrics.get('tone_levels', 0) < 3 and metrics.get('tone_pixels', 0) > 0:
        warnings.append('插图区层次不足 3 级，可能被压平；请检查插图归属或增大网点单元。')
    if pattern_info['cell_mm'] < 0.12:
        warnings.append('网点单元小于 0.12mm，印刷时可能糊成灰；请增大网点单元或降低输出比例。')
    if pattern_info['cell_mm'] > 1.2:
        warnings.append('网点单元大于 1.2mm，插图颗粒明显；请减小网点单元。')
    if metrics['paper_ink_ratio'] > 0.004:
        warnings.append('空白纸面出现墨点，请检查分区边界或阈值。')
    if not boundary['stable']:
        warnings.append('不同内容交接处出现底色跳变或重复描边；请检查相邻分区归属后再确认。')
    lost = [region for region in regions if region.get('preserved') is False]
    if lost:
        warnings.append(f'{len(lost)} 个区域可能丢失细节，请在区域清单中核对并手工改判。')
    return warnings


def render(image, values=None, manual=None):
    """从连续图像渲染黑白结果，返回 (布尔墨点图, 报告)。"""
    validate_image(image)
    require(not is_binary(image), '当前图像已只有黑白两色，请从原图或连续稿重新渲染，避免反复二值化')
    config = settings({key: value for key, value in (values or {}).items() if key != 'zones'})
    canvas = zones((values or {}).get('zones') if manual is None else manual, image.shape[1], image.shape[0])
    source_height, source_width = image.shape[:2]
    scale = config['scale']
    target_width = max(32, round(source_width * scale))
    target_height = max(32, round(source_height * scale))
    require(target_width * target_height <= 36_000_000, '黑白输出像素过多，请降低比例')
    work = image
    if (target_height, target_width) != (source_height, source_width):
        work = cv2.resize(image, (target_width, target_height), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    validate_image(work)
    height, width = work.shape[:2]
    # 人工分区在源稿坐标中确认；输出缩放后按同一比例映射到输出坐标。
    zones_used = [{'kind': zone['kind'], 'rect': _scaled_rect(zone['rect'], scale, width, height)} for zone in canvas]
    source = normalized(gray(work))
    cell = max(16, min(48, int(round(min(height, width) / 42))))
    content = _clean_mask(source < 238, max(2, int(round(scale * 2))))
    dark = _clean_mask(source < 160, 1)
    records, _, _ = _component_records(content, source, cell)
    records = [record for record in records if record['area'] >= 3]
    categories, panels = _category_map((height, width), records, dark, cell)
    categories = _mark_lines(categories, records)
    manual_panels = _apply_zones(categories, panels, zones_used, (height, width), dark)
    panels.extend(manual_panels)

    ink = _render_structure(source, categories)
    ink |= _render_tone(source, categories, config)
    ink |= _render_panels(panels, (height, width))
    ink = _finish(ink, categories, dark, cell)

    pattern_info = pattern(config)
    regions = _regions(categories, panels, zones_used, ink, source, cell, scale)
    for region in regions:
        region['rect_output'] = list(region['rect'])
        region['rect'] = _source_rect(region['rect_output'], scale, source_width, source_height)
    boundary = boundary_check(ink, source, categories, cell)
    metrics = _metrics(config, pattern_info, ink, source, categories, regions)
    warnings = _warnings(metrics, boundary, regions, config, pattern_info)
    report = {
        'settings': config,
        'pattern': pattern_info,
        'dimensions': [width, height],
        'source_dimensions': [source_width, source_height],
        'metrics': metrics,
        'boundary': boundary,
        'warnings': warnings,
        'regions': regions,
        'zone_overrides': len([zone for zone in zones_used if zone['kind'] != 'auto']),
        'two_level': metrics['two_level'],
    }
    report['settings']['zones'] = canvas
    report['regions'].sort(key=lambda item: (item['rect'][1], item['rect'][0], item['kind']))
    return ink, report


def _clip(rect, shape):
    height, width = shape
    x = max(0, min(width - 1, int(rect[0])))
    y = max(0, min(height - 1, int(rect[1])))
    w = max(1, min(width - x, int(rect[2])))
    h = max(1, min(height - y, int(rect[3])))
    return [x, y, w, h]


def _scaled_rect(rect, scale, width, height):
    if scale == 1:
        return list(rect)
    x, y, w, h = rect
    return _clip([round(x * scale), round(y * scale), max(1, round(w * scale)), max(1, round(h * scale))], (height, width))


def _source_rect(rect, scale, width, height):
    if scale == 1:
        return list(rect)
    x, y, w, h = rect
    return _clip([round(x / scale), round(y / scale), max(1, round(w / scale)), max(1, round(h / scale))], (height, width))


def _metrics(config, pattern_info, ink, source, categories, regions):
    paper = source > 240
    structure = (categories == TEXT) | (categories == LINE)
    tone = categories == TONE
    metrics = {
        'width': ink.shape[1],
        'height': ink.shape[0],
        'scale': config['scale'],
        'levels': [0, 255],
        'two_level': bool(np.unique(ink).size <= 2),
        'ink_ratio': round(float(ink.mean()), 4),
        'paper_ink_ratio': round(float(ink[paper].mean()), 4) if paper.any() else 0.0,
        'cell_px': pattern_info['cell_px'],
        'cell_mm': pattern_info['cell_mm'],
        'output_dpi': pattern_info['output_dpi'],
        'style': pattern_info['style'],
        'label_share': {KIND_NAMES[value]: round(float((categories == value).mean()), 4) for value in (BLANK, TEXT, LINE, TONE, REVERSE, SOLID)},
    }
    if structure.any():
        count, _, stats, _ = cv2.connectedComponentsWithStats((ink & structure).astype(np.uint8), 8)
        areas = stats[1:, cv2.CC_STAT_AREA]
        metrics['structure_components'] = int(count - 1)
        metrics['structure_specks'] = int(np.sum(areas <= 2)) if areas.size else 0
        metrics['structure_stroke_min_px'] = _stroke_width(ink & structure)
        if areas.size:
            heights = stats[1:, cv2.CC_STAT_HEIGHT]
            metrics['structure_median_height_px'] = round(float(np.median(heights)), 2)
    if tone.any():
        metrics['tone_density'] = round(float(ink[tone].mean()), 4)
        metrics['tone_pixels'] = int(tone.sum())
        metrics['tone_levels'] = tone_levels(ink[tone])
    if np.any(categories == REVERSE):
        glyph = ~ink[categories == REVERSE]
        metrics['reverse_glyph_ratio'] = round(float(glyph.mean()), 4)
    lost = [region for region in regions if region.get('preserved') is False]
    metrics['lost_regions'] = len(lost)
    metrics['patterns'] = 'clustered-dot' if config['style'] == 'cluster' else 'ordered'
    return metrics


def signature(source_sha256, config, zones_used, report):
    payload = {
        'source': source_sha256,
        'settings': config,
        'zones': zones_used,
        'dimensions': report['dimensions'],
        'pattern': report['pattern'],
        'warnings': report['warnings'],
        'regions': [(item['kind'], item['rect'], item.get('preserved')) for item in report['regions']],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def encode_png(ink):
    """编码为 1 位黑白 PNG；文件只含黑白两种取值，不含灰度。"""
    image = Image.fromarray(np.where(ink, 0, 255).astype(np.uint8), mode='L')
    image = image.convert('1', dither=Image.Dither.NONE)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()
