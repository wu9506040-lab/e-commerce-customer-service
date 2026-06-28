"""让 pytest 跳过 rag/ 目录下的两个手动脚本。

test_ingest.py 和 test_pipeline.py 按各自 docstring 约定是手动跑的脚本
（`docker exec customer-service-api python -m app.services.rag.test_ingest`），
不是 pytest 测试。

但 pytest 默认按 test_*.py 自动收集 → M7/M4/V3 重构移除了 pipeline.run() 后
触发 ImportError。

pytest 9.x 不再支持 [pytest] collect_ignore_glob ini 选项，改用本 conftest.py
的 collect_ignore 变量（pytest 6/7/8/9 一直支持的 Python 接口）。
"""
collect_ignore = ["test_ingest.py", "test_pipeline.py"]