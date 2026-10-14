# FARLAND SAGA I Saturn Tribute 三语本地化（公开仓库）

本仓库存放 FARLAND SAGA I Saturn Tribute（世嘉 Saturn 光盘镜像 + Steam 外壳）中、韩、英三语本地化中不涉及版权的部分：工具代码、三语译稿、声明式补丁配方与字体，用于补丁的打包、迭代与分发。游戏原版数据、候选镜像、原版备份等版权相关内容留在私有工作区，不在本仓库。

本仓库是项目的主工作目录，入口脚本 `flow.sh` 与 `uv.sh` 就在仓库根下，所有命令从仓库内发起。逆向产物（解包资源、解码镜像、候选、原版备份与归档）全部写在仓库同级的私有工作区 `work/` 中，由 `tools/path_compat.py` 的 `WORK` 指向；设置环境变量 `FARLAND_WORK` 可以改到别处。仓库单独克隆到任何位置都复现同一套目录布局：

```
<任意位置>/
  <本仓库>/      工具、译稿、补丁配方与字体
  work/          私有工作区：解包、解码、候选、备份与归档
```

```bash
uv run --locked python tools/localize.py --help
```

或安装为可编辑包后使用：

```bash
pip install -e .
```

## 快速开始

需要 Python 3.14，依赖声明在 `pyproject.toml`。公开仓库只含公开依赖；静态反汇编所需的 IDALib 为 Hex-Rays 专有产品，列为可选 extra `ida`，仅在私有工作区从本地 wheel 安装。仓库根、私有工作区根与安装根由 `tools/path_compat.py` 的 `REPO`、`WORK`、`WORKSPACE` 约定，公有数据（`translation/`、`config/patches/`、`fonts/`）走 `REPO`，游戏数据走 `WORK`；`WORK` 默认为仓库同级的 `work/` 目录。

## 从已购游戏安装开始

社区成员持有一份通过 Steam 购买的游戏安装即可从零复现全部产物。整条链只读游戏目录，不启动游戏，也不修改安装文件；解包与冻结产物全部落在私有工作区。

```bash
export FARLAND_WORK=/path/to/your/work                        # 私有工作区根，默认为仓库同级的 work/ 目录
GAME=/path/to/FARLAND\ SAGA\ I\ Saturn\ Tribute                # 游戏安装根

python tools/localize.py status                                          # 确认尚未初始化
python tools/localize.py unpack --game "$GAME"                           # 解出 decoded/ 与 extracted/steam
python tools/localize.py assets freeze --game "$GAME"                    # 冻结原版图集与 Unity 资源
python tools/localize.py extract-unity                                   # 重建 assets/sources/unity.json
python tools/localize.py build-first --output zh-CN-seed01 --game "$GAME" # 首个候选
python tools/localize.py verify --stage zh-CN-seed01
```

各步骤的职责与产物：

| 命令 | 读取 | 写入 |
| --- | --- | --- |
| `unpack` | 游戏安装的 SSF 容器与主程序 | `decoded/`（含 `FarlandSaga_track1.bin`）、`extracted/steam/`、`analysis/FarlandSaga.exe` |
| `assets freeze` | 游戏安装的 `Texture.tex` 与 `S-Tribute_Data/` | `assets/sources/Texture.tex`、`assets/sources/unity/`、`assets/sources/unity-managed.zip` |
| `extract-unity` | `assets/sources/unity/` 与 `translation/corpus.json` | `assets/sources/unity.json`；未被语料库收录的命中写入 `unity_unmatched.json` 待人工确认 |
| `build-first` | `extracted/steam/` 与 `translation/corpus.json` | `staging/<输出>/` 首个候选（Saturn 侧，含加密镜像） |

`unpack` 与 `assets freeze` 在检测到游戏目录已安装过补丁时拒绝执行，提示先恢复原版、用 `--game` 指向干净安装，或显式加 `--force`。已打补丁的安装解包出来的是补丁后的资源，后续候选会建立在错误的基础上。

`build-first` 只构建 Saturn 光盘侧。要得到含 Steam 外壳（Unity 文字）的完整候选，在首个候选之上继续：

```bash
python tools/localize.py build full --base zh-CN-seed01 --output zh-CN-full01 --language zh-CN
```

完整候选的 Unity 文字需要 Windows 字体（`C:/Windows/Fonts` 下的 SimHei、Malgun Gothic、Arial）；Saturn 侧使用仓库内的 GNU Unifont，不依赖外部字体。纯 Linux 环境目前只能构建 Saturn 侧。

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

私有工作区保存游戏数据与私有记录：`extracted/`、`decoded/`、`staging/`（三语候选与历史版本报告）、`analysis/`、`backups/`（原版备份）、`storage/`（归档）、`assets/`（冻结原版素材与预览）、`maintenance/`、`vendor/`（IDA idalib wheel）、`install_report.json`、`config/storage_policy.json`、`config/cleanup.json`，以及私有运行环境 `pyproject.toml`、`uv.lock`、`.venv`、`.uv-cache`、`.pycache`、`.tmp`。实际安装以 `install_report.json` 为准，当前候选以 `config/storage_policy.json` 为准。恢复链（原版备份、共享归档对象与各版本清单）完整位于私有工作区，迁移时须连同 `storage/`、`backups/` 与候选清单整体搬迁，不能只复制本仓库。

## 流程说明

日常合并、构建、校验、环境维护和安装边界集中在 [tools/README.md](tools/README.md)。历史版本与格式调查集中在 [HISTORY.md](HISTORY.md)，补丁配置格式见 [config/patches/README.md](config/patches/README.md)，译稿字段与合并规则见 [translation/README.md](translation/README.md)。

三语 07 候选包括 371 个外层图集文字块、7 个 View 提示面板、存档文件夹标题与商店价格修复。操作指南正文、独立按键指南和制作人员页仍待后续处理。离线回归可核对编码、边界和哈希，游戏内显示与流程需用户自行检查。

## 许可

代码按现状提供。`fonts/unifont-18.0.01/` 为 GNU Unifont，依据 GPL 与 SIL OFL 1.1 许可分发，许可证文件见该目录。
