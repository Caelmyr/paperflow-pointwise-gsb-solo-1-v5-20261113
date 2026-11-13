from __future__ import annotations

import math

from .core import choice, number, require


class Parameter:
    def __init__(self, default=None, required=False):
        self.default = default
        self.required = required

    def validate(self, value, field):
        return value

    def describe(self):
        return {'default': self.default, 'required': self.required, 'type': 'any'}


class Numeric(Parameter):
    def __init__(self, low, high, default=None, integer=False, odd=False, required=False):
        super().__init__(default, required)
        self.low = low
        self.high = high
        self.integer = integer
        self.odd = odd

    def validate(self, value, field):
        value = number(value, field, self.low, self.high, self.integer)
        require(math.isfinite(value), field + '必须为有限数')
        if self.odd:
            require(value % 2 == 1, field + '必须为奇数')
        return value

    def describe(self):
        result = super().describe()
        result['type'] = 'integer' if self.integer else 'number'
        result['minimum'] = self.low
        result['maximum'] = self.high
        result['odd'] = self.odd
        return result


class Enumeration(Parameter):
    def __init__(self, options, default=None, required=False):
        super().__init__(default, required)
        self.options = tuple(options)

    def validate(self, value, field):
        return choice(value, field, self.options)

    def describe(self):
        result = super().describe()
        result['type'] = 'enum'
        result['options'] = list(self.options)
        return result


class Region(Parameter):
    def validate(self, value, field):
        if value is None and not self.required:
            return None
        require(isinstance(value, list) and len(value) == 4, field + '需要[x,y,宽,高]')
        result = []
        for index, item in enumerate(value):
            minimum = 0 if index < 2 else 32
            result.append(number(item, field, minimum, 16000, True))
        return result

    def describe(self):
        result = super().describe()
        result['type'] = 'rectangle'
        return result


class Boolean(Parameter):
    def validate(self, value, field):
        require(isinstance(value, bool), field + '必须为布尔值')
        return value

    def describe(self):
        result = super().describe()
        result['type'] = 'boolean'
        return result


class Zones(Parameter):
    def validate(self, value, field):
        require(isinstance(value, (list, tuple)) and len(value) <= 200, field + '需要分区列表')
        result = []
        for item in value:
            require(isinstance(item, dict), field + '分区格式无效')
            result.append({'kind': item.get('kind'), 'rect': item.get('rect')})
        return result

    def describe(self):
        result = super().describe()
        result['type'] = 'zones'
        return result


class Corners(Parameter):
    def validate(self, value, field):
        if value is None:
            return None
        require(isinstance(value, list) and len(value) == 4, '需要四个角点')
        result = []
        for point in value:
            require(isinstance(point, list) and len(point) == 2, '角点需要[x,y]')
            result.append([number(item, '角点', 0, 16000) for item in point])
        return result

    def describe(self):
        result = super().describe()
        result['type'] = 'quadrilateral'
        return result


class Operation:
    def __init__(self, name, category, fields):
        self.name = name
        self.category = category
        self.fields = fields

    def validate(self, values):
        require(isinstance(values, dict), '操作参数必须为对象')
        unknown = set(values) - set(self.fields)
        require(not unknown, '未知参数：' + ', '.join(sorted(unknown)))
        normalized = {}
        for field, specification in self.fields.items():
            if field not in values:
                require(not specification.required, '缺少参数：' + field)
                if specification.default is not None:
                    normalized[field] = specification.default
                continue
            normalized[field] = specification.validate(values[field], field)
        return normalized

    def describe(self):
        return {
            'name': self.name,
            'category': self.category,
            'parameters': {field: specification.describe() for field, specification in self.fields.items()},
        }


def build_registry():
    registry = {}
    registry['perspective'] = Operation('透视校正', 'geometry', {
        'points': Corners(),
    })
    registry['rotate'] = Operation('旋转', 'geometry', {
        'angle': Numeric(-360, 360, 90),
    })
    registry['deskew'] = Operation('倾斜校正', 'geometry', {})
    registry['illumination'] = Operation('光照修复', 'background', {
        'strength': Numeric(0, 1, 0.7),
    })
    registry['color'] = Operation('扫描色彩', 'color', {
        'mode': Enumeration(('color', 'gray', 'binary', 'adaptive'), 'gray'),
        'threshold': Numeric(0, 255, 160, integer=True),
        'block_size': Numeric(3, 201, 35, integer=True, odd=True),
        'constant': Numeric(-50, 50, 12),
    })
    registry['mono'] = Operation('黑白分区渲染', 'color', {
        'scale': Numeric(0.1, 4, 1),
        'cell': Numeric(2, 24, 8, integer=True),
        'style': Enumeration(('cluster', 'ordered'), 'cluster'),
        'assumed_dpi': Numeric(72, 1200, 300, integer=True),
        'keep_scale': Boolean(True),
        'zones': Zones(),
    })
    registry['denoise'] = Operation('清除污点', 'cleanup', {
        'area': Numeric(0, 100, 8, integer=True),
        'median': Numeric(1, 9, 3, integer=True, odd=True),
        'region': Region(),
    })
    registry['crop'] = Operation('区域裁切', 'geometry', {
        'region': Region(required=True),
    })
    registry['enhance'] = Operation('扫描增强', 'background', {
        'strength': Numeric(0, 1, 0.6),
        'mode': Enumeration(('color', 'gray', 'binary', 'adaptive'), 'adaptive'),
        'threshold': Numeric(0, 255, 160, integer=True),
        'block_size': Numeric(3, 201, 35, integer=True, odd=True),
        'constant': Numeric(-50, 50, 12),
        'area': Numeric(0, 100, 3, integer=True),
        'median': Numeric(1, 9, 1, integer=True, odd=True),
    })
    registry['contrast'] = Operation('亮度对比度', 'color', {
        'contrast': Numeric(0.1, 3, 1.15),
        'brightness': Numeric(-100, 100, 0),
    })
    registry['clahe'] = Operation('局部对比增强', 'color', {
        'clip': Numeric(0.5, 8, 2),
        'grid': Numeric(2, 32, 8, integer=True),
    })
    registry['sharpen'] = Operation('文字锐化', 'cleanup', {
        'amount': Numeric(0, 3, 0.8),
        'radius': Numeric(0.1, 5, 1.2),
        'threshold': Numeric(0, 50, 3),
    })
    registry['white_balance'] = Operation('白平衡', 'color', {
        'strength': Numeric(0, 1, 0.7),
        'method': Enumeration(('white_patch', 'gray_world'), 'white_patch'),
    })
    registry['gamma'] = Operation('伽马校正', 'color', {
        'gamma': Numeric(0.2, 4, 1.2),
    })
    registry['stretch'] = Operation('直方图拉伸', 'color', {
        'lower': Numeric(0, 20, 1),
        'upper': Numeric(80, 100, 99),
    })
    registry['bilateral'] = Operation('保边降噪', 'cleanup', {
        'diameter': Numeric(3, 15, 5, integer=True),
        'sigma_color': Numeric(1, 150, 35),
        'sigma_space': Numeric(1, 150, 35),
    })
    registry['remove_border'] = Operation('清除边框', 'cleanup', {
        'size': Numeric(1, 300, 12, integer=True),
    })
    registry['pad'] = Operation('扩展白边', 'geometry', {
        'top': Numeric(0, 1000, 20, integer=True),
        'bottom': Numeric(0, 1000, 20, integer=True),
        'left': Numeric(0, 1000, 20, integer=True),
        'right': Numeric(0, 1000, 20, integer=True),
    })
    registry['resize'] = Operation('缩放', 'geometry', {
        'scale': Numeric(0.1, 4, 1),
    })
    registry['erase'] = Operation('区域擦除', 'cleanup', {
        'region': Region(required=True),
        'mode': Enumeration(('white', 'inpaint'), 'white'),
        'radius': Numeric(1, 20, 5),
    })
    registry['morph_text'] = Operation('文字形态优化', 'cleanup', {
        'mode': Enumeration(('thicken', 'thin', 'close', 'open'), 'thicken'),
        'size': Numeric(1, 7, 2, integer=True),
        'iterations': Numeric(1, 3, 1, integer=True),
    })
    registry['sauvola'] = Operation('Sauvola 二值化', 'color', {
        'window': Numeric(3, 201, 41, integer=True, odd=True),
        'k': Numeric(0.01, 0.8, 0.2),
    })
    registry['remove_lines'] = Operation('清除表格线', 'cleanup', {
        'orientation': Enumeration(('horizontal', 'vertical', 'both'), 'both'),
        'length': Numeric(20, 1000, 80, integer=True),
    })
    registry['flatten_background'] = Operation('背景归一化', 'background', {
        'radius': Numeric(7, 151, 35, integer=True, odd=True),
        'strength': Numeric(0, 1, 0.8),
    })
    registry['invert'] = Operation('反色', 'color', {})
    registry['flip'] = Operation('镜像翻转', 'geometry', {
        'axis': Enumeration(('horizontal', 'vertical'), 'horizontal'),
    })
    registry['ocr'] = Operation('本地文字识别', 'ocr', {
        'language': Enumeration(('chi_sim+eng', 'chi_sim', 'eng'), 'chi_sim+eng'),
        'psm': Numeric(3, 13, 3, integer=True),
        'minimum': Numeric(0, 100, 0),
    })
    registry['restore'] = Operation('恢复原图', 'history', {})
    return registry


REGISTRY = build_registry()


def validate_operation(operation, values):
    require(operation in REGISTRY, '不支持的操作')
    return REGISTRY[operation].validate(values)


def operation_catalog():
    return {key: specification.describe() for key, specification in REGISTRY.items()}
