from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

from . import ocr
from .core import ROOT, DomainError, Store, choice, require, string
from .demo import seed
from .documents import Documents
from .exports import Exporter
from .imaging import detect_edges, read_image, skew_angle
from .mono import Monochrome
from .tasks import Scheduler


def create_app(data_root=None, demo=True):
    app = Flask(__name__, static_folder=str(ROOT / 'static'), static_url_path='/static')
    app.config.update(MAX_CONTENT_LENGTH=240 * 1024 * 1024, JSON_AS_ASCII=False)
    app.json.ensure_ascii = False
    store = Store(data_root) if data_root else Store()
    documents = Documents(store)
    exporter = Exporter(store)
    scheduler = Scheduler(store, documents)
    monochrome_service = Monochrome(store, documents)
    app.extensions.update(store=store, documents=documents, exporter=exporter, scheduler=scheduler, mono=monochrome_service)
    if demo:
        seed(store, documents)

    @app.before_request
    def protect_local_requests():
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            origin = request.headers.get('Origin')
            if origin:
                from urllib.parse import urlsplit
                parsed = urlsplit(origin)
                require(parsed.netloc == request.host, '拒绝跨站写入请求', 403)
            require(request.headers.get('X-PaperFlow') == '1', '缺少请求校验头 X-PaperFlow', 403)

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(DomainError)
    def domain_error(error):
        return jsonify(error=error.message, code=error.code), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description, code=error.name), error.code

    @app.errorhandler(Exception)
    def unexpected(error):
        app.logger.exception('请求失败')
        return jsonify(error='服务器处理失败：' + str(error), code='internal_error'), 500

    def body():
        value = request.get_json(silent=True)
        require(isinstance(value, dict), '请求体必须为 JSON 对象')
        return value

    @app.get('/')
    def index():
        return send_from_directory(ROOT / 'static', 'index.html')

    @app.get('/api/health')
    def health():
        return jsonify(status='ok', version='1.0.0', ocr=ocr.status(), revision=store.snapshot()['revision'])

    @app.get('/api/workspace')
    def workspace():
        return jsonify(store.public())

    @app.get('/api/events')
    def events():
        return jsonify(list(reversed(store.snapshot()['events'][-200:])))

    @app.post('/api/collections')
    def create_collection():
        value = body()
        return jsonify(store.create_collection(value.get('name'), value.get('category', '未分类'))), 201

    @app.patch('/api/collections/<identifier>')
    def update_collection(identifier):
        return jsonify(store.update_collection(identifier, body()))

    @app.delete('/api/collections/<identifier>')
    def delete_collection(identifier):
        store.delete_collection(identifier)
        return jsonify(deleted=identifier)

    @app.post('/api/documents')
    def create_document():
        value = body()
        return jsonify(store.create_document(value.get('collection_id'), value.get('name'), value.get('category', '未分类'))), 201

    @app.get('/api/documents/<identifier>')
    def get_document(identifier):
        return jsonify(documents.document(identifier))

    @app.patch('/api/documents/<identifier>')
    def update_document(identifier):
        return jsonify(store.update_document(identifier, body()))

    @app.delete('/api/documents/<identifier>')
    def delete_document(identifier):
        store.delete_document(identifier)
        return jsonify(deleted=identifier)

    @app.post('/api/documents/<identifier>/import')
    def import_files(identifier):
        store.get('documents', identifier)
        files = request.files.getlist('files')
        require(0 < len(files) <= 30, '请选择1–30个文件')
        position = request.form.get('position')
        if position is not None:
            require(position.isdigit(), '插入位置必须为非负整数')
            position = int(position)
        imported = []
        failures = []
        for uploaded in files:
            path = None
            try:
                path, filename = documents.save_upload(uploaded)
                new_pages = documents.import_path(identifier, path, filename, position)
                imported.extend(new_pages)
                if position is not None:
                    position += len(new_pages)
            except Exception as error:
                failures.append({'filename': uploaded.filename, 'error': str(error)})
            finally:
                if path:
                    path.unlink(missing_ok=True)
        return jsonify(page_ids=imported, failures=failures, document=documents.document(identifier)), (207 if failures else 201)

    @app.post('/api/documents/<identifier>/reorder')
    def reorder(identifier):
        return jsonify(documents.reorder(identifier, body().get('page_ids')))

    @app.post('/api/documents/<identifier>/split')
    def split(identifier):
        value = body()
        return jsonify(documents.split(identifier, value.get('page_ids'), value.get('name'))), 201

    @app.post('/api/documents/merge')
    def merge():
        value = body()
        return jsonify(documents.merge(value.get('document_ids'), value.get('name'), value.get('collection_id'), value.get('consume', False))), 201

    @app.post('/api/pages/copy')
    def copy_pages():
        value = body()
        return jsonify(page_ids=documents.copy_pages(value.get('page_ids'), value.get('target_id'), value.get('position'))), 201

    @app.post('/api/pages/move')
    def move_pages():
        value = body()
        return jsonify(documents.move_pages(value.get('page_ids'), value.get('target_id'), value.get('position')))

    @app.post('/api/pages/delete')
    def delete_pages():
        documents.delete_pages(body().get('page_ids'))
        return jsonify(deleted=True)

    @app.get('/api/pages/<identifier>')
    def get_page(identifier):
        return jsonify(store.get('pages', identifier))

    @app.patch('/api/pages/<identifier>')
    def rename_page(identifier):
        return jsonify(documents.rename_page(identifier, body().get('name')))

    @app.get('/api/pages/<identifier>/image')
    def page_image(identifier):
        page = store.get('pages', identifier)
        variant = choice(request.args.get('variant', 'current'), '图像版本', ('current', 'original', 'thumbnail'))
        return send_file(store.resolve(page[variant]), conditional=True)

    @app.post('/api/pages/<identifier>/mono-preview')
    def mono_preview(identifier):
        return jsonify(monochrome_service.preview(identifier, body())), 201

    @app.get('/api/mono/previews/<preview_id>/image')
    def mono_preview_image(preview_id):
        return send_file(monochrome_service.preview_image(preview_id), mimetype='image/png', conditional=True)

    @app.post('/api/pages/<identifier>/mono-commit')
    def mono_commit(identifier):
        return jsonify(monochrome_service.commit(identifier, body()))

    @app.get('/api/pages/<identifier>/mono')
    def mono_status(identifier):
        return jsonify(monochrome_service.status(identifier))

    @app.get('/api/pages/<identifier>/edges')
    def edges(identifier):
        page = store.get('pages', identifier)
        return jsonify(detect_edges(read_image(store.resolve(page['current']))))

    @app.get('/api/pages/<identifier>/skew')
    def skew(identifier):
        page = store.get('pages', identifier)
        return jsonify(skew_angle(read_image(store.resolve(page['current']))))

    @app.post('/api/pages/<identifier>/process')
    def process(identifier):
        value = body()
        return jsonify(documents.apply(identifier, value.get('operation'), value.get('params', {}), value.get('revision')))

    @app.post('/api/pages/<identifier>/restore')
    def restore(identifier):
        return jsonify(documents.restore(identifier, body().get('history_id')))

    @app.post('/api/pages/<identifier>/ocr')
    def recognize(identifier):
        return jsonify(documents.recognize(identifier, body()))

    @app.patch('/api/pages/<identifier>/ocr')
    def edit_ocr(identifier):
        return jsonify(documents.edit_ocr(identifier, body()))

    @app.post('/api/pages/<identifier>/regions')
    def extract_region(identifier):
        return jsonify(documents.extract_region(identifier, body())), 201

    @app.get('/api/pages/<identifier>/regions/<region_id>')
    def download_region(identifier, region_id):
        page = store.get('pages', identifier)
        region = next((region for region in page['regions'] if region['id'] == region_id), None)
        require(region is not None, '区域不存在', 404)
        return send_file(store.resolve(region['path']), as_attachment=True, download_name=region['name'] + '.png')

    @app.get('/api/search')
    def search():
        return jsonify(ocr.search(store.snapshot(), request.args.get('q'), request.args.get('document_id')))

    @app.post('/api/tasks')
    def submit_task():
        value = body()
        return jsonify(scheduler.submit(value.get('page_ids'), value.get('operation'), value.get('params', {}))), 202

    @app.get('/api/tasks')
    def tasks():
        return jsonify(scheduler.list())

    @app.get('/api/tasks/<identifier>')
    def get_task(identifier):
        return jsonify(store.get('tasks', identifier))

    @app.post('/api/tasks/<identifier>/cancel')
    def cancel_task(identifier):
        return jsonify(scheduler.cancel(identifier))

    @app.post('/api/tasks/<identifier>/retry')
    def retry_task(identifier):
        return jsonify(scheduler.retry(identifier)), 202

    @app.post('/api/documents/<identifier>/export')
    def export(identifier):
        value = body()
        return jsonify(exporter.export(identifier, value.get('kind'), value.get('options', {}))), 201

    @app.get('/api/exports')
    def export_list():
        return jsonify(list(reversed(list(store.snapshot().get('exports', {}).values()))))

    @app.get('/api/exports/<identifier>/download')
    def download_export(identifier):
        record = store.snapshot().get('exports', {}).get(identifier)
        require(record is not None, '导出记录不存在', 404)
        return send_file(store.resolve(record['path']), mimetype=record['mime'], as_attachment=True, download_name=record['filename'])

    @app.post('/api/demo')
    def import_demo():
        value = body()
        document_id = value.get('document_id')
        store.get('documents', document_id)
        choice(value.get('sample', 'clean'), '演示样本', ('clean', 'photo', 'pdf'))
        sample = value.get('sample', 'clean')
        path = ROOT / 'demo' / {'clean': 'demo_1.png', 'photo': 'demo_photo.png', 'pdf': 'demo_document.pdf'}[sample]
        return jsonify(page_ids=documents.import_path(document_id, path)), 201

    @app.post('/api/demo/recovery')
    def load_recovery_demo():
        import hashlib
        import json
        import shutil
        from .core import now
        folder = ROOT / 'demo' / 'recovery_case'
        manifest = json.loads((folder / 'fragment.json').read_text(encoding='utf-8'))
        document_id = manifest['document']['id']
        snapshot = store.snapshot()
        if document_id in snapshot['documents']:
            return jsonify(documents.document(document_id))
        for relative, checksum in manifest['files'].items():
            destination = store.resolve(relative)
            source = (folder / 'files' / relative).resolve()
            require(source.is_relative_to((folder / 'files').resolve()), '演示文件引用越界')
            require(hashlib.sha256(source.read_bytes()).hexdigest() == checksum, '演示文件校验失败')
            if destination.exists():
                require(hashlib.sha256(destination.read_bytes()).hexdigest() == checksum, '演示资源冲突', 409)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
        with store.transaction() as candidate:
            require(document_id not in candidate['documents'], '恢复资料已载入', 409)
            collection = manifest['collection']
            collection['name'] = '恢复练习'
            candidate['collections'].setdefault(collection['id'], collection)
            candidate['documents'][document_id] = manifest['document']
            candidate['pages'].update(manifest['pages'])
            candidate.setdefault('sources', {}).update(manifest['sources'])
            task = manifest['task']
            task['status'] = 'interrupted'
            task['finished_at'] = now()
            task['error'] = '从真实中断现场载入，请核对页面与已完成项后重试。'
            candidate['tasks'][task['id']] = task
            store.event(candidate, '载入恢复练习资料', document_id)
        return jsonify(documents.document(document_id)), 201

    from .advanced import register_advanced
    register_advanced(app, store, documents, exporter)
    return app
