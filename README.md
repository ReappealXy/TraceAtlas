# TraceAtlas

TraceAtlas 是一款面向 Windows 的数据分析与可视化桌面软件，可用于审计场景下的资金分析、资金流向梳理与关联关系探索，帮助用户通过图形化方式发现数据联系。

当前版本为 **0.1.0 开发预览版**，已实现基础平台。可创建和重开本地项目，导入普通 XLSX 单工作表并人工确认字段，保存原文件及行号，查看标准数据、执行账户计数验证任务、查询来源和导出 CSV/XLSX。后台任务支持排队、取消和失败重试；项目支持单写锁、只读打开、备份恢复、项目内索引与自身格式迁移。

资金分析、图谱、领域模型、完整格式适配与 OCR 尚未实现。账户计数用于验证基础链路，不代表业务分析模块已完成。

## 使用

目标系统：Windows 10 22H2 / Windows 11 x64。构建后运行 `dist/TraceAtlas/TraceAtlas.exe`，或使用 `dist/TraceAtlas-0.1.0-setup-x64.exe` 安装。目录式程序必须保留整个 `TraceAtlas` 文件夹；最终用户无需安装 Python 或数据库。

第一次使用：创建项目 → 导入 Excel → 确认工作表、表头及字段映射 → 检查质量报告 → 导入 → 按账户计数 → 查看结果与来源 → 导出。已完成操作自动保存，关闭后重新打开项目可继续查看。

错误行默认阻止导入；只有明确选择后才保存合格行。重复导入产生独立批次，不自动去重。金额使用精确十进制，账户和长号码按文本保存；未知币种、时区和方向保留未知。项目原文件副本独立于外部输入文件，移动项目时请移动整个项目文件夹。

## 开发与验证

使用 Python 3.12 和 uv：

```powershell
uv sync --frozen
uv run python -m traceatlas.app
uv run pytest -q
uv run ruff check src scripts tests
```

构建目录式程序及安装包（需要 Inno Setup 6.7.3）：

```powershell
./scripts/build.ps1 -InnoCompiler "C:/Path/To/Inno Setup 6/ISCC.exe"
uv run python scripts/check_bundle.py
```

`-SkipInstaller` 只构建目录式程序。构建产物包含第三方许可清单。安装与项目数据分离，卸载程序不默认删除用户项目。

第一版验证目标为 16GB 内存、单项目约 100 万条流水。仓库提供自动化测试和合成数据基准脚本，测试结果仅适用于实际测试过的机器、样本和路径；开发机验证不能替代干净 Windows 10/11 的安装、升级、缩放与多显示器验收。当前预览版尚未完成该完整环境矩阵，也未签名或发布。
