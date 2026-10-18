# FARLAND SAGA I Saturn Tribute 三语本地化

## 快速开始

**玩家**：下载 Release 里的 `FarlandSagaPatcher-windows-amd64.exe`，放到游戏根目录与 `FarlandSaga.exe` 同级，双击运行，选语言后点「安装」。还原点「还原」。

**贡献者**：`bash flow.sh test --cli-only` 跑通后，改 `translation/` 下的 JSON 参与翻译，行为改动改 `config/patches/` 下的配方。

**维护者**：命令见 [tools/README.md](tools/README.md)，打包见 [patcher/README.md](patcher/README.md)。

## 文件职责

### 仓库根

| 文件 | 职责 |
| --- | --- |
| `flow.sh` | 统一入口，转调 `uv.sh run --locked python tools/localize.py` |
| `uv.sh` | uv 运行包装，锁定依赖 |
| `pyproject.toml` | Python 依赖与 `ida` 可选 extra（Hex-Rays 专有产品，仅私有工作区装） |
| `uv.lock` | 锁定的依赖版本 |
| `.python-version` | 固定 Python 3.14 |
| `.gitignore` | 排除私有工作区与临时构建产物；轻量声明式载荷随仓库纳管 |
| `.gitattributes` | 文本行尾与二进制类型声明 |
| `.editorconfig` | 编辑器缩进与行尾约定 |
| `README.md` | 本文件 |
| `HISTORY.md` | 历史版本进度与格式调查记录 |

### `tools/`

统一入口是 `localize.py`。工具链核心已全面重构为领域分层包 `tools/fls/`（含 `core`, `disc`, `containers`, `text`, `builders`, `storage`, `cli`），`tools/` 根目录各文件保留为 100% 兼容的导入垫片，既可直接作为模块导入，也可供原有命令行与测试无缝调用。

| 文件 | 职责 |
| --- | --- |
| `localize.py` | **统一命令入口**，所有子命令都从这里发起 |
| `bootstrap.py` | 从已购游戏安装引导出私有工作区 |
| `build_alpha.py` | 不依赖私有工作区，从零构建简体中文可玩候选 |
| `build_full_languages.py` | 从三语译稿构建完整的中/韩/英候选 |
| `build_title_images.py` | 把三语标题渲染进原始索引 SPR |
| `build_ui_labels.py` | 本地化原生地形精灵与共享状态砖 |
| `build_equipment_labels.py` | 翻译各图集与 tilemap 里的装备标题 |
| `build_review.py` | 生成可检索的三语统一审阅页 |
| `build_patcher.py` | 构建内嵌全部语言补丁的独立补丁器 |
| `disc_decode.py` | 撤销 Steam Saturn 镜像的扇区置换与加密 |
| `disc_extract.py` | 从 MODE1/2352 数据轨读 ISO9660 文件 |
| `iso_extents.py` | 数据轨空闲扩展区扫描与搬移规划 |
| `iso_layout.py` | Saturn 数据轨的 ISO9660 布局与未占用区 |
| `cd_parity.py` | CD MODE1 扇区 EDC/ECC 的已知答案向量 |
| `steam_container.py` | SSF Steam 容器（Texture.tex 等）的读写 |
| `container_atlases.py` | 在 SSF Texture/Parts PNG 容器里补声明的文字区域 |
| `extract_text.py` | 导出索引事件字符串，保留原偏移 |
| `extract_catalog.py` | 导出菜单字面量与定长的物品/法术/单位记录 |
| `extract_unity_text.py` | 从冻结的 Unity 原版重建 Steam 外壳文字行表 |
| `title_sprites.py` | 解码每个 `TITLE*.SPR` 里的三张索引图 |
| `native_patches.py` | 应用声明式定长文本与位图字形配方 |
| `runtime_text.py` | 本地化主程序里的固定原生字写入 |
| `overlay_font.py` | 补 Steam 物品说明字体，同时保留原有字符 |
| `unity_resources.py` | 补剩余 Steam 文本字段并启用 CJK 字体 |
| `translation_data.py` | 读写 ID 稳定的多语共享译稿 |
| `merge_full_translations.py` | 校验共享语料库，按 ID 合并各语言草稿 |
| `merge_translation_json.py` | 把单份 ID 键草稿合并进共享语料库 |
| `language_encoding.py` | 原生日语字符串的 token 与字形槽助手 |
| `unifont_bitmap.py` | 读 Unifont HEX 字形（读 gzip，保留 16 行单元格布局） |
| `package_release.py` | 把校验过的候选打包成可分发补丁载荷 |
| `candidate.py` | 候选克隆、资源校验与打包 |
| `install_alpha.py` | 安装 alpha 并校验原版备份 |
| `restore_original.py` | 仅用 Linux/WSL 从受保护备份还原原版文件 |
| `version_storage.py` | 历史候选与安装记录的无损冷存储 |
| `workspace_assets.py` | 按内容去重存储预览，解析历史版本 |
| `workspace_status.py` | 不重建、不还原地读取工作区状态 |
| `path_compat.py` | `REPO` / `WORK` / `WORKSPACE` 三个根的路径约定 |
| `file_hash.py` | 流式文件哈希 |
| `process_guard.py` | Linux 侧检查 Windows 安装上的游戏进程 |
| `ida_inspect.py` | 静态检查；数据库与报告全部留在 `work/` |
| `ida_sh2.py` | 经 IDALib 调查 SH2 二进制 |
| `audit_remaining_text.py` | 只读清点剩余字面文本及其 SH2 指针 |
| `test_shards.py` | 工作流回归脚本的分片调度 |
| `README.md` | 日常流程与各工具详细说明 |

### `patcher/` — 补丁器（Go，仅标准库）

| 文件 | 职责 |
| --- | --- |
| `main.go` | 入口、子命令分发与命令行输出 |
| `install.go` | 安装、还原与状态核对；原版备份的解析、校验与清扫 |
| `gui_windows.go` | Win32 图形界面（仅 Windows 编译） |
| `i18n.go` | 三张界面文字表，与平台无关，Linux 上也能测 |
| `embedded.go` | 编译期内嵌的补丁载荷 |
| `payload.go` | 载荷 ZIP 与 Format 1/Format 2 稀疏差异对象的解析、回放 |
| `cdrom.go` | Saturn CD-ROM MODE1 扇区 EDC/ECC 校验与扇区变换引擎 |
| `ssf.go` | SSF Steam 容器加解密与参数解析引擎 |
| `overlay.go` | Steam 物品说明 RGBA 字体生成与 LZSS 压缩引擎 |
| `patcher_test.go` | 单元测试：差异回放、安装/还原往返、备份策略、算法 KAT |
| `console_windows.go` / `console_other.go` | 控制台编码的平台分支 |
| `gui_other.go` | 非 Windows 上 `gui` 子命令的提示 |
| `go.mod` | 模块声明，无第三方依赖 |
| `README.md` | 命令、载荷格式与安全边界 |

### `tests/`

| 文件 | 职责 |
| --- | --- |
| `test_workflows.py` | 针对冻结产物的集成回归 |
| `test_unity_extract.py` | Unity 文字行表提取器 |
| `test_bootstrap.py` | 引导命令；固件不出仓库 |
| `test_storage_bundles.py` | 历史版本存储的安全性与往返 |
| `test_cli.py` | 入口分发与安装预检 |
| `test_resources.py` | 可编辑资源的快速隔离回归 |
| `test_iso_extents.py` | ISO9660 空闲扩展区扫描器 |
| `test_cd_parity.py` | CD MODE1 EDC/ECC 的交叉核对 |
| `test_chinese_rebuild.py` | 全新中文文本段是否完整替换 |
| `test_translation_merge.py` | 译稿合并的语言隔离安全性 |

### `translation/` — 三语译稿

| 文件 | 职责 |
| --- | --- |
| `corpus.json` | 剧情、菜单、名称、说明等普通文字主表，按稳定 ID 存三语译文 |
| `glossary.json` | 专名、术语与审校意见 |
| `titles.json` | 50 张图片关卡标题 |
| `ui_labels.json` | 地形、装备与图片界面标签 |
| `frontend_submenus.json` | 外层菜单与提示面板 |
| `README.md` | 字段含义与合并规则 |

### `config/patches/` — 声明式补丁配方

| 文件 | 职责 |
| --- | --- |
| `frontend-submenus.json` | 外层菜单与提示面板（最大的一份） |
| `frontend-menu.json` | 外层主菜单 |
| `condition-labels.json` | 状态标签 |
| `item-prices.json` | 商店价格 |
| `save-folder.json` | 存档文件夹标题 |
| `menu-spacing.json` | 菜单字距 |
| `dash-center.json` | 破折号居中 |
| `README.md` | 配方格式 |
