# PaperFlow 文档图像数字化工作台

原生HTML/CSS/JavaScript + Flask + OpenCV + 本地Tesseract中英文识别，JSON原子持久化。

Docker启动：`docker compose up -d --build`，打开 http://127.0.0.1:8888 。首次构建需下载依赖，之后核心功能离线可用。端口占用时用环境变量PAPERFLOW_PORT指定其他宿主端口。PAPERFLOW_DATA_DIR可指定宿主机独立数据目录，默认./data；不要让多个服务写同一工作区。停止：`docker compose down`。

本机启动：`bash start.sh --port 8888`。Windows用start.bat或start.bat 8889，stop.bat停止；脚本未在Windows实机验证。固定版本依赖见requirements.lock.txt。

首次打开自带中英文演示页及手机拍摄练习图。支持图片/PDF导入、校正、光照、色彩、区域提取、OCR、人工校订、多页编排、导出。页面书签与页面复核直接连接已有后端服务；导出归档可选择完整或不携带原件。更多工具包括工作流、快照、质量诊断及备份。参数及接口见docs/API.md。

JPEG容量交付：顶部「▣ JPEG 容量交付」按整批总容量与保护档位生成方案。每页在原像素尺度生成多档候选并解码复测文字边缘、细彩线与照片渐变损失，再逐页分配质量；核对时会显示实际暂存 JPEG 解码后的 1:1 窗口，而不是未压缩预览。保护与容量的取舍逐项可见，达不到的容量会列出缺口与需要放宽的内容，不会漏页、缩尺寸或转灰度。确认后才可下载，且下载内容就是确认过的暂存编码。demo/practice/capacity_delivery 提供整页、细彩线、照片和深色硬页四种练习图。

原稿、处理图与来源分别留存。demo/practice中的图片、PDF和文本可直接导入练习。字体许可证见resources/FONT-LICENSE.txt。已有项目测试：`python -m pytest tests/test_acceptance.py tests/test_delivery.py -q`；普通测试不代替对复杂业务边界的核验。
