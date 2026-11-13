# PaperFlow 本地接口

默认基址 `http://127.0.0.1:8888/api`。实际端口以本次启动配置为准。JSON 使用UTF-8。所有写请求必须带 `X-PaperFlow: 1`，JSON 写请求带 `Content-Type: application/json`。浏览器跨站写入被拒绝。无付费服务、外部账号或认证依赖。

错误：`{"error":"中文说明","code":"invalid_request"}`，状态400参数无效、403请求来源/校验头、404不存在、409版本/状态冲突、413文件过大、429队列已满、503 OCR不可用。批量导入有失败时状态207并返回 `failures`，已成功文件保留。各操作 JSON 修改成功才返回成功结果。

## 核心流程示例

```sh
curl http://127.0.0.1:8888/api/workspace
curl -X POST http://127.0.0.1:8888/api/collections -H 'X-PaperFlow: 1' -H 'Content-Type: application/json' -d '{"name":"研究资料","category":"工作"}'
```

用返回的集合ID创建文档：

```json
POST /documents
{"collection_id":"collection_实际ID","name":"会议扫描件","category":"会议"}
```

上传：`POST /documents/{id}/import`，multipart `files` 可以重复；可选 form `position` 是从0开始的插入索引。返回 page_ids、failures、document。源文件完整保留，页面生成缩略图。

```json
POST /pages/{id}/process
{"operation":"illumination","params":{"strength":0.7},"revision":0}
```

revision 可选，不匹配返回409。操作包括 perspective / rotate / deskew / illumination / color / denoise / crop / enhance 及高级滤镜。完整参数模式在 `GET /operations`，每个参数包含类型、默认值、范围、枚举及奇数约束。

```json
POST /pages/{id}/process
{"operation":"perspective","params":{"points":[[10,20],[1300,20],[1300,1800],[10,1800]]}}
```

角点单位为当前图像像素。纸张必须为凸四边形。选区格式 `[x,y,width,height]`，最小32×32。

```json
POST /pages/{id}/ocr
{"language":"chi_sim+eng","psm":3,"minimum":0}
```

识别返回页面，其 `ocr` 含text、boxes、confidence、language、stale、edited。每个文字框有id、text、confidence、x、y、width、height和line。

```json
PATCH /pages/{id}/ocr
{"text":"人工校订文本 Hello PaperFlow"}
```

单词编辑用 `{"boxes":[{"id":"word_实际ID","text":"修改文字"}]}`。整段修改生成文本覆盖层；单词编辑保持原坐标。

```json
POST /tasks
{"page_ids":["page_实际ID"],"operation":"ocr","params":{"language":"chi_sim+eng"}}
```

返回202与任务id。`GET /tasks/{id}` 查询。`POST /tasks/{id}/cancel` 逐页取消，`POST /tasks/{id}/retry` 重试失败和未完成页。单页失败保存在results，其他页继续；done/total/progress/succeeded/failed均真实持久化。

```json
POST /documents/{id}/export
{"kind":"searchable_pdf","options":{"dpi":150}}
```

kind 为images/txt/pdf/searchable_pdf/zip；options 可传page_ids（指定导出顺序）、dpi、image_format、quality、include_originals。结果返回导出id，`GET /exports/{id}/download` 下载；`GET /exports/{id}/verify` 校验。可搜索PDF拒绝缺失或过期OCR。

## JPEG 容量交付

旧图片导出对所有页套同一质量值。容量交付按页编码、按页解码测量，再按总容量分配：

```json
POST /documents/{id}/deliveries
{"options":{"budget_bytes":1048576,"profile":"balanced","floor_quality":30,"chroma_sampling":"guard"}}
```

`GET /deliveries/options` 返回保护档位、阈值、质量档位与不可让步项。方案返回 `status`：

- `ready`：可为每页选定不低于保护阈值的编码，整批 JPEG 总量不超预算。
- `infeasible_protection`：某些页即使最低质量档位也达不到所选保护阈值（`allocation.unsatisfiable_pages` 列出每个失败指标和可换档位）。
- `infeasible_budget`：整批在保护下限之上的最小容量仍超预算（`allocation.blocked_pages` 逐页列出下限质量、采样、容量与最紧指标；`allocation.alternatives` 列出换档位后的最低容量）。

方案不会漏页、缩小像素或转灰度。`key=allocation.reason` 说明原因，`allocation.steps` 记录实际生效的逐页降档。

```json
{"options":{"budget_bytes":1048576,"profile":"strict","quality_ladder":"fine",
  "page_profiles":{"page_实际ID":{"profile":"color","floor_quality":60,"ceiling_quality":96}}}}
```

`page_profiles` 用页面ID指定每页保护档位、质量底线和上限。`chroma_sampling` 为 `guard`（含细彩线页面自动保持 4:4:4）或 `4:4:4`（整批强制）。

核对：`GET /deliveries/{id}/preview?page_id=…&family=page|text|color|gradient` 返回雷达式窗口与指标；`GET …/preview-image?…&which=source|candidate|difference` 返回 PNG。候选图始终由实际暂存 JPEG 解码得到，`pixel_scale` 为 1.0。`family` 未指定时选该页对应内容的最佳窗口，也可传 `x`、`y` 手动定位。

确认与下载：`POST /deliveries/{id}/confirm` 校验暂存 JPEG 的 SHA-256、字节数、解码尺寸及整批容量后置为已确认；`GET /deliveries/{id}/download` 只打包这些已确认的 JPEG，并再次逐页比对 SHA-256。输入（图像文件、页序、尺寸）变化后方案与确认自动失效，必须重新生成并确认。`GET /deliveries/{id}/verify` 会重新解码并复算所有指标。

调整：`POST /deliveries/{id}/replan` 可带 `options` 覆盖预算/档位/质量底线并返回带 `comparison` 的新方案（`comparison` 列出设置变化、逐页质量与字节变化、缺口变化）。首次比较可用 `POST /documents/{id}/deliveries` 的 `compared_to`。

## 文档编排

- `POST /documents/{id}/reorder`：page_ids 必须完整且不重复。
- `POST /pages/copy`：page_ids、target_id、可选position，复制图像及OCR记录。
- `POST /pages/move`：page_ids、target_id、可选position，移动关联。
- `POST /pages/delete`：page_ids，删除记录，二进制原文件保留。
- `POST /documents/{id}/split`：page_ids、name，按源页序拆分。
- `POST /documents/merge`：document_ids（顺序有效）、name，可选collection_id、consume。默认复制合并；consume=true移动并移除源文档记录。
- `POST /documents/{id}/blank`：name、width、height、position，插入空白页。
- `POST /pages/{id}/split-spread`：axis、ratio、overlap，拆开跨页扫描。
- `POST /pages/concatenate`：page_ids、target_id、orientation、gap，拼接页面。

## 区域、历史、快照

`POST /pages/{id}/regions` 参数name、kind（text/illustration/other）、region。返回区域id，下载 `GET /pages/{id}/regions/{region_id}`。`POST /pages/{id}/restore` 参数空对象恢复原图，或传history_id恢复该版本。`GET /pages/{id}` 返回完整图像与OCR历史。

`POST /documents/{id}/checkpoints` 保存name命名快照；`GET /documents/{id}/checkpoints` 列表；`GET /checkpoints/{id}/diff` 差异；`POST /checkpoints/{id}/restore` 恢复为新文档（可选name、collection_id）。

## 其他服务

质量分析 `/pages/{id}/analysis`，版面 `/layout`，处理前后差异 `/comparison`，建议 `/recommendations`；元数据 `/documents/{id}/metadata`；检索分面 `/catalog/facets`；POST `/catalog/query` 支持分类、标签、作者、集合、OCR、分页排序；复核队列 `/review/queue`；低置信度 `/pages/{id}/low-confidence`；页面批注 `/notes`；工作流 `/pipelines` 与 `/pipeline-runs`；导出预检 `/documents/{id}/preflight`；完整性 `/audit/integrity?deep=1`；空间 `/audit/storage`；来源 `/sources`；备份 `/backups`；归档导入 multipart `/archives/import`（file、collection_id）；用户设置 `/preferences`；会话 `/session`。

所有已注册接口完整列表见下方（由运行中的同版本 Flask 路由表生成）。

| 方法 | 路径 |
|---|---|
| POST | `/api/archives/import` |
| POST | `/api/archives/preview` |
| POST | `/api/audit/cleanup` |
| GET | `/api/audit/duplicates` |
| POST | `/api/audit/events` |
| GET | `/api/audit/integrity` |
| POST | `/api/audit/orphans` |
| GET | `/api/audit/storage` |
| GET | `/api/audit/summary` |
| POST | `/api/backups` |
| GET | `/api/backups` |
| GET | `/api/backups/<identifier>/download` |
| GET | `/api/backups/<identifier>/verify` |
| POST | `/api/catalog/batch` |
| GET | `/api/catalog/facets` |
| POST | `/api/catalog/query` |
| DELETE | `/api/checkpoints/<identifier>` |
| GET | `/api/checkpoints/<identifier>/diff` |
| POST | `/api/checkpoints/<identifier>/restore` |
| POST | `/api/collections` |
| PATCH | `/api/collections/<identifier>` |
| DELETE | `/api/collections/<identifier>` |
| POST | `/api/demo` |
| POST | `/api/demo/recovery` |
| POST | `/api/documents` |
| POST | `/api/documents/<document_id>/blank` |
| GET | `/api/documents/<document_id>/bookmarks` |
| POST | `/api/documents/<document_id>/bookmarks` |
| DELETE | `/api/documents/<document_id>/bookmarks/<bookmark_id>` |
| POST | `/api/documents/<document_id>/checkpoints` |
| GET | `/api/documents/<document_id>/checkpoints` |
| POST | `/api/documents/<document_id>/contact-sheet` |
| GET | `/api/documents/<document_id>/export-history` |
| POST | `/api/documents/<document_id>/interchange` |
| PATCH | `/api/documents/<document_id>/metadata` |
| GET | `/api/documents/<document_id>/preflight` |
| POST | `/api/documents/<document_id>/replace` |
| POST | `/api/documents/<document_id>/replace-preview` |
| GET | `/api/documents/<document_id>/report` |
| POST | `/api/documents/<document_id>/report` |
| GET | `/api/documents/<document_id>/suggest-name` |
| GET | `/api/documents/<document_id>/text-statistics` |
| GET | `/api/documents/<identifier>` |
| GET | `/api/deliveries/options` |
| GET | `/api/deliveries/<identifier>` |
| POST | `/api/deliveries/<identifier>/confirm` |
| GET | `/api/deliveries/<identifier>/verify` |
| GET | `/api/deliveries/<identifier>/download` |
| GET | `/api/deliveries/<identifier>/preview` |
| GET | `/api/deliveries/<identifier>/preview-image` |
| POST | `/api/deliveries/<identifier>/replan` |
| PATCH | `/api/documents/<identifier>` |
| DELETE | `/api/documents/<identifier>` |
| POST | `/api/documents/<document_id>/deliveries` |
| GET | `/api/documents/<document_id>/deliveries` |
| POST | `/api/documents/<identifier>/export` |
| POST | `/api/documents/<identifier>/import` |
| POST | `/api/documents/<identifier>/reorder` |
| POST | `/api/documents/<identifier>/split` |
| POST | `/api/documents/merge` |
| GET | `/api/events` |
| GET | `/api/exports` |
| GET | `/api/exports/<identifier>/download` |
| GET | `/api/exports/<identifier>/verify` |
| GET | `/api/health` |
| GET | `/api/operations` |
| GET | `/api/pages/<identifier>` |
| PATCH | `/api/pages/<identifier>` |
| GET | `/api/pages/<identifier>/edges` |
| GET | `/api/pages/<identifier>/image` |
| POST | `/api/pages/<identifier>/ocr` |
| PATCH | `/api/pages/<identifier>/ocr` |
| POST | `/api/pages/<identifier>/process` |
| POST | `/api/pages/<identifier>/regions` |
| GET | `/api/pages/<identifier>/regions/<region_id>` |
| POST | `/api/pages/<identifier>/restore` |
| GET | `/api/pages/<identifier>/skew` |
| GET | `/api/pages/<page_id>/analysis` |
| GET | `/api/pages/<page_id>/comparison` |
| GET | `/api/pages/<page_id>/layout` |
| GET | `/api/pages/<page_id>/low-confidence` |
| POST | `/api/pages/<page_id>/notes` |
| PATCH | `/api/pages/<page_id>/notes/<note_id>` |
| DELETE | `/api/pages/<page_id>/notes/<note_id>` |
| GET | `/api/pages/<page_id>/provenance` |
| GET | `/api/pages/<page_id>/recommendations` |
| POST | `/api/pages/<page_id>/review` |
| POST | `/api/pages/<page_id>/split-spread` |
| POST | `/api/pages/concatenate` |
| POST | `/api/pages/copy` |
| POST | `/api/pages/delete` |
| POST | `/api/pages/move` |
| GET | `/api/pipeline-runs` |
| GET | `/api/pipeline-runs/<identifier>` |
| POST | `/api/pipeline-runs/<identifier>/cancel` |
| GET | `/api/pipelines` |
| POST | `/api/pipelines` |
| GET | `/api/pipelines/<identifier>` |
| PATCH | `/api/pipelines/<identifier>` |
| DELETE | `/api/pipelines/<identifier>` |
| POST | `/api/pipelines/<identifier>/run` |
| GET | `/api/preferences` |
| PATCH | `/api/preferences` |
| POST | `/api/preferences/reset` |
| GET | `/api/reports/workspace` |
| GET | `/api/review/queue` |
| GET | `/api/review/settings` |
| PATCH | `/api/review/settings` |
| GET | `/api/search` |
| GET | `/api/session` |
| PUT | `/api/session` |
| GET | `/api/sources` |
| GET | `/api/sources/<identifier>/download` |
| GET | `/api/sources/<identifier>/verify` |
| POST | `/api/tasks` |
| GET | `/api/tasks` |
| GET | `/api/tasks/<identifier>` |
| POST | `/api/tasks/<identifier>/cancel` |
| POST | `/api/tasks/<identifier>/retry` |
| GET | `/api/workspace` |

页面的“恢复练习”使用 POST /api/demo/recovery 载入从真实中断服务保存的练习资料；不是自动验收脚本。页面书签和复核入口对应已有目录与审核接口。
