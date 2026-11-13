from __future__ import annotations

from flask import Blueprint, jsonify, request, send_file

from . import analysis, layout
from .archive import Archives
from .audit import Auditor
from .catalog import Catalog
from .core import DomainError, require
from .fonts import FontLibrary
from .imaging import read_image
from .interchange import Interchange
from .parameters import operation_catalog
from .pipelines import Pipelines
from .preferences import Preferences
from .reports import Reports
from .review import Review


def register_advanced(app, store, documents, exporter):
    routes = Blueprint('advanced', __name__, url_prefix='/api')
    archives = Archives(store, documents)
    auditor = Auditor(store)
    catalog = Catalog(store)
    review = Review(store)
    pipelines = Pipelines(store, documents)
    preferences = Preferences(store)
    reports = Reports(store, auditor, review)
    interchange = Interchange(store, exporter)
    font_library = FontLibrary(store)
    app.extensions.update(archives=archives, auditor=auditor, catalog=catalog, review=review,
                          pipelines=pipelines, preferences=preferences, reports=reports,
                          interchange=interchange, font_library=font_library)

    def body():
        values = request.get_json(silent=True)
        require(isinstance(values, dict), '请求体必须为 JSON 对象')
        return values

    def image(page_id, variant='current'):
        page = store.get('pages', page_id)
        return read_image(store.resolve(page[variant]))

    @routes.get('/operations')
    def operations():
        return jsonify(operation_catalog())

    @routes.get('/pages/<page_id>/analysis')
    def page_analysis(page_id):
        return jsonify(analysis.full_analysis(image(page_id)))

    @routes.get('/pages/<page_id>/layout')
    def page_layout(page_id):
        page = store.get('pages', page_id)
        return jsonify(layout.analyze_layout(image(page_id), page.get('ocr')))

    @routes.get('/pages/<page_id>/comparison')
    def page_comparison(page_id):
        return jsonify(analysis.compare_images(image(page_id, 'original'), image(page_id)))

    @routes.get('/pages/<page_id>/recommendations')
    def page_recommendations(page_id):
        return jsonify(analysis.recommend(image(page_id)))

    @routes.post('/catalog/query')
    def catalog_query():
        return jsonify(catalog.query(body()))

    @routes.get('/catalog/facets')
    def catalog_facets():
        return jsonify(catalog.facets())

    @routes.post('/catalog/batch')
    def catalog_batch():
        return jsonify(catalog.batch_update(body()))

    @routes.patch('/documents/<document_id>/metadata')
    def document_metadata(document_id):
        return jsonify(catalog.update_metadata(document_id, body()))

    @routes.get('/documents/<document_id>/suggest-name')
    def suggest_name(document_id):
        return jsonify(catalog.suggest_name(document_id))

    @routes.get('/documents/<document_id>/bookmarks')
    def bookmarks(document_id):
        return jsonify(catalog.bookmarks(document_id))

    @routes.post('/documents/<document_id>/bookmarks')
    def add_bookmark(document_id):
        return jsonify(catalog.add_bookmark(document_id, body())), 201

    @routes.delete('/documents/<document_id>/bookmarks/<bookmark_id>')
    def delete_bookmark(document_id, bookmark_id):
        return jsonify(catalog.delete_bookmark(document_id, bookmark_id))

    @routes.get('/review/settings')
    def review_settings():
        return jsonify(review.configuration())

    @routes.patch('/review/settings')
    def update_review_settings():
        return jsonify(review.configure(body()))

    @routes.get('/review/queue')
    def review_queue():
        return jsonify(review.queue(request.args.get('document_id')))

    @routes.get('/pages/<page_id>/low-confidence')
    def low_confidence(page_id):
        value = request.args.get('threshold')
        if value is not None:
            try:
                value = float(value)
            except ValueError:
                raise DomainError('置信度阈值必须为数字')
        return jsonify(review.low_confidence(page_id, value))

    @routes.post('/pages/<page_id>/review')
    def page_review(page_id):
        return jsonify(review.set_status(page_id, body()))

    @routes.post('/pages/<page_id>/notes')
    def add_note(page_id):
        return jsonify(review.add_note(page_id, body())), 201

    @routes.patch('/pages/<page_id>/notes/<note_id>')
    def update_note(page_id, note_id):
        return jsonify(review.update_note(page_id, note_id, body()))

    @routes.delete('/pages/<page_id>/notes/<note_id>')
    def delete_note(page_id, note_id):
        return jsonify(review.delete_note(page_id, note_id))

    @routes.get('/documents/<document_id>/text-statistics')
    def text_statistics(document_id):
        return jsonify(review.text_statistics(document_id))

    @routes.post('/documents/<document_id>/replace-preview')
    def replace_preview(document_id):
        return jsonify(review.replace_preview(document_id, body()))

    @routes.post('/documents/<document_id>/replace')
    def replace_apply(document_id):
        return jsonify(review.replace_apply(document_id, body()))

    @routes.get('/pipelines')
    def pipeline_list():
        return jsonify(pipelines.list())

    @routes.post('/pipelines')
    def pipeline_create():
        return jsonify(pipelines.create(body())), 201

    @routes.get('/pipelines/<identifier>')
    def pipeline_get(identifier):
        return jsonify(pipelines.get(identifier))

    @routes.patch('/pipelines/<identifier>')
    def pipeline_update(identifier):
        return jsonify(pipelines.update(identifier, body()))

    @routes.delete('/pipelines/<identifier>')
    def pipeline_delete(identifier):
        return jsonify(pipelines.delete(identifier))

    @routes.post('/pipelines/<identifier>/run')
    def pipeline_submit(identifier):
        return jsonify(pipelines.submit(identifier, body().get('page_ids'))), 202

    @routes.get('/pipeline-runs')
    def pipeline_runs():
        return jsonify(list(reversed(list(store.snapshot().get('pipeline_runs', {}).values()))))

    @routes.get('/pipeline-runs/<identifier>')
    def pipeline_run(identifier):
        return jsonify(pipelines.get_run(identifier))

    @routes.post('/pipeline-runs/<identifier>/cancel')
    def pipeline_cancel(identifier):
        return jsonify(pipelines.cancel(identifier))

    @routes.get('/audit/integrity')
    def integrity():
        return jsonify(auditor.integrity(request.args.get('deep') == '1'))

    @routes.get('/audit/storage')
    def storage():
        return jsonify(auditor.storage())

    @routes.get('/audit/duplicates')
    def duplicates():
        return jsonify(auditor.duplicates())

    @routes.get('/audit/summary')
    def summary():
        return jsonify(auditor.summary())

    @routes.post('/audit/events')
    def filtered_events():
        return jsonify(auditor.events(body()))

    @routes.post('/audit/orphans')
    def orphan_preview():
        return jsonify(auditor.orphan_files(body().get('minimum_age_hours', 24)))

    @routes.post('/audit/cleanup')
    def cleanup():
        return jsonify(auditor.cleanup(body()))

    @routes.post('/backups')
    def create_backup():
        return jsonify(archives.backup()), 201

    @routes.get('/backups')
    def backup_list():
        return jsonify(list(store.snapshot().get('backups', {}).values()))

    @routes.get('/backups/<identifier>/verify')
    def verify_backup(identifier):
        return jsonify(archives.verify_backup(identifier))

    @routes.get('/backups/<identifier>/download')
    def download_backup(identifier):
        backup = store.snapshot().get('backups', {}).get(identifier)
        require(backup is not None, '备份不存在', 404)
        return send_file(store.resolve(backup['path']), as_attachment=True, download_name='PaperFlow_工作区备份.zip')

    @routes.post('/archives/preview')
    def preview_archive():
        uploaded = request.files.get('file')
        require(uploaded is not None, '请选择 ZIP 归档文件')
        path = store.new_file('incoming', '.zip')
        try:
            uploaded.save(path)
            return jsonify(archives.preview(path))
        finally:
            path.unlink(missing_ok=True)

    @routes.post('/archives/import')
    def import_archive():
        uploaded = request.files.get('file')
        require(uploaded is not None, '请选择 ZIP 归档文件')
        path = store.new_file('incoming', '.zip')
        try:
            uploaded.save(path)
            result = archives.import_document(path, request.form.get('collection_id'), request.form.get('name'))
            return jsonify(result), 201
        finally:
            path.unlink(missing_ok=True)

    @routes.post('/documents/<document_id>/interchange')
    def document_interchange(document_id):
        return jsonify(interchange.export(document_id, body().get('kind'))), 201

    @routes.get('/documents/<document_id>/report')
    def document_report(document_id):
        return jsonify(reports.document(document_id))

    @routes.post('/documents/<document_id>/report')
    def export_report(document_id):
        return jsonify(reports.export(document_id, body().get('kind', 'html'))), 201

    @routes.get('/reports/workspace')
    def workspace_report():
        return jsonify(reports.workspace())

    @routes.get('/preferences')
    def get_preferences():
        return jsonify(preferences.get())

    @routes.patch('/preferences')
    def update_preferences():
        return jsonify(preferences.update(body()))

    @routes.post('/preferences/reset')
    def reset_preferences():
        return jsonify(preferences.reset())

    @routes.get('/session')
    def get_session():
        return jsonify(preferences.session())

    @routes.put('/session')
    def save_session():
        return jsonify(preferences.save_session(body()))

    from .sources import Sources
    from .checkpoints import Checkpoints
    from .assembly import Assembly
    sources = Sources(store)
    checkpoints = Checkpoints(store)
    assembly = Assembly(store, documents)
    app.extensions.update(sources=sources, checkpoints=checkpoints, assembly=assembly)

    @routes.get('/sources')
    def source_list():
        return jsonify(sources.list(request.args.get('document_id')))

    @routes.get('/sources/<identifier>/verify')
    def source_verify(identifier):
        return jsonify(sources.verify(identifier))

    @routes.get('/sources/<identifier>/download')
    def source_download(identifier):
        source = sources.get(identifier)
        return send_file(store.resolve(source['path']), as_attachment=True, download_name=source['name'])

    @routes.get('/pages/<page_id>/provenance')
    def provenance(page_id):
        return jsonify(sources.provenance(page_id))

    @routes.post('/documents/<document_id>/checkpoints')
    def checkpoint_create(document_id):
        return jsonify(checkpoints.create(document_id, body())), 201

    @routes.get('/documents/<document_id>/checkpoints')
    def checkpoint_list(document_id):
        return jsonify(checkpoints.list(document_id))

    @routes.get('/checkpoints/<identifier>/diff')
    def checkpoint_diff(identifier):
        return jsonify(checkpoints.diff(identifier))

    @routes.post('/checkpoints/<identifier>/restore')
    def checkpoint_restore(identifier):
        return jsonify(checkpoints.restore_copy(identifier, body())), 201

    @routes.delete('/checkpoints/<identifier>')
    def checkpoint_delete(identifier):
        return jsonify(checkpoints.delete(identifier))

    @routes.post('/documents/<document_id>/blank')
    def blank_page(document_id):
        return jsonify(assembly.blank(document_id, body())), 201

    @routes.post('/pages/<page_id>/split-spread')
    def split_spread(page_id):
        return jsonify(assembly.split_spread(page_id, body())), 201

    @routes.post('/documents/<document_id>/contact-sheet')
    def contact_sheet(document_id):
        return jsonify(assembly.contact_sheet(document_id, body())), 201

    @routes.post('/pages/concatenate')
    def concatenate():
        values = body()
        return jsonify(assembly.concatenate(values.get('page_ids'), values.get('target_id'), values)), 201

    from .preflight import Preflight
    preflight = Preflight(store, exporter)
    app.extensions['preflight'] = preflight

    @routes.get('/documents/<document_id>/preflight')
    def document_preflight(document_id):
        font_ids = request.args.getlist('font_ids') or None
        return jsonify(preflight.document(document_id,
                                         request.args.get('kind', 'searchable_pdf'),
                                         font_ids))

    @routes.get('/exports/<identifier>/verify')
    def export_verify(identifier):
        return jsonify(preflight.export(identifier))

    @routes.get('/documents/<document_id>/export-history')
    def document_export_history(document_id):
        return jsonify(preflight.export_history(document_id))

    # ---------- 字体库与文字层交付前检查 ----------
    @routes.get('/fonts')
    def font_list():
        return jsonify(font_library.list_records())

    @routes.post('/fonts')
    def font_upload():
        uploaded = request.files.get('file')
        require(uploaded is not None, '请选择 TTF / OTF / TTC 字体文件')
        return jsonify(font_library.upload(uploaded)), 201

    @routes.delete('/fonts/<font_id>')
    def font_delete(font_id):
        return jsonify(font_library.delete(font_id))

    @routes.post('/fonts/coverage')
    def font_coverage():
        value = body()
        text = value.get('text', '')
        require(isinstance(text, str) and text.strip(), '请提供需要检查的文字')
        font_ids = value.get('font_ids') or []
        require(isinstance(font_ids, list) and 0 < len(font_ids) <= 8, '字体回退链无效')
        return jsonify(font_library.coverage(text, font_ids))

    @routes.get('/documents/<document_id>/text-layer/preview')
    def text_layer_preview(document_id):
        options = {
            'page_ids': request.args.getlist('page_ids') or None,
            'font_ids': request.args.getlist('font_ids') or None,
        }
        if options['page_ids'] is None:
            options.pop('page_ids')
        if options['font_ids'] is None:
            options.pop('font_ids')
        if 'dpi' in request.args:
            try:
                options['dpi'] = int(request.args['dpi'])
            except ValueError:
                raise DomainError('DPI 必须为整数')
        return jsonify(exporter.text_layer_preview(document_id, options))

    @routes.get('/documents/<document_id>/text-layer/preview-image')
    def text_layer_preview_image(document_id):
        options = {
            'font_ids': request.args.getlist('font_ids') or None,
        }
        if options['font_ids'] is None:
            options.pop('font_ids')
        if 'dpi' in request.args:
            try:
                options['dpi'] = int(request.args['dpi'])
            except ValueError:
                raise DomainError('DPI 必须为整数')
        if 'page_index' in request.args:
            try:
                options['page_index'] = int(request.args['page_index'])
            except ValueError:
                raise DomainError('页码必须为非负整数')
        data, _, _ = exporter.preview_image(document_id, options)
        from flask import Response
        return Response(data, mimetype='image/png', headers={'Cache-Control': 'no-store'})

    app.register_blueprint(routes)
