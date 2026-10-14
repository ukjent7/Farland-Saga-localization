# FARLAND SAGA I Saturn Tribute 三语本地化（公开仓库）

本仓库存放 FARLAND SAGA I Saturn Tribute（世嘉 Saturn 光盘镜像 + Steam 外壳）中、韩、英三语本地化中不涉及版权的部分：工具代码、三语译稿、声明式补丁配方与字体，用于补丁的打包、迭代与分发。游戏原版数据、候选镜像、原版备份等版权相关内容留在私有工作区，不在本仓库。

日常操作仍在私有工作区内进行，入口为 `bash work/flow.sh`；本仓库默认位于私有工作区的 `work/公开仓库/` 子目录，入口通过发现逻辑运行仓库内的 `tools/localize.py`。单独使用本仓库时，用环境变量 `FARLAND_WORK` 指向私有工作区根目录。

```bash
uv run --locked python tools/localize.py --help
```

或安装为可编辑包后使用：

```bash
pip install -e .
```

## 快速开始

需要 Python 3.14，依赖声明在 `pyproject.toml`。公开仓库只含公开依赖；静态反汇编所需的 IDALib 为 Hex-Rays 专有产品，列为可选 extra `ida`，仅在私有工作区从本地 wheel 安装。仓库根、私有工作区根与安装根由 `tools/path_compat.py` 的 `REPO`、`WORK`、`WORKSPACE` 约定，公有数据（`translation/`、`config/patches/`、`fonts/`）走 `REPO`，游戏数据走 `WORK`。

## 修改从这里开始

| 需要修改的内容 | 统一来源 |
| --- | --- |
| 剧情、菜单、名称、说明、Steam 普通文字 | [translation/corpus.json](translation/corpus.json)，按稳定 ID 保存三语译文 |
| 专名、术语与审校意见 | [translation/glossary.json](translation/glossary.json) |
| 50 张图片关卡标题 | [translation/titles.json](translation/titles.json) |
| 地形、装备与图片界面标签 | [translation/ui_labels.json](translation/ui_labels.json) |
| 外层菜单与提示面板 | [translation/frontend_submenus.json](translation/frontend_submenus.json) |
| 字距、状态、存档标题、商店价格等行为 | [config/patches/](config/patches/README.md) |
| 查阅译稿 | [统一审阅页](review.html)，由 `flow.sh review` 生成，已被 .gitignore 忽略 |

普通文字以当前各语言译稿逐 ID 合并；旧对齐表中的中文快照不能回写覆盖当前中文。继续翻译请编辑统一表或合并 ID 批次，保留源文、偏移、容量、字位和复核记录。构建候选的 `inputs/` 是冻结输入，不能作为新的编辑来源。

## 目录职责

| 目录 | 职责 |
| --- | --- |
| `tools/` | 公共处理器与 `localize.py` 统一命令入口 |
| `tests/` | 入口与工作流回归检查 |
| `translation/` | 三语译稿 JSON 与译稿说明 |
| `config/patches/` | 声明式补丁配方 |
| `fonts/unifont-18.0.01/` | GNU Unifont 字体与许可证 |
| `HISTORY.md` | 历史版本进度与格式调查记录 |

私有工作区保存游戏数据与私有记录：`extracted/`、`decoded/`、`staging/`（三语候选与历史版本报告）、`analysis/`、`backups/`（原版备份）、`storage/`（归档）、`assets/`（冻结原版素材与预览）、`maintenance/`、`vendor/`（IDA idalib wheel）、`install_report.json`、`config/storage_policy.json`、`config/cleanup.json`、`flow.sh`、`uv.sh`、`pyproject.toml`、`uv.lock`。实际安装以 `install_report.json` 为准，当前候选以 `config/storage_policy.json` 为准。恢复链（原版备份、共享归档对象与各版本清单）完整位于私有工作区，迁移时须连同 `storage/`、`backups/` 与候选清单整体搬迁，不能只复制本仓库。

## 流程说明

日常合并、构建、校验、环境维护和安装边界集中在 [tools/README.md](tools/README.md)。历史版本与格式调查集中在 [HISTORY.md](HISTORY.md)，补丁配置格式见 [config/patches/README.md](config/patches/README.md)，译稿字段与合并规则见 [translation/README.md](translation/README.md)。

三语 07 候选包括 371 个外层图集文字块、7 个 View 提示面板、存档文件夹标题与商店价格修复。操作指南正文、独立按键指南和制作人员页仍待后续处理。离线回归可核对编码、边界和哈希，游戏内显示与流程需用户自行检查。

## 许可

代码按现状提供。`fonts/unifont-18.0.01/` 为 GNU Unifont，依据 GPL 与 SIL OFL 1.1 许可分发，许可证文件见该目录。
