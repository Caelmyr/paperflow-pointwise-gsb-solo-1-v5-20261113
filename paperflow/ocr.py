from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

import cv2
import pytesseract
from pytesseract import Output

from .core import ROOT, DomainError, choice, identity, now, number, require, string
from .imaging import gray

OCR_LOCK = threading.Lock()


def configure():
    command = shutil.which('tesseract')
    if not command:
        candidates = [
            ROOT / '.tools' / 'ocr' / 'usr' / 'bin' / 'tesseract',
            ROOT / '.tools' / 'Tesseract-OCR' / 'tesseract.exe',
        ]
        if os.name == 'nt':
            for variable in ('ProgramFiles', 'LOCALAPPDATA', 'ProgramFiles(x86)'):
                base = os.environ.get(variable)
                if base:
                    candidates.extend([Path(base) / 'Tesseract-OCR' / 'tesseract.exe', Path(base) / 'Programs' / 'Tesseract-OCR' / 'tesseract.exe'])
        for candidate in candidates:
            if candidate.is_file():
                command = str(candidate)
                break
    if not command:
        return None
    pytesseract.pytesseract.tesseract_cmd = command
    local = ROOT / '.tools' / 'ocr' / 'usr'
    if Path(command).is_relative_to(local):
        library = local / 'lib' / 'x86_64-linux-gnu'
        os.environ['LD_LIBRARY_PATH'] = str(library) + os.pathsep + os.environ.get('LD_LIBRARY_PATH', '')
        os.environ['TESSDATA_PREFIX'] = str(local / 'share' / 'tesseract-ocr' / '5' / 'tessdata')
    portable_languages = ROOT / '.tools' / 'Tesseract-OCR' / 'tessdata'
    if portable_languages.is_dir() and os.name == 'nt':
        os.environ['TESSDATA_PREFIX'] = str(portable_languages)
    os.environ.setdefault('OMP_THREAD_LIMIT', '2')
    return command


def status():
    command = configure()
    if not command:
        return {'available': False, 'languages': [], 'message': '未找到 Tesseract；请运行环境安装脚本'}
    try:
        completed = subprocess.run([command, '--list-langs'], capture_output=True, text=True, timeout=15)
        languages = [line.strip() for line in completed.stdout.splitlines()[1:] if line.strip()]
        version = subprocess.run([command, '--version'], capture_output=True, text=True, timeout=15).stdout.splitlines()[0]
        return {
            'available': completed.returncode == 0 and 'chi_sim' in languages and 'eng' in languages,
            'languages': languages,
            'version': version,
            'message': '中英文离线引擎就绪' if 'chi_sim' in languages and 'eng' in languages else '缺少 chi_sim 或 eng 语言包',
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'available': False, 'languages': [], 'message': str(error)}


def recognize(image, language='chi_sim+eng', psm=3, minimum=0):
    choice(language, '识别语言', ('chi_sim+eng', 'chi_sim', 'eng'))
    psm = number(psm, '版面模式', 3, 13, True)
    minimum = number(minimum, '最低置信度', 0, 100)
    require(configure() is not None, 'OCR 引擎未安装', 503)
    height, width = image.shape[:2]
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    try:
        with OCR_LOCK:
            data = pytesseract.image_to_data(rgb, lang=language, config=f'--psm {psm} --dpi 150', output_type=Output.DICT, timeout=120)
    except (pytesseract.TesseractError, RuntimeError, OSError) as error:
        raise DomainError('本地 OCR 失败：' + str(error), 503, 'ocr_failed') from error
    boxes = []
    lines = {}
    for index, text in enumerate(data['text']):
        text = text.strip()
        confidence = float(data['conf'][index])
        if not text or confidence < minimum:
            continue
        line_key = (data['block_num'][index], data['par_num'][index], data['line_num'][index])
        box = {
            'id': identity('word'),
            'text': text,
            'confidence': round(confidence, 2),
            'x': int(data['left'][index]),
            'y': int(data['top'][index]),
            'width': int(data['width'][index]),
            'height': int(data['height'][index]),
            'line': ':'.join(map(str, line_key)),
            'orientation': 'h',
            'edited': False,
        }
        boxes.append(box)
        lines.setdefault(line_key, []).append(text)
    text = '\n'.join(' '.join(words) for words in lines.values())
    confidence = sum(box['confidence'] for box in boxes) / len(boxes) if boxes else 0
    return {
        'text': text,
        'boxes': boxes,
        'language': language,
        'psm': psm,
        'confidence': round(confidence, 2),
        'width': width,
        'height': height,
        'recognized_at': now(),
        'edited_at': None,
        'edited': False,
        'engine': 'tesseract',
        'stale': False,
    }


def edit_result(current, values):
    require(current is not None, '请先执行 OCR', 409)
    require(isinstance(values, dict), 'OCR 编辑参数无效')
    if 'boxes' in values:
        require(isinstance(values['boxes'], list) and len(values['boxes']) <= 20000, '文字框数量无效')
        existing = {box['id']: box for box in current['boxes']}
        seen = set()
        for change in values['boxes']:
            require(isinstance(change, dict), '文字框修改无效')
            identifier = change.get('id')
            require(identifier in existing and identifier not in seen, '文字框不存在或重复')
            seen.add(identifier)
            box = existing[identifier]
            box['text'] = string(change.get('text', ''), '文字框文本', 10000, True)
            if 'orientation' in change:
                box['orientation'] = choice(change['orientation'], '文字方向', ('h', 'v'))
            box['edited'] = True
        current['text'] = text_from_boxes(current['boxes'])
    if 'text' in values:
        current['text'] = string(values['text'], '识别文本', 500000, True)
        current['text_override'] = True
    current['edited'] = True
    current['edited_at'] = now()
    return current


def text_from_boxes(boxes):
    groups = {}
    for box in boxes:
        groups.setdefault(box['line'], []).append(box['text'])
    return '\n'.join(' '.join(words) for words in groups.values())


def search(state, query, document_id=None, case_sensitive=False):
    query = string(query, '搜索词', 200)
    needle = query if case_sensitive else query.casefold()
    results = []
    documents = state['documents'].values()
    if document_id:
        documents = [document for document in documents if document['id'] == document_id]
    for document in documents:
        for index, page_id in enumerate(document['page_ids']):
            page = state['pages'][page_id]
            result = page.get('ocr')
            if not result:
                continue
            text = result['text']
            haystack = text if case_sensitive else text.casefold()
            offset = 0
            matches = []
            while len(matches) < 100:
                found = haystack.find(needle, offset)
                if found < 0:
                    break
                matches.append({'start': found, 'end': found + len(query), 'snippet': text[max(0, found - 35):found + len(query) + 70]})
                offset = found + max(1, len(query))
            if matches:
                results.append({
                    'document_id': document['id'],
                    'document_name': document['name'],
                    'page_id': page_id,
                    'page_number': index + 1,
                    'matches': matches,
                    'boxes': [box['id'] for box in result['boxes'] if needle in (box['text'] if case_sensitive else box['text'].casefold())],
                })
    return results
