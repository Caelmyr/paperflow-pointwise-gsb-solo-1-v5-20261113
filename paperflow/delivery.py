from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from .core import choice, identity, now, number, require
from .imaging import read_image

MAX_DELIVERY_PAGES = 300
MAX_DELIVERY_BYTES = 1024 * 1024 * 1024
MIN_DELIVERY_BYTES = 8 * 1024

FINE_LADDER = (96, 90, 84, 78, 72, 66, 60, 54, 48, 42, 36, 30)
COARSE_LADDER = (96, 84, 72, 60, 48, 36, 30)
SAMPLING_NAMES = {0: '4:4:4', 2: '4:2:0'}
SAMPLING_FACTORS = {
    0: int(cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444),
    2: int(cv2.IMWRITE_JPEG_SAMPLING_FACTOR_420),
}
PREVIEW_SIZE = (512, 384)
BAND_ROWS = 256
BAND_HALO = 8
MEMORY_MEGABYTES = int(os.environ.get('PAPERFLOW_DELIVERY_MEMORY_MB', '1200'))

STATUS_LABELS = {
    'ready': '可交付',
    'infeasible_protection': '保护档位在质量底线内无法实现',
    'infeasible_budget': '容量目标低于保护底线',
}

METRICS = {
    'text_edge_ssim': ('min', '文字边缘结构相似度'),
    'text_edge_contrast': ('min', '文字边缘对比度保留'),
    'faint_line_ssim': ('min', '浅色线结构相似度'),
    'thin_color_retention': ('min', '细彩线色度保留'),
    'thin_color_mae': ('max', '细彩线色度偏差'),
    'halo_p95': ('max', '细彩线周围色晕'),
    'gradient_ssim': ('min', '照片渐变结构相似度'),
    'gradient_mae': ('max', '照片渐变低频偏差'),
    'blockiness': ('max', '平滑区块效应'),
    'colored_chroma_mae': ('max', '彩色区色度偏差'),
    'psnr': ('min', '整体信噪比'),
}

PROTECTION = {
    'strict': {
        'label': '复核级',
        'description': '文字、细彩线和照片渐变都按最保守阈值，用于长期存档或再次复核。',
        'thresholds': {
            'text_edge_ssim': 0.983, 'text_edge_contrast': 0.960, 'faint_line_ssim': 0.955,
            'thin_color_retention': 0.975, 'thin_color_mae': 3.0, 'halo_p95': 4.0,
            'gradient_ssim': 0.992, 'gradient_mae': 0.55, 'blockiness': 0.055,
            'colored_chroma_mae': 3.0, 'psnr': 38.0,
        },
    },
    'text': {
        'label': '文字优先',
        'description': '先保住文字边缘与浅色表格线，照片与彩线允许轻微损失。',
        'thresholds': {
            'text_edge_ssim': 0.970, 'text_edge_contrast': 0.940, 'faint_line_ssim': 0.945,
            'thin_color_retention': 0.950, 'thin_color_mae': 5.0, 'halo_p95': 8.0,
            'gradient_ssim': 0.975, 'gradient_mae': 1.00, 'blockiness': 0.150,
            'colored_chroma_mae': 6.0, 'psnr': 35.0,
        },
    },
    'color': {
        'label': '细彩线优先',
        'description': '先保住细红字与浅色彩线；色度细节不足时不允许降低色度采样。',
        'thresholds': {
            'text_edge_ssim': 0.960, 'text_edge_contrast': 0.925, 'faint_line_ssim': 0.925,
            'thin_color_retention': 0.965, 'thin_color_mae': 4.0, 'halo_p95': 6.0,
            'gradient_ssim': 0.975, 'gradient_mae': 1.00, 'blockiness': 0.150,
            'colored_chroma_mae': 4.0, 'psnr': 35.0,
        },
    },
    'photo': {
        'label': '照片渐变优先',
        'description': '先保住照片渐变与平滑背景，抑制分块和带纹。',
        'thresholds': {
            'text_edge_ssim': 0.960, 'text_edge_contrast': 0.925, 'faint_line_ssim': 0.925,
            'thin_color_retention': 0.950, 'thin_color_mae': 5.0, 'halo_p95': 8.0,
            'gradient_ssim': 0.992, 'gradient_mae': 0.55, 'blockiness': 0.055,
            'colored_chroma_mae': 6.0, 'psnr': 35.0,
        },
    },
    'balanced': {
        'label': '均衡',
        'description': '三类内容都保留可见余量，是默认起点。',
        'thresholds': {
            'text_edge_ssim': 0.962, 'text_edge_contrast': 0.925, 'faint_line_ssim': 0.925,
            'thin_color_retention': 0.955, 'thin_color_mae': 5.0, 'halo_p95': 7.0,
            'gradient_ssim': 0.982, 'gradient_mae': 0.80, 'blockiness': 0.110,
            'colored_chroma_mae': 5.0, 'psnr': 34.0,
        },
    },
    'compact': {
        'label': '紧凑',
        'description': '仍为原像素彩色 JPEG；放宽到仍能辨认细彩线与浅色线的程度，适合纸质颗粒较重的扫描。',
        'thresholds': {
            'text_edge_ssim': 0.930, 'text_edge_contrast': 0.880, 'faint_line_ssim': 0.850,
            'thin_color_retention': 0.900, 'thin_color_mae': 10.0, 'halo_p95': 16.0,
            'gradient_ssim': 0.950, 'gradient_mae': 2.20, 'blockiness': 0.300,
            'colored_chroma_mae': 11.0, 'psnr': 30.0,
        },
    },
}
PROFILE_ORDER = ('strict', 'text', 'color', 'photo', 'balanced', 'compact')

GUARANTEES = [
    {'key': 'complete_pages', 'check': '交付页序必须等于文档当前完整页序；不接受漏页、替换页或删页换容量'},
    {'key': 'native_pixels', 'check': '每个候选解码后的宽高必须与页面记录及源图逐像素一致，禁止缩小尺寸'},
    {'key': 'colour_retained', 'check': '所有候选按 RGB 编码，禁止转灰度或二值化换容量'},
    {'key': 'encoded_preview', 'check': '核对窗口由实际暂存 JPEG 解码得到，不提供未压缩图像冒充成品'},
    {'key': 'confirmed_bytes', 'check': '下载读取确认时校验过的暂存 JPEG，并再次比较 SHA-256 与字节数'},
    {'key': 'source_untouched', 'check': '交付只读源图，不改动原图、处理记录、人工文字或既有导出'},
]


def human_bytes(value):
    if value is None:
        return '未知'
    if value >= 1024 * 1024:
        return f'{value / 1024 / 1024:.2f} MB'
    if value >= 1024:
        return f'{value / 1024:.1f} KB'
    return f'{value} B'


def direction(name):
    return METRICS[name][0]


def metric_label(name):
    return METRICS[name][1]


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')
    return value[:100] or 'page'


def encode_jpeg(bgr, quality, sampling):
    parameters = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality),
                  int(cv2.IMWRITE_JPEG_OPTIMIZE), 1,
                  int(cv2.IMWRITE_JPEG_SAMPLING_FACTOR), SAMPLING_FACTORS[sampling]]
    success, buffer = cv2.imencode('.jpg', bgr, parameters)
    require(success, 'JPEG 编码失败', 500)
    return buffer.tobytes()


def decode_jpeg(payload):
    image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
    require(image is not None, '候选 JPEG 无法解码', 500)
    return image


def band_palette_chroma(band_bgr, chroma):
    """Relative chroma floor so pale tints on noisy scans are not mistaken for colour detail."""
    sample = chroma[::4, ::4]
    reference = float(np.percentile(sample, 99)) if sample.size >= 64 else 0.0
    return reference * 0.35


def _dilate(mask, size):
    return cv2.dilate(mask.astype(np.uint8), np.ones((size, size), np.uint8)).astype(bool)


def _erode(mask, size):
    return cv2.erode(mask.astype(np.uint8), np.ones((size, size), np.uint8)).astype(bool)


def _bands(height, rows=BAND_ROWS, halo=BAND_HALO):
    for top in range(0, height, rows):
        bottom = min(height, top + rows)
        yield top, bottom, max(0, top - halo), min(height, bottom + halo)


def _mean(values, mask):
    if float(mask.sum()) < 200:
        return None
    return round(float(values[mask].mean()), 4)


def _ratio(first, second):
    if first is None or second is None or second <= 0:
        return None
    return round(float(first / second), 4)


def _pctl(values, mask, percentile):
    if float(mask.sum()) < 200:
        return None
    return round(float(np.percentile(values[mask], percentile)), 4)


class PageProfile:
    """源图分析与逐候选测量，均按原像素尺度、分带处理以限制内存。"""

    def __init__(self, bgr):
        self.bgr = bgr
        height, width = bgr.shape[:2]
        self.shape = (height, width)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        self.gray = gray

        # 第一遍：梯度分布，用于固定强边缘阈值
        histogram = np.zeros(1025, dtype=np.int64)
        for top, bottom, low, high in _bands(height, rows=512, halo=4):
            band = gray[low:high].astype(np.float32)
            gradient = cv2.magnitude(cv2.Sobel(band, cv2.CV_32F, 1, 0, ksize=3),
                                     cv2.Sobel(band, cv2.CV_32F, 0, 1, ksize=3))
            scaled = np.clip(gradient, 0, 1024).astype(np.int32)
            counts = np.bincount(scaled.ravel(), minlength=1025)
            histogram += counts
            del band, gradient, scaled
        total = max(1, int(histogram.sum()))
        cumulative = np.cumsum(histogram)
        peak = float(np.searchsorted(cumulative, total * 0.995))
        self.peak_gradient = peak
        self.strong_threshold = max(18.0, peak * 0.35)

        # 第二遍：建立整页掩膜（布尔，约 1 字节/像素）
        self.strong = np.zeros((height, width), bool)
        self.faint = np.zeros((height, width), bool)
        self.flat = np.zeros((height, width), bool)
        self.coloured = np.zeros((height, width), bool)
        for top, bottom, low, high in _bands(height):
            band = gray[low:high].astype(np.float32)
            gradient = cv2.magnitude(cv2.Sobel(band, cv2.CV_32F, 1, 0, ksize=3),
                                     cv2.Sobel(band, cv2.CV_32F, 0, 1, ksize=3))
            strong = gradient >= self.strong_threshold
            strong = _dilate(strong, 3)
            faint = _dilate((gradient >= 4.0) & ~strong, 3) & ~strong
            local_mean = cv2.blur(band, (7, 7))
            local_square = cv2.blur(band * band, (7, 7))
            variance = np.maximum(local_square - local_mean * local_mean, 0.0)
            flat = _erode((variance < 1.5) & (gradient < 3.0), 9)
            lab = cv2.cvtColor(bgr[low:high], cv2.COLOR_BGR2LAB).astype(np.float32)
            chroma = cv2.magnitude(lab[:, :, 1] - 128.0, lab[:, :, 2] - 128.0)
            coloured = (chroma >= max(14.0, band_palette_chroma(bgr[low:high], chroma))) & (gradient >= 6.0)
            rows = bottom - top
            offset = top - low
            self.strong[top:bottom] = strong[offset:offset + rows]
            self.faint[top:bottom] = faint[offset:offset + rows]
            self.flat[top:bottom] = flat[offset:offset + rows]
            self.coloured[top:bottom] = coloured[offset:offset + rows]
            del band, gradient, strong, faint, local_mean, local_square, variance, flat, lab, chroma, coloured

        grown = _dilate(self.coloured, 2)
        distance = cv2.distanceTransform(grown.astype(np.uint8), cv2.DIST_L2, 3)
        self.thin = grown & (distance <= 2.0)
        self.halo = _dilate(grown, 11) & ~_dilate(grown, 3) & ~self.coloured
        self.gray_float = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
        self.source_edge_gradient = (float(self._gradient_mean(self.strong))
                                     if int(self.strong.sum()) >= 200 else 0.0)
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
        self.chroma = cv2.magnitude(lab[:, :, 1] - 128.0, lab[:, :, 2] - 128.0)
        del lab
        self.counts = {
            'text_edges': int(self.strong.sum()),
            'faint_lines': int(self.faint.sum()),
            'thin_color': int(self.thin.sum()),
            'colored': int(self.coloured.sum()),
            'halo': int(self.halo.sum()),
            'gradient': int(self.flat.sum()),
        }
        self.color_floor = max(400, int(0.0005 * height * width))
        self.has_color_detail = self.counts['thin_color'] >= self.color_floor
        self.regions = {
            'text': self._window(self.strong) if self.counts['text_edges'] >= 400 else None,
            'color': self._window(_dilate(self.coloured, 2)) if self.has_color_detail else None,
            'gradient': self._window(self.flat) if self.counts['gradient'] >= 20_000 else None,
        }
        if self.counts['thin_color'] >= max(400, int(0.0005 * height * width)):
            self.kind = 'color'
        elif self.counts['gradient'] >= 120_000 and self.counts['text_edges'] < 80_000:
            self.kind = 'photo'
        elif self.counts['text_edges'] >= 400:
            self.kind = 'text'
        else:
            self.kind = 'plain'

    def _window(self, mask):
        height, width = mask.shape
        window_width = min(PREVIEW_SIZE[0], width)
        window_height = min(PREVIEW_SIZE[1], height)
        scale = max(1, (max(width, height) + 799) // 800)
        if scale > 1:
            small = cv2.resize(mask.astype(np.float32),
                               (max(1, width // scale), max(1, height // scale)),
                               interpolation=cv2.INTER_AREA) > 0.30
        else:
            small = mask
        small_height, small_width = small.shape
        sample_width = max(1, window_width // scale)
        sample_height = max(1, window_height // scale)
        integral = cv2.integral(small.astype(np.float32)) / scale ** 2
        best = (-1.0, 0, 0)
        step_x = max(8, sample_width // 6)
        step_y = max(8, sample_height // 6)
        for y in range(0, max(1, small_height - sample_height + 1), step_y):
            high = integral[min(y + sample_height, small_height)]
            low = integral[y]
            for x in range(0, max(1, small_width - sample_width + 1), step_x):
                right = min(x + sample_width, small_width)
                value = high[right] - high[x] - low[right] + low[x]
                if value > best[0]:
                    best = (value, x, y)
        x = min(max(0, best[1] * scale), width - window_width)
        y = min(max(0, best[2] * scale), height - window_height)
        return [int(x), int(y), int(window_width), int(window_height)]

    def applicable(self):
        keys = ['text_edge_ssim', 'text_edge_contrast', 'faint_line_ssim',
                'gradient_ssim', 'gradient_mae', 'blockiness', 'psnr']
        if self.counts['text_edges'] < 400:
            keys.remove('text_edge_ssim')
            keys.remove('text_edge_contrast')
        if self.counts['faint_lines'] < 400:
            keys.remove('faint_line_ssim')
        if self.counts['gradient'] < 20_000:
            keys.remove('gradient_ssim')
            keys.remove('gradient_mae')
            keys.remove('blockiness')
        if self.counts['thin_color'] >= max(400, int(0.0005 * self.shape[0] * self.shape[1])):
            keys += ['thin_color_retention', 'thin_color_mae', 'halo_p95', 'colored_chroma_mae']
        return keys

    def measure(self, decoded_bgr):
        require(decoded_bgr.shape[:2] == self.shape, '候选解码尺寸与原像素不一致')
        height, width = self.shape
        sums = {name: 0.0 for name in (
            'strong_ssim', 'strong_gradient', 'strong_smoothed_source', 'strong_smoothed_decoded', 'faint_ssim', 'flat_ssim',
            'flat_difference', 'gradient_low_frequency', 'thin_chroma', 'thin_abs', 'thin_source_chroma',
            'coloured_abs', 'square_error', 'pixels')}
        halo_excess = []
        block = {'inside': 0.0, 'border': 0.0, 'count': 0}
        for top, bottom, low, high in _bands(height):
            source_gray = self.gray_float[low:high]
            decoded_band = decoded_bgr[low:high]
            decoded_gray = cv2.cvtColor(decoded_band, cv2.COLOR_BGR2GRAY).astype(np.float32)
            ssim = _ssim_map(source_gray, decoded_gray)
            decoded_gradient = cv2.magnitude(cv2.Sobel(decoded_gray, cv2.CV_32F, 1, 0, ksize=3),
                                             cv2.Sobel(decoded_gray, cv2.CV_32F, 0, 1, ksize=3))
            smoothed_source_gradient = cv2.magnitude(cv2.Sobel(cv2.GaussianBlur(source_gray, (0, 0), 1.2), cv2.CV_32F, 1, 0, ksize=3),
                                                     cv2.Sobel(cv2.GaussianBlur(source_gray, (0, 0), 1.2), cv2.CV_32F, 0, 1, ksize=3))
            smoothed_decoded_gradient = cv2.magnitude(cv2.Sobel(cv2.GaussianBlur(decoded_gray, (0, 0), 1.2), cv2.CV_32F, 1, 0, ksize=3),
                                                      cv2.Sobel(cv2.GaussianBlur(decoded_gray, (0, 0), 1.2), cv2.CV_32F, 0, 1, ksize=3))
            decoded_lab = cv2.cvtColor(decoded_band, cv2.COLOR_BGR2LAB).astype(np.float32)
            source_chroma = self.chroma[low:high]
            decoded_chroma = cv2.magnitude(decoded_lab[:, :, 1] - 128.0, decoded_lab[:, :, 2] - 128.0)
            difference = np.abs(decoded_band.astype(np.int16) - self.bgr[low:high].astype(np.int16)).astype(np.float32)
            rows = bottom - top
            offset = top - low
            crop = slice(offset, offset + rows)
            strong = self.strong[top:bottom]
            faint = self.faint[top:bottom]
            flat = self.flat[top:bottom]
            thin = self.thin[top:bottom]
            halo = self.halo[top:bottom]
            coloured = self.coloured[top:bottom]
            sums['strong_ssim'] += float(ssim[crop][strong].sum())
            sums['strong_gradient'] += float(decoded_gradient[crop][strong].sum())
            sums['strong_smoothed_source'] += float(smoothed_source_gradient[crop][strong].sum())
            sums['strong_smoothed_decoded'] += float(smoothed_decoded_gradient[crop][strong].sum())
            sums['faint_ssim'] += float(ssim[crop][faint].sum())
            sums['flat_ssim'] += float(ssim[crop][flat].sum())
            sums['flat_difference'] += float(difference.mean(axis=2)[crop][flat].sum())
            smoothed_source = cv2.GaussianBlur(source_gray, (0, 0), 5.0)
            smoothed_decoded = cv2.GaussianBlur(decoded_gray, (0, 0), 5.0)
            sums['gradient_low_frequency'] += float(np.abs(smoothed_decoded - smoothed_source)[crop][flat].sum())
            del smoothed_source, smoothed_decoded
            if thin.any():
                sums['thin_chroma'] += float(decoded_chroma[crop][thin].sum())
                sums['thin_source_chroma'] += float(source_chroma[crop][thin].sum())
                sums['thin_abs'] += float(np.abs(decoded_chroma - source_chroma)[crop][thin].sum())
            if coloured.any():
                sums['coloured_abs'] += float(np.abs(decoded_chroma - source_chroma)[crop][coloured].sum())
            if halo.any():
                halo_excess.append((np.maximum(0.0, decoded_chroma - source_chroma)[crop][halo]).copy())
            sums['square_error'] += float((difference ** 2).sum())
            sums['pixels'] += float(difference.shape[0] * difference.shape[1] * difference.shape[2])
            if flat.any():
                steps = np.abs(np.diff(decoded_gray, axis=1))
                border = (np.arange(steps.shape[1]) % 8) == 7
                usable = flat[:, :-1] & flat[:, 1:]
                if usable.any():
                    block_rows = steps[crop]
                    block['inside'] += float(block_rows[usable].sum())
                    block['border'] += float(block_rows[np.broadcast_to(border, usable.shape) & usable].sum())
                    block['count'] += int(usable.sum())
                    del block_rows
            del source_gray, decoded_gray, ssim, decoded_gradient, decoded_lab
            del smoothed_source_gradient, smoothed_decoded_gradient
            del source_chroma, decoded_chroma, difference

        def masked(name, key):
            count = float(getattr(self, key).sum())
            return round(sums[name] / count, 4) if count >= 200 else None

        metrics = {
            'text_edge_ssim': masked('strong_ssim', 'strong'),
            'text_edge_contrast': _ratio(masked('strong_smoothed_decoded', 'strong'),
                                         masked('strong_smoothed_source', 'strong')),
            'faint_line_ssim': masked('faint_ssim', 'faint'),
            'thin_color_retention': _ratio(masked('thin_chroma', 'thin'), masked('thin_source_chroma', 'thin')),
            'thin_color_mae': masked('thin_abs', 'thin'),
            'halo_p95': self._halo_p95(halo_excess),
            'gradient_ssim': masked('flat_ssim', 'flat'),
            'gradient_mae': masked('gradient_low_frequency', 'flat'),
            'blockiness': self._blockiness(block),
            'colored_chroma_mae': masked('coloured_abs', 'coloured'),
            'psnr': round(10.0 * float(np.log10(255.0 ** 2 / max(sums['square_error'] / max(sums['pixels'], 1), 1e-9))), 4)
                      if sums['pixels'] else None,
        }
        return metrics

    def _gradient_mean(self, mask):
        if float(mask.sum()) < 200:
            return 0.0
        total = 0.0
        for top, bottom, low, high in _bands(self.shape[0]):
            band = self.gray[low:high].astype(np.float32)
            gradient = cv2.magnitude(cv2.Sobel(band, cv2.CV_32F, 1, 0, ksize=3),
                                     cv2.Sobel(band, cv2.CV_32F, 0, 1, ksize=3))
            rows = bottom - top
            total += float(gradient[top - low:top - low + rows][mask[top:bottom]].sum())
        return total / float(mask.sum())

    def _halo_p95(self, chunks):
        if self.counts['halo'] < 200 or not chunks:
            return None
        values = np.concatenate(chunks)
        return round(float(np.percentile(values, 95)), 4)

    def _blockiness(self, block):
        if block['count'] < 2000:
            return None
        inside = block['inside'] / block['count']
        border_count = max(1, int(round(block['count'] / 7)))
        on_border = block['border'] / border_count
        return round(max(0.0, on_border - inside), 4)


def _ssim_map(first, second):
    c1, c2 = 6.5025, 58.5225
    mean_first = cv2.GaussianBlur(first, (0, 0), 1.5)
    mean_second = cv2.GaussianBlur(second, (0, 0), 1.5)
    variance_first = cv2.GaussianBlur(first * first, (0, 0), 1.5) - mean_first * mean_first
    variance_second = cv2.GaussianBlur(second * second, (0, 0), 1.5) - mean_second * mean_second
    covariance = cv2.GaussianBlur(first * second, (0, 0), 1.5) - mean_first * mean_second
    numerator = (2 * mean_first * mean_second + c1) * (2 * covariance + c2)
    denominator = (mean_first * mean_first + mean_second * mean_second + c1) * (variance_first + variance_second + c2)
    return numerator / np.maximum(denominator, 1e-9)


BOUNDED_MINIMUMS = {'text_edge_ssim', 'text_edge_contrast', 'faint_line_ssim',
                    'thin_color_retention', 'gradient_ssim'}


def evaluate(metrics, thresholds, keys):
    """Return failures, worst normalised headroom and binding metric.

    Headroom 1.0 means a full metric range of slack; 0.0 means exactly at the
    threshold; negative means the threshold is violated.
    """
    failures = []
    margin = 9.99
    binding = None
    for name in keys:
        expected = thresholds[name]
        value = metrics.get(name)
        if value is None:
            continue
        if direction(name) == 'min':
            if name in BOUNDED_MINIMUMS:
                scale = max(1e-6, 1.0 - expected)
            else:
                scale = max(1e-6, expected)
            headroom = (value - expected) / scale
            failed = value < expected
        else:
            headroom = (expected - value) / max(1e-6, expected)
            failed = value > expected
        headroom = round(float(headroom), 4)
        if headroom < margin:
            margin = max(0.0, headroom)
            binding = name
        if failed:
            failures.append({'metric': name, 'label': metric_label(name), 'value': value,
                             'threshold': expected, 'direction': direction(name),
                             'headroom': headroom})
    return failures, round(margin, 4), binding


class Delivery:
    def __init__(self, store, exporter):
        self.store = store
        self.exporter = exporter
        (store.root / 'deliveries').mkdir(exist_ok=True)
        self.memory_lock = threading.Lock()
        self.cache_lock = threading.Lock()
        self.memory_used = 0

    # ---------------------------------------------------------------- input

    def _pages(self, document_id):
        document, pages = self.exporter.select(document_id, None)
        require(bool(pages), '文档没有可交付页面')
        require(len(pages) <= MAX_DELIVERY_PAGES,
                f'一次 JPEG 交付最多 {MAX_DELIVERY_PAGES} 页；本批 {len(pages)} 页。'
                '请先用「拆分」把文档按交付批次分开，而不是删页或漏页来降低页数', 409)
        return document, pages

    def fingerprint(self, document, pages):
        payload = json.dumps({
            'document': document['id'],
            'order': [page['id'] for page in pages],
            'pages': [{
                'id': page['id'], 'name': page['name'], 'current': page['current'],
                'revision': page['revision'], 'width': page['width'], 'height': page['height'],
                'bytes': self.store.resolve(page['current']).stat().st_size,
            } for page in pages],
        }, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(payload).hexdigest()

    def options_fingerprint(self, options):
        return hashlib.sha256(json.dumps(options, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode('utf-8')).hexdigest()

    def validate_options(self, options, pages):
        require(isinstance(options, dict), '交付选项必须为对象')
        budget = options.get('budget_bytes')
        require(isinstance(budget, int) and not isinstance(budget, bool), '总容量必须为整数字节')
        require(MIN_DELIVERY_BYTES <= budget <= MAX_DELIVERY_BYTES, '总容量必须在 8KB 到 1GB 之间')
        profile = choice(options.get('profile', 'balanced'), '保护档位', PROFILE_ORDER)
        floor_quality = number(options.get('floor_quality', 30), '质量底线', 30, 96, True)
        require(floor_quality in FINE_LADDER, '质量底线必须取 30–96 的档位')
        chroma = choice(options.get('chroma_sampling', 'guard'), '色度采样策略', ('guard', '4:4:4'))
        ladder = choice(options.get('quality_ladder', 'auto'), '质量档位密度', ('auto', 'fine', 'coarse'))
        defaults = options.get('page_profiles') or {}
        require(isinstance(defaults, dict), '逐页保护设置必须为对象')
        page_ids = {page['id'] for page in pages}
        require(set(defaults).issubset(page_ids), '逐页保护设置包含不属于本批的页面')
        overrides = {}
        for key, value in defaults.items():
            if isinstance(value, dict):
                require(set(value).issubset(set(PROFILE_ORDER) | {'profile', 'floor_quality', 'chroma_sampling',
                                                                  'ceiling_quality'}),
                        '逐页保护设置字段无效')
                entry = {}
                if 'profile' in value:
                    entry['profile'] = choice(value['profile'], '逐页保护档位', PROFILE_ORDER)
                if 'floor_quality' in value:
                    entry['floor_quality'] = number(value['floor_quality'], '逐页质量底线', 30, 96, True)
                    require(entry['floor_quality'] in FINE_LADDER, '逐页质量底线必须取 30–96 的档位')
                if 'chroma_sampling' in value:
                    entry['chroma_sampling'] = choice(value['chroma_sampling'], '逐页色度采样策略', ('guard', '4:4:4'))
                if 'ceiling_quality' in value:
                    entry['ceiling_quality'] = number(value['ceiling_quality'], '逐页上限质量', 30, 96, True)
                    require(entry['ceiling_quality'] in FINE_LADDER, '逐页上限质量必须取 30–96 的档位')
                    require(entry['ceiling_quality'] >= entry.get('floor_quality', floor_quality),
                            '逐页上限质量不能低于该页质量底线')
                overrides[key] = entry
            else:
                overrides[key] = {'profile': choice(value, '逐页保护档位', PROFILE_ORDER)}
        return {
            'budget_bytes': budget,
            'profile': profile,
            'floor_quality': floor_quality,
            'chroma_sampling': chroma,
            'quality_ladder': ladder,
            'page_profiles': overrides,
        }

    def _page_options(self, options, page):
        entry = options['page_profiles'].get(page['id'], {})
        return (entry.get('profile', options['profile']),
                entry.get('floor_quality', options['floor_quality']),
                entry.get('chroma_sampling', options['chroma_sampling']),
                entry.get('ceiling_quality', 96))

    def _ladder(self, options, page_count, largest_pixels):
        mode = options['quality_ladder']
        if mode == 'fine' or (mode == 'auto' and page_count <= 40 and largest_pixels <= 12_000_000):
            return FINE_LADDER
        return COARSE_LADDER

    # --------------------------------------------------------- page analysis

    def _analyze_page(self, number, page, profile_name, floor_quality, chroma_policy,
                      ceiling_quality, ladder, token, cache):
        try:
            cv2.setNumThreads(1)
            path = self.store.resolve(page['current'])
            payload_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            cache_key = (payload_sha, profile_name, floor_quality, chroma_policy, ceiling_quality, ladder)
            with self.cache_lock:
                cached = cache.get(cache_key)
            if cached is not None:
                record, candidates = cached
                if number != record['number'] or page['id'] != record['page_id']:
                    record = dict(record)
                    record['page_id'] = page['id']
                    record['number'] = number
                    record['name'] = page['name']
                return record, candidates
            bgr = read_image(path)
            require((bgr.shape[1], bgr.shape[0]) == (page['width'], page['height']),
                    f'第 {number} 页记录尺寸与文件不一致', 409)
            profile = PageProfile(bgr)
            keys = profile.applicable()
            guard = profile.has_color_detail and chroma_policy == 'guard'
            samplings = (0,) if guard else (2, 0)
            candidates = []
            qualities = [quality for quality in ladder
                         if floor_quality <= quality <= ceiling_quality]
            require(bool(qualities), f'第 {number} 页在质量底线与上限之间没有可用档位')
            for sampling in samplings:
                for quality in qualities:
                    encoded = encode_jpeg(bgr, quality, sampling)
                    decoded = decode_jpeg(encoded)
                    metrics = profile.measure(decoded)
                    per_profile = {}
                    for name in PROFILE_ORDER:
                        failures, margin, binding = evaluate(metrics, PROTECTION[name]['thresholds'], keys)
                        per_profile[name] = {'passes': not failures, 'margin': margin,
                                             'binding': binding, 'failures': failures}
                    candidates.append({
                        'quality': quality,
                        'sampling': sampling,
                        'sampling_name': SAMPLING_NAMES[sampling],
                        'bytes': len(encoded),
                        'sha256': hashlib.sha256(encoded).hexdigest(),
                        'metrics': metrics,
                        'profiles': per_profile,
                    })
                    del decoded, encoded
            candidates.sort(key=lambda item: (item['quality'], -item['sampling']), reverse=True)
            record = {
                'page_id': page['id'],
                'number': number,
                'name': page['name'],
                'width': page['width'],
                'height': page['height'],
                'source_sha256': payload_sha,
                'profile': profile_name,
                'profile_label': PROTECTION[profile_name]['label'],
                'floor_quality': floor_quality,
                'ceiling_quality': ceiling_quality,
                'chroma_sampling': chroma_policy,
                'content': profile.counts,
                'color_floor': profile.color_floor,
                'has_color_detail': profile.has_color_detail,
                'content_kind': profile.kind,
                'applicable_metrics': keys,
                'regions': profile.regions,
                'ladder': [{
                    'quality': item['quality'], 'sampling': item['sampling_name'],
                    'bytes': item['bytes'], 'sha256': item['sha256'],
                    **{name: item['profiles'][name]['passes'] for name in PROFILE_ORDER},
                } for item in candidates],
                'profile_ranges': {},
            }
            for name in PROFILE_ORDER:
                passing = [item for item in candidates if item['profiles'][name]['passes']]
                record['profile_ranges'][name] = {
                    'floor': self._public_candidate(passing[-1], name) if passing else None,
                    'ceiling': self._public_candidate(passing[0], name) if passing else None,
                    'unsatisfiable': bool(not passing),
                    'lowest_quality': candidates[-1]['quality'],
                    'lowest_sampling': candidates[-1]['sampling_name'],
                    'failures_at_lowest': [] if passing else candidates[-1]['profiles'][name]['failures'],
                }
            with self.cache_lock:
                cache.setdefault(cache_key, (record, candidates))
            return record, candidates
        finally:
            token.release()

    def _public_candidate(self, candidate, profile_name):
        if candidate is None:
            return None
        detail = candidate['profiles'][profile_name]
        return {
            'quality': candidate['quality'],
            'sampling': candidate['sampling_name'],
            'bytes': candidate['bytes'],
            'sha256': candidate['sha256'],
            'margin': detail['margin'],
            'binding': detail['binding'],
            'failures': detail['failures'],
            'profile': profile_name,
        }

    # --------------------------------------------------------------- planning

    def plan(self, document_id, options=None, compared_to=None):
        options = options or {}
        document, pages = self._pages(document_id)
        if options.get('page_ids') is not None:
            require(list(options['page_ids']) == [page['id'] for page in pages],
                    '交付必须使用完整页序；不能通过漏页或替换页换取容量')
        validated = self.validate_options(options, pages)
        largest = max(page['width'] * page['height'] for page in pages)
        ladder = self._ladder(validated, len(pages), largest)
        require(bool(ladder), '质量底线内没有可用档位')
        records, internals = self._analyze_all(pages, validated, ladder)
        plan = {
            'id': identity('delivery'),
            'document_id': document_id,
            'document_name': document['name'],
            'created_at': now(),
            'status': 'ready',
            'options': validated,
            'options_fingerprint': self.options_fingerprint(validated),
            'input_fingerprint': self.fingerprint(document, pages),
            'page_ids': [page['id'] for page in pages],
            'page_count': len(pages),
            'pages': records,
            'scenarios': {},
            'confirmed': False,
            'confirmed_at': None,
            'downloads': 0,
            'last_download_at': None,
            'replan_of': compared_to,
            'invariants': GUARANTEES,
        }
        plan['scenarios'] = self._scenarios(records, validated)
        plan['allocation'] = self._allocate(plan, internals, validated)
        if plan['status'] == 'ready':
            plan['jpeg_bytes'] = self._stage(plan)
            plan['staged_at'] = now()
            plan['allocation']['selected_jpeg_bytes'] = plan['jpeg_bytes']
            plan['allocation']['headroom_bytes'] = validated['budget_bytes'] - plan['jpeg_bytes']
        else:
            plan['jpeg_bytes'] = None
        plan['comparison'] = self._comparison(plan, compared_to)
        with self.store.transaction() as state:
            state.setdefault('deliveries', {})[plan['id']] = plan
            self.store.event(state, '生成 JPEG 交付方案', plan['id'], {
                'document_id': document_id, 'status': plan['status'],
                'budget_bytes': validated['budget_bytes'], 'jpeg_bytes': plan.get('jpeg_bytes'),
                'used_pages': plan['page_ids'],
            })
        return self._public(plan)

    def _analyze_all(self, pages, validated, ladder):
        workers = min(8, max(1, os.cpu_count() or 2))
        budget_tokens = max(1, MEMORY_MEGABYTES)
        self.memory_used = 0
        pending = list(enumerate(pages))

        def tokens_for(page):
            return max(24, int(page['width'] * page['height'] * 12 / (1024 * 1024)))

        results = [None] * len(pages)
        cache = {}
        condition = threading.Condition(self.memory_lock)

        def acquire(token_count):
            with condition:
                while self.memory_used + token_count > budget_tokens:
                    condition.wait()
                self.memory_used += token_count

        def task(index, page):
            token_count = tokens_for(page)
            acquire(token_count)
            page_profile, page_floor, page_chroma, page_ceiling = self._page_options(validated, page)
            token = _Token(token_count, condition, self)
            results[index] = self._analyze_page(index + 1, page, page_profile, page_floor,
                                                page_chroma, page_ceiling, ladder, token, cache)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(task, index, page) for index, page in pending]
            for future in futures:
                future.result()
        records = [item[0] for item in results]
        internals = {item[0]['page_id']: item[1] for item in results}
        return records, internals

    def _scenarios(self, records, options):
        result = {}
        for name in PROFILE_ORDER:
            ranges = [record['profile_ranges'][name] for record in records]
            missing = sum(1 for item in ranges if item['floor'] is None)
            floor_total = None if missing else sum(item['floor']['bytes'] for item in ranges)
            ceiling_total = None if missing else sum(item['ceiling']['bytes'] for item in ranges)
            deficit = None if floor_total is None else max(0, floor_total - options['budget_bytes'])
            result[name] = {
                'profile': name,
                'label': PROTECTION[name]['label'],
                'description': PROTECTION[name]['description'],
                'floor_bytes': floor_total,
                'ceiling_bytes': ceiling_total,
                'unsatisfiable_pages': missing,
                'fits_budget': floor_total is not None and floor_total <= options['budget_bytes'],
                'deficit_bytes': deficit,
            }
        return result

    def _allocate(self, plan, internals, options):
        records = plan['pages']
        unsatisfiable = [record for record in records
                         if record['profile_ranges'][record['profile']]['floor'] is None]
        if unsatisfiable:
            plan['status'] = 'infeasible_protection'
            return {
                'reason': 'protection_unsatisfiable',
                'budget_bytes': options['budget_bytes'],
                'selected_jpeg_bytes': None, 'floor_bytes': None,
                'ceiling_bytes': None, 'deficit_bytes': None,
                'steps': [], 'blocked_pages': [], 'alternatives': [],
                'unsatisfiable_pages': [{
                    'page_id': record['page_id'], 'number': record['number'], 'name': record['name'],
                    'profile': record['profile'], 'profile_label': record['profile_label'],
                    'content_kind': record['content_kind'], 'content': record['content'],
                    'lowest_quality': record['profile_ranges'][record['profile']]['lowest_quality'],
                    'lowest_sampling': record['profile_ranges'][record['profile']]['lowest_sampling'],
                    'failures': record['profile_ranges'][record['profile']]['failures_at_lowest'],
                    'alternatives': [{
                        'profile': name, 'label': PROTECTION[name]['label'],
                        'feasible': record['profile_ranges'][name]['floor'] is not None,
                        'floor_bytes': record['profile_ranges'][name]['floor']['bytes']
                        if record['profile_ranges'][name]['floor'] else None,
                    } for name in PROFILE_ORDER if name != record['profile']],
                } for record in unsatisfiable],
                'explanation': '即使最低质量档位也有页面达不到所选保护阈值。系统不会把这些页暗中压到阈值以下，'
                               '也不会缩小或转灰度；请调整保护档位或质量底线后重新比较。',
            }
        entries = []
        for record in records:
            passing = [item for item in internals[record['page_id']]
                       if item['profiles'][record['profile']]['passes']]
            entries.append({'record': record, 'passing': passing, 'index': 0})
        ceiling = sum(entry['passing'][0]['bytes'] for entry in entries)
        floor = sum(entry['passing'][-1]['bytes'] for entry in entries)
        budget = options['budget_bytes']
        current = ceiling
        steps = []
        while current > budget:
            best = None
            for entry in entries:
                if entry['index'] + 1 >= len(entry['passing']):
                    continue
                now_item = entry['passing'][entry['index']]
                next_item = entry['passing'][entry['index'] + 1]
                saved = now_item['bytes'] - next_item['bytes']
                if saved <= 0:
                    continue
                # Level the batch: reduce the page currently at the highest quality
                # first, so identical pages end up on the same rung instead of one
                # being driven to its floor while others stay at maximum.
                key = (now_item['quality'], saved)
                if best is None or key > best['key']:
                    best = {'entry': entry, 'key': key, 'saved': saved,
                            'from': now_item, 'to': next_item}
            if best is None:
                break
            entry = best['entry']
            entry['index'] += 1
            current -= best['saved']
            record = entry['record']
            steps.append({
                'page_id': record['page_id'], 'number': record['number'], 'name': record['name'],
                'profile': record['profile'], 'content_kind': record['content_kind'],
                'from_quality': best['from']['quality'], 'to_quality': best['to']['quality'],
                'from_sampling': best['from']['sampling_name'], 'to_sampling': best['to']['sampling_name'],
                'bytes_saved': best['saved'],
                'margin_after': best['to']['profiles'][record['profile']]['margin'],
                'binding_after': best['to']['profiles'][record['profile']]['binding'],
            })
        if current > budget:
            plan['status'] = 'infeasible_budget'
            blocked = []
            for entry in entries:
                record = entry['record']
                chosen = entry['passing'][-1]
                detail = chosen['profiles'][record['profile']]
                blocked.append({
                    'page_id': record['page_id'], 'number': record['number'], 'name': record['name'],
                    'profile': record['profile'], 'profile_label': record['profile_label'],
                    'content_kind': record['content_kind'], 'content': record['content'],
                    'floor_quality': chosen['quality'], 'floor_sampling': chosen['sampling_name'],
                    'floor_bytes': chosen['bytes'],
                    'binding': detail['binding'],
                    'margin': detail['margin'],
                    'metrics': {key: chosen['metrics'].get(key) for key in record['applicable_metrics']},
                    'below_threshold': None,
                })
            alternatives = [scenario for name, scenario in plan['scenarios'].items()
                            if name != options['profile'] and scenario['floor_bytes'] is not None]
            alternatives.sort(key=lambda item: item['floor_bytes'])
            return {
                'reason': 'budget_below_protection_floor',
                'budget_bytes': budget, 'selected_jpeg_bytes': current,
                'floor_bytes': floor, 'ceiling_bytes': ceiling,
                'deficit_bytes': current - budget,
                'steps': steps, 'blocked_pages': blocked,
                'unsatisfiable_pages': [], 'alternatives': alternatives,
                'explanation': (
                    f'在所选保护档位与质量底线内，整批最低只能压到 {human_bytes(floor)}，'
                    f'仍比容量目标多 {human_bytes(current - budget)}。没有页面被压到各自保护下限以下；'
                    '请提高容量目标、改用更宽松的保护档位或降低质量底线后重新比较。'
                ),
            }
        for entry in entries:
            chosen = entry['passing'][entry['index']]
            entry['record']['selection'] = self._selection(entry['record'], chosen, entry['passing'])
        return {
            'reason': 'ok',
            'budget_bytes': budget, 'selected_jpeg_bytes': current,
            'floor_bytes': floor, 'ceiling_bytes': ceiling, 'deficit_bytes': 0,
            'headroom_bytes': budget - current,
            'steps': steps, 'blocked_pages': [], 'unsatisfiable_pages': [],
            'alternatives': [], 'explanation': None,
        }

    def _selection(self, record, candidate, passing):
        detail = candidate['profiles'][record['profile']]
        others = {}
        for name in PROFILE_ORDER:
            if name == record['profile']:
                continue
            failures, margin, binding = evaluate(candidate['metrics'],
                                                 PROTECTION[name]['thresholds'],
                                                 record['applicable_metrics'])
            others[name] = {'passes': not failures, 'margin': margin,
                            'binding': binding, 'failures': failures}
        return {
            'quality': candidate['quality'],
            'sampling': candidate['sampling_name'],
            'sampling_factor': candidate['sampling'],
            'bytes': candidate['bytes'],
            'sha256': candidate['sha256'],
            'metrics': candidate['metrics'],
            'margin': detail['margin'],
            'binding': detail['binding'],
            'failures': detail['failures'],
            'at_floor': (candidate['quality'] == passing[-1]['quality']
                         and candidate['sampling'] == passing[-1]['sampling']),
            'at_ceiling': (candidate['quality'] == passing[0]['quality']
                           and candidate['sampling'] == passing[0]['sampling']),
            'borderline': detail['margin'] < 0.10,
            'quality_range': [passing[-1]['quality'], passing[0]['quality']],
            'metric_margin_range': [passing[-1]['profiles'][record['profile']]['margin'],
                                    passing[0]['profiles'][record['profile']]['margin']],
            'other_profiles': others,
        }

    def _stage(self, plan):
        directory = self.store.root / 'deliveries' / plan['id']
        directory.mkdir(parents=True, exist_ok=True)
        state = self.store.snapshot()
        payloads = {}
        for record in plan['pages']:
            selection = record['selection']
            bgr = read_image(self.store.resolve(state['pages'][record['page_id']]['current']))
            payload = encode_jpeg(bgr, selection['quality'], selection['sampling_factor'])
            require(hashlib.sha256(payload).hexdigest() == selection['sha256'],
                    f'第 {record["number"]} 页重编码结果与评估结果不一致，请重新生成方案', 409)
            path = directory / f'{record["number"]:04d}.jpg'
            path.write_bytes(payload)
            payloads[record['page_id']] = (path, len(payload))
        total = 0
        for record in plan['pages']:
            path, size = payloads[record['page_id']]
            record['selection']['staged'] = self.store.relative(path)
            record['selection']['bytes'] = size
            total += size
        return total

    def _comparison(self, plan, compared_to):
        if not compared_to:
            return None
        previous = self.store.snapshot().get('deliveries', {}).get(compared_to)
        if not previous:
            return {'previous_plan_id': compared_to, 'available': False}
        before = {page['page_id']: page for page in previous['pages']}
        rows = []
        for page in plan['pages']:
            old = before.get(page['page_id'])
            earlier = self._comparable(old) if old else None
            current = self._comparable(page)
            rows.append({
                'page_id': page['page_id'], 'number': page['number'], 'name': page['name'],
                'before_quality': earlier['quality'] if earlier else None,
                'after_quality': current['quality'] if current else None,
                'before_sampling': earlier['sampling'] if earlier else None,
                'after_sampling': current['sampling'] if current else None,
                'before_bytes': earlier['bytes'] if earlier else None,
                'after_bytes': current['bytes'] if current else None,
                'before_margin': earlier['margin'] if earlier else None,
                'after_margin': current['margin'] if current else None,
                'before_at_floor': earlier['at_floor'] if earlier else None,
                'after_at_floor': current['at_floor'] if current else None,
                'before_at_ceiling': earlier['at_ceiling'] if earlier else None,
                'after_at_ceiling': current['at_ceiling'] if current else None,
                'input_changed': old is not None and old['source_sha256'] != page['source_sha256'],
                'added': old is None,
            })
        before_ids = {page['page_id'] for page in plan['pages']}
        removed = []
        for page in previous['pages']:
            if page['page_id'] in before_ids:
                continue
            earlier = self._comparable(page)
            removed.append({'page_id': page['page_id'], 'name': page['name'],
                            'before_quality': earlier['quality'] if earlier else None,
                            'before_bytes': earlier['bytes'] if earlier else None})
        return {
            'previous_plan_id': compared_to,
            'available': True,
            'input_changed': previous['input_fingerprint'] != plan['input_fingerprint'],
            'before_status': previous['status'],
            'after_status': plan['status'],
            'budget': {'before': previous['options']['budget_bytes'],
                       'after': plan['options']['budget_bytes'],
                       'changed': previous['options']['budget_bytes'] != plan['options']['budget_bytes']},
            'protection': {'before': previous['options']['profile'], 'after': plan['options']['profile'],
                           'changed': previous['options']['profile'] != plan['options']['profile']},
            'floor_quality': {'before': previous['options']['floor_quality'],
                              'after': plan['options']['floor_quality'],
                              'changed': previous['options']['floor_quality'] != plan['options']['floor_quality']},
            'chroma_sampling': {'before': previous['options']['chroma_sampling'],
                                'after': plan['options']['chroma_sampling'],
                                'changed': previous['options']['chroma_sampling'] != plan['options']['chroma_sampling']},
            'page_profiles_changed': previous['options']['page_profiles'] != plan['options']['page_profiles'],
            'jpeg_bytes': {'before': previous.get('jpeg_bytes'), 'after': plan.get('jpeg_bytes')},
            'deficit_bytes': {'before': (previous.get('allocation') or {}).get('deficit_bytes'),
                              'after': (plan.get('allocation') or {}).get('deficit_bytes')},
            'pages': rows,
            'removed_pages': removed,
            'now_at_floor': [page['number'] for page in plan['pages']
                             if self._comparable(page) and self._comparable(page).get('at_floor')],
            'using_floor_estimates': any(
                page.get('selection') is None and self._comparable(page) is not None
                for page in plan['pages']) or any(
                previous_page.get('selection') is None
                for previous_page in previous['pages']),
        }

    def _comparable(self, record):
        """Selection if the plan delivered; otherwise the page's protection floor
        so comparisons still show what each side would have cost."""
        if record is None:
            return None
        selection = record.get('selection')
        if selection:
            return selection
        floor = record['profile_ranges'][record['profile']]['floor']
        if not floor:
            return None
        return {'quality': floor['quality'], 'sampling': floor['sampling'],
                'bytes': floor['bytes'], 'margin': floor['margin'],
                'binding': floor['binding'], 'at_floor': True, 'at_ceiling': False,
                'estimate': True}

    # ---------------------------------------------------------------- public

    def _public(self, plan):
        state = self.store.snapshot()
        public = json.loads(json.dumps(plan, ensure_ascii=False))
        for page in public['pages']:
            selection = page.get('selection')
            if selection:
                selection.pop('staged', None)
        try:
            document = state['documents'][plan['document_id']]
            pages = [state['pages'][page_id] for page_id in plan['page_ids']]
            current = self.fingerprint(document, pages)
        except Exception:
            current = None
        public['input_changed'] = current != plan['input_fingerprint']
        public['download_ready'] = bool(plan['status'] == 'ready' and plan.get('confirmed')
                                        and not public['input_changed'])
        if public['input_changed']:
            public['input_notice'] = '页面图像、页序或尺寸已变化：方案与确认均已失效，请重新生成方案并再次确认。'
        public['budget_vs_jpeg'] = {
            'budget_bytes': plan['options']['budget_bytes'],
            'jpeg_bytes': plan.get('jpeg_bytes'),
            'headroom_bytes': (plan['options']['budget_bytes'] - plan['jpeg_bytes']) if plan.get('jpeg_bytes') else None,
        }
        public['download_count'] = plan.get('downloads', 0)
        public['status_label'] = STATUS_LABELS.get(plan['status'], plan['status'])
        public['protection_profiles'] = [
            {'profile': name, 'label': PROTECTION[name]['label'],
             'description': PROTECTION[name]['description'],
             'thresholds': PROTECTION[name]['thresholds']} for name in PROFILE_ORDER]
        public['metric_labels'] = {name: metric_label(name) for name in METRICS}
        return public

    def get(self, plan_id):
        plan = self.store.snapshot().get('deliveries', {}).get(plan_id)
        require(plan is not None, '交付方案不存在', 404)
        return self._public(plan)

    def list(self, document_id=None):
        state = self.store.snapshot()
        values = list(state.get('deliveries', {}).values())
        if document_id:
            values = [value for value in values if value['document_id'] == document_id]
        values.sort(key=lambda item: item['created_at'], reverse=True)
        return [{
            'id': plan['id'], 'document_id': plan['document_id'],
            'document_name': plan['document_name'], 'created_at': plan['created_at'],
            'status': plan['status'], 'status_label': STATUS_LABELS.get(plan['status'], plan['status']),
            'profile': plan['options']['profile'], 'budget_bytes': plan['options']['budget_bytes'],
            'jpeg_bytes': plan.get('jpeg_bytes'), 'page_count': plan['page_count'],
            'confirmed': bool(plan.get('confirmed')), 'download_count': plan.get('downloads', 0),
            'replan_of': plan.get('replan_of'),
        } for plan in values]

    def confirm(self, plan_id):
        state = self.store.snapshot()
        plan = state.get('deliveries', {}).get(plan_id)
        require(plan is not None, '交付方案不存在', 404)
        require(plan['status'] == 'ready', '方案不可交付：容量或保护在当前底线下无法满足', 409)
        document = state['documents'].get(plan['document_id'])
        require(document is not None, '文档不存在', 404)
        require(plan['page_ids'] == document['page_ids'], '页序或页面已变化，请重新生成方案', 409)
        pages = [state['pages'][page_id] for page_id in plan['page_ids']]
        require(self.fingerprint(document, pages) == plan['input_fingerprint'],
                '输入版本已变化（图像、页序或尺寸不同），请重新生成并确认', 409)
        integrity = self._integrity(plan, state)
        require(integrity['valid'], '暂存编码与方案不一致：' + '；'.join(integrity['errors']), 409)
        require(plan['jpeg_bytes'] <= plan['options']['budget_bytes'], '整批 JPEG 超出容量目标', 409)
        with self.store.transaction() as candidate:
            record = candidate['deliveries'][plan_id]
            record['confirmed'] = True
            record['confirmed_at'] = now()
            record['confirmed_fingerprint'] = plan['input_fingerprint']
            record['confirmed_options_fingerprint'] = plan['options_fingerprint']
            record['confirmed_jpeg_bytes'] = plan['jpeg_bytes']
            self.store.event(candidate, '确认 JPEG 交付方案', plan_id,
                             {'jpeg_bytes': plan['jpeg_bytes'], 'pages': plan['page_count']})
        return self.get(plan_id)

    def replan(self, plan_id, options=None):
        previous = self.store.snapshot().get('deliveries', {}).get(plan_id)
        require(previous is not None, '交付方案不存在', 404)
        base = dict(previous['options'])
        base.pop('page_ids', None)
        if options:
            require(isinstance(options, dict), '调整选项必须为对象')
            base.update(options)
        return self.plan(previous['document_id'], base, compared_to=plan_id)

    # ---------------------------------------------------------------- verify

    def _integrity(self, plan, state):
        errors = []
        total = 0
        for record in plan['pages']:
            selection = record.get('selection')
            if not selection:
                errors.append(f'第 {record["number"]} 页没有选定编码')
                continue
            staged = selection.get('staged')
            path = self.store.resolve(staged) if staged else None
            if path is None or not path.is_file():
                errors.append(f'第 {record["number"]} 页暂存文件缺失')
                continue
            payload = path.read_bytes()
            if len(payload) != selection['bytes']:
                errors.append(f'第 {record["number"]} 页暂存字节数与方案不一致')
            if hashlib.sha256(payload).hexdigest() != selection['sha256']:
                errors.append(f'第 {record["number"]} 页暂存 SHA-256 与方案不一致')
            decoded = decode_jpeg(payload)
            if (decoded.shape[1], decoded.shape[0]) != (record['width'], record['height']):
                errors.append(f'第 {record["number"]} 页暂存 JPEG 尺寸与原像素不一致')
            del decoded
            if record['source_sha256'] != hashlib.sha256(
                    self.store.resolve(state['pages'][record['page_id']]['current']).read_bytes()).hexdigest():
                errors.append(f'第 {record["number"]} 页源图自方案生成后已变化')
            total += len(payload)
        if total != plan.get('jpeg_bytes'):
            errors.append('暂存 JPEG 总字节数与方案记录不一致')
        if total > plan['options']['budget_bytes']:
            errors.append('暂存 JPEG 总容量超出目标')
        return {'valid': not errors, 'errors': errors, 'jpeg_bytes': total}

    def verify(self, plan_id):
        state = self.store.snapshot()
        plan = state.get('deliveries', {}).get(plan_id)
        require(plan is not None, '交付方案不存在', 404)
        errors = []
        warnings = []
        pages = []
        total = 0
        for record in plan['pages']:
            selection = record.get('selection')
            if not selection:
                errors.append(f'第 {record["number"]} 页没有选定编码')
                pages.append({'page_id': record['page_id'], 'number': record['number'],
                              'valid': False, 'issues': ['此页没有选定编码']})
                continue
            staged = selection.get('staged')
            path = self.store.resolve(staged) if staged else None
            issues = []
            if path is None or not path.is_file():
                issues.append('暂存 JPEG 缺失')
                errors.append(f'第 {record["number"]} 页：暂存 JPEG 缺失')
                pages.append({'page_id': record['page_id'], 'number': record['number'],
                              'valid': False, 'issues': issues})
                continue
            payload = path.read_bytes()
            actual_sha = hashlib.sha256(payload).hexdigest()
            if actual_sha != selection['sha256']:
                issues.append('暂存 SHA-256 与方案不一致')
            if len(payload) != selection['bytes']:
                issues.append('暂存字节数与方案不一致')
            decoded = decode_jpeg(payload)
            if (decoded.shape[1], decoded.shape[0]) != (record['width'], record['height']):
                issues.append('解码尺寸与原像素不一致')
            source = read_image(self.store.resolve(state['pages'][record['page_id']]['current']))
            if source.shape != decoded.shape:
                issues.append('源图与暂存 JPEG 尺寸不一致')
            if issues:
                errors.extend(f'第 {record["number"]} 页：' + issue for issue in issues)
                pages.append({'page_id': record['page_id'], 'number': record['number'],
                              'valid': False, 'issues': issues})
                del decoded, source
                continue
            cv2.setNumThreads(1)
            profile = PageProfile(source)
            metrics = profile.measure(decoded)
            del decoded, source
            mismatches = []
            for key in record['applicable_metrics']:
                declared = selection['metrics'].get(key)
                recomputed = metrics.get(key)
                if declared is None or recomputed is None:
                    continue
                if abs(declared - recomputed) > max(0.002, abs(declared) * 0.01):
                    mismatches.append({'metric': key, 'label': metric_label(key),
                                       'declared': declared, 'recomputed': recomputed})
            failures, margin, binding = evaluate(metrics,
                                                 PROTECTION[record['profile']]['thresholds'],
                                                 record['applicable_metrics'])
            if failures:
                issues.append('重算指标低于保护阈值：' + '、'.join(item['label'] for item in failures))
            if mismatches:
                warnings.append(f'第 {record["number"]} 页重算指标与方案记录有差异')
            pages.append({
                'page_id': record['page_id'], 'number': record['number'], 'name': record['name'],
                'valid': not issues, 'issues': issues,
                'quality': selection['quality'], 'sampling': selection['sampling'],
                'width': record['width'], 'height': record['height'],
                'bytes': len(payload), 'sha256': actual_sha,
                'metrics': metrics, 'metric_mismatches': mismatches,
                'protection_failures': failures, 'margin': margin, 'binding': binding,
                'content_kind': record['content_kind'], 'profile': record['profile'],
            })
            total += len(payload)
        if plan['status'] != 'ready':
            errors.append('方案状态不可交付：' + STATUS_LABELS.get(plan['status'], plan['status']))
        if total != plan.get('jpeg_bytes'):
            errors.append('各页 JPEG 总字节数与方案记录不一致')
        if total > plan['options']['budget_bytes']:
            errors.append('各页 JPEG 总容量超出目标')
        for record in plan['pages']:
            selection = record.get('selection')
            if not selection:
                continue
            if plan['options']['chroma_sampling'] == '4:4:4' and selection['sampling'] != '4:4:4':
                errors.append(f'第 {record["number"]} 页降低了色度采样，违反本方案策略')
            if selection['sampling'] != '4:4:4' and record['content']['thin_color'] >= record['color_floor']:
                errors.append(f'第 {record["number"]} 页含细彩线却降低了色度采样')
        return {
            'plan_id': plan_id,
            'checked_at': now(),
            'valid': not errors,
            'errors': errors,
            'warnings': warnings,
            'status': plan['status'],
            'status_label': STATUS_LABELS.get(plan['status'], plan['status']),
            'confirmed': bool(plan.get('confirmed')),
            'jpeg_bytes': total,
            'budget_bytes': plan['options']['budget_bytes'],
            'page_count': len(pages),
            'pages': pages,
        }

    # -------------------------------------------------------------- download

    def download(self, plan_id):
        state = self.store.snapshot()
        plan = state.get('deliveries', {}).get(plan_id)
        require(plan is not None, '交付方案不存在', 404)
        require(plan['status'] == 'ready' and plan.get('confirmed'),
                '方案未确认或不可交付；请先核对 1:1 解码候选与损失指标并确认', 409)
        document = state['documents'].get(plan['document_id'])
        require(document is not None and plan['page_ids'] == document['page_ids'],
                '文档页序已变化，确认已失效；请重新生成方案', 409)
        pages = [state['pages'][page_id] for page_id in plan['page_ids']]
        current = self.fingerprint(document, pages)
        require(current == plan['input_fingerprint'] and current == plan.get('confirmed_fingerprint'),
                '输入版本已变化，确认已失效；请重新生成方案并确认后再下载', 409)
        require(plan['jpeg_bytes'] <= plan['options']['budget_bytes'],
                '交付结果超出容量目标，已拒绝下载', 409)
        integrity = self._integrity(plan, state)
        require(integrity['valid'], '交付编码校验失败：' + '；'.join(integrity['errors']), 409)
        buffer = io.BytesIO()
        manifest_pages = []
        for record in plan['pages']:
            selection = record['selection']
            manifest_pages.append({
                'number': record['number'], 'page_id': record['page_id'], 'name': record['name'],
                'file': f'{record["number"]:04d}_{safe_name(record["name"])}.jpg',
                'width': record['width'], 'height': record['height'],
                'quality': selection['quality'], 'sampling': selection['sampling'],
                'bytes': selection['bytes'], 'sha256': selection['sha256'],
                'profile': record['profile'],
                'metrics': {key: selection['metrics'].get(key)
                            for key in record['applicable_metrics']},
            })
        manifest = {
            'format': 'paperflow-jpeg-delivery',
            'schema': 1,
            'plan_id': plan_id,
            'document': {'id': plan['document_id'], 'name': plan['document_name']},
            'created_at': plan['created_at'],
            'confirmed_at': plan.get('confirmed_at'),
            'budget_bytes': plan['options']['budget_bytes'],
            'jpeg_bytes': plan['jpeg_bytes'],
            'options': plan['options'],
            'page_count': plan['page_count'],
            'pages': manifest_pages,
            'verification': {
                'encoded_preview': True,
                'decoded_from_staged': True,
                'complete_pages': True,
                'native_pixels': True,
                'colour_retained': True,
            },
        }
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for record, entry in zip(plan['pages'], manifest_pages):
                path = self.store.resolve(record['selection']['staged'])
                payload = path.read_bytes()
                require(hashlib.sha256(payload).hexdigest() == entry['sha256'],
                        f'第 {record["number"]} 页暂存编码被修改，拒绝下载', 409)
                require(len(payload) == entry['bytes'],
                        f'第 {record["number"]} 页暂存字节数被修改，拒绝下载', 409)
                archive.writestr(entry['file'], payload)
            archive.writestr('delivery.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        data = buffer.getvalue()
        filename = safe_name(plan['document_name']) + '_JPEG交付_' + plan_id.split('_', 1)[1][:8] + '.zip'
        with self.store.transaction() as candidate:
            record = candidate['deliveries'][plan_id]
            record['downloads'] = record.get('downloads', 0) + 1
            record['last_download_at'] = now()
            record['download_bytes'] = len(data)
            self.store.event(candidate, '下载 JPEG 交付', plan_id,
                             {'bytes': len(data), 'jpeg_bytes': plan['jpeg_bytes']})
        return data, filename

    # --------------------------------------------------------------- preview

    def _window_for(self, record, family, x, y):
        width, height = record['width'], record['height']
        if x is not None or y is not None:
            require(isinstance(x, int) and not isinstance(x, bool) and
                    isinstance(y, int) and not isinstance(y, bool), '裁切坐标必须为整数')
            window_width = min(PREVIEW_SIZE[0], width)
            window_height = min(PREVIEW_SIZE[1], height)
            return [max(0, min(x, width - window_width)), max(0, min(y, height - window_height)),
                    window_width, window_height]
        if family != 'page' and record['regions'].get(family):
            return record['regions'][family]
        return [0, 0, width, height]

    def _preview_pair(self, plan_id, page_id, family, x, y):
        state = self.store.snapshot()
        plan = state.get('deliveries', {}).get(plan_id)
        require(plan is not None, '交付方案不存在', 404)
        record = next((item for item in plan['pages'] if item['page_id'] == page_id), None)
        require(record is not None, '页面不在本交付方案中', 404)
        selection = record.get('selection')
        require(selection is not None, '此方案没有为该页选定编码', 409)
        path = self.store.resolve(selection['staged'])
        require(path.is_file(), '暂存 JPEG 缺失，请重新生成方案', 409)
        payload = path.read_bytes()
        require(hashlib.sha256(payload).hexdigest() == selection['sha256'],
                '暂存编码与方案确认的 SHA-256 不一致', 409)
        decoded = decode_jpeg(payload)
        source = read_image(self.store.resolve(state['pages'][page_id]['current']))
        require(decoded.shape == source.shape, '暂存 JPEG 尺寸与原像素不一致', 409)
        window = self._window_for(record, family or 'page', x, y)
        return {'plan': plan, 'record': record, 'selection': selection, 'window': window,
                'decoded': decoded, 'source': source}

    def preview(self, plan_id, page_id, family='page', x=None, y=None):
        choice(family, '检查内容', ('text', 'color', 'gradient', 'page'))
        pair = self._preview_pair(plan_id, page_id, family, x, y)
        left, top, crop_width, crop_height = pair['window']
        source_crop = pair['source'][top:top + crop_height, left:left + crop_width]
        decoded_crop = pair['decoded'][top:top + crop_height, left:left + crop_width]
        cv2.setNumThreads(1)
        window_profile = PageProfile(source_crop)
        metrics = window_profile.measure(decoded_crop)
        record = pair['record']
        selection = pair['selection']
        failures, margin, binding = evaluate(metrics, PROTECTION[record['profile']]['thresholds'],
                                             window_profile.applicable())
        return {
            'plan_id': plan_id,
            'page_id': page_id,
            'family': family,
            'window': pair['window'],
            'pixel_scale': 1.0,
            'note': '候选图为该页实际暂存 JPEG 解码后的 1:1 原像素；参考图为源图同一像素窗口，仅用于对照，不是交付内容。',
            'candidate': {
                'quality': selection['quality'],
                'sampling': selection['sampling'],
                'bytes': selection['bytes'],
                'sha256': selection['sha256'],
                'at_floor': selection['at_floor'],
                'at_ceiling': selection['at_ceiling'],
                'quality_range': selection['quality_range'],
                'margin': selection['margin'],
                'binding': selection['binding'],
                'binding_label': metric_label(selection['binding']) if selection['binding'] else None,
                'staged_sha256_matches': True,
            },
            'profile': record['profile'],
            'profile_label': record['profile_label'],
            'content_kind': record['content_kind'],
            'window_content': window_profile.counts,
            'window_metrics': metrics,
            'window_applicable': window_profile.applicable(),
            'window_failures': failures,
            'window_margin': margin,
            'window_binding': binding,
            'page_metrics': selection['metrics'],
            'page_failures': selection['failures'],
            'page_margin': selection['margin'],
            'page_binding': selection['binding'],
            'images': {
                'source': f'/api/deliveries/{plan_id}/preview-image?page_id={page_id}&family={family}&which=source&x={left}&y={top}',
                'candidate': f'/api/deliveries/{plan_id}/preview-image?page_id={page_id}&family={family}&which=candidate&x={left}&y={top}',
                'difference': f'/api/deliveries/{plan_id}/preview-image?page_id={page_id}&family={family}&which=difference&x={left}&y={top}',
            },
        }

    def preview_image(self, plan_id, page_id, which, family='page', x=None, y=None):
        choice(which, '预览图像', ('source', 'candidate', 'difference'))
        choice(family, '检查内容', ('text', 'color', 'gradient', 'page'))
        pair = self._preview_pair(plan_id, page_id, family, x, y)
        left, top, crop_width, crop_height = pair['window']
        source_crop = pair['source'][top:top + crop_height, left:left + crop_width]
        decoded_crop = pair['decoded'][top:top + crop_height, left:left + crop_width]
        if which == 'source':
            image = source_crop
        elif which == 'candidate':
            image = decoded_crop
        else:
            difference = np.abs(decoded_crop.astype(np.int16) - source_crop.astype(np.int16)).astype(np.float32)
            image = np.clip(difference * 8, 0, 255).astype(np.uint8)
        success, buffer = cv2.imencode('.png', image)
        require(success, '预览编码失败', 500)
        return buffer.tobytes(), pair['window']


class _Token:
    def __init__(self, count, condition, delivery):
        self.count = count
        self.condition = condition
        self.delivery = delivery

    def release(self):
        with self.condition:
            self.delivery.memory_used -= self.count
            self.condition.notify_all()
