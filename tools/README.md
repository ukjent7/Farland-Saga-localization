# 工具与统一流程

默认入口 `bash flow.sh` 就在仓库根下，从自身位置定位项目，并通过 `uv.sh run --locked` 运行仓库内的 `tools/localize.py`。逆向产物写在仓库同级的私有工作区 `work/`（`FARLAND_WORK` 可改）。命令、路径和环境均使用 WSL/Linux。三语共用处理器和译稿；历史脚本仅供恢复与追溯。

## 状态与环境

```bash
bash flow.sh status
bash flow.sh doctor
bash flow.sh --help
bash uv.sh sync --locked
```

`status` 只读取实际安装和候选元数据；`doctor` 核对锁定依赖、候选可用性、当前安装与原版备份哈希。安装记录以私有工作区的 `install_report.json` 为准，当前候选以私有工作区的 `config/storage_policy.json` 为准。

使用 Python 3.14。私有工作区的 `pyproject.toml`、`uv.lock` 与 `.venv` 构成运行环境（含专有 IDALib wheel）；公开仓库自带一份只含公开依赖的 `pyproject.toml`，IDALib 列为可选 extra `ida`，两份清单版本一致。`uv.sh` 在私有工作区工程存在时优先使用它，单独检出仓库时用仓库自己的清单。环境与缓存为 `work/.venv`、`.uv-cache`、`.pycache` 和 `.tmp`。单独运行工具使用 `bash uv.sh run --locked python tools/工具.py`。依赖维护使用 `uv.sh add/lock/sync --locked`，不使用全局 pip。

IDALib 来自 `/home/kk/ida-pro-9.4`；wheel 与来源哈希在私有工作区 `vendor/idalib/`。静态反汇编使用该 IDALib，未经用户允许不得引入 Capstone。旧 PowerShell 包装与 Win32 内存探针仅保存在历史归档中。

## 译稿修改与审阅

[译稿说明](../translation/README.md)列出当前主题表。普通文字只编辑 `translation/corpus.json`，按 ID 合并三语译文；旧批次和候选 `inputs/` 保留为冻结记录。原文提取器只用于新空白工作区，禁止用提取结果覆盖已有译稿。

合并批次放在 `work/` 内，示例：

```json
{
  "translator": "reviewer",
  "translations": [
    {"id": "EVE/EVE01_1.EVT:0001", "translation": "久等了！", "status": "reviewed", "notes": "审校记录"}
  ]
}
```

```bash
bash flow.sh merge --language zh-CN --draft translation/draft.json --overwrite
bash flow.sh merge --language ko-KR --draft translation/draft.json --overwrite
bash flow.sh merge --language en-US --draft translation/draft.json --overwrite
bash flow.sh merge
bash flow.sh review
```

`--overwrite` 允许明确替换已有译文；没有此参数时拒绝覆盖不同文本。合并核对未知和重复 ID、容量、控制字符与语言编码；只更新指定语言的译文及审校意见。无参数 `merge` 校验现有统一译稿，不从历史批次回写。统一审阅页在 [review.html](../review.html)，包含日文与三语对照。

图片标题、界面标签和外层菜单分别使用浅层三语主题表。图集配置声明边界、字体、布局和保护区域；修改文字后须构建并检查容量报告。

## 从游戏安装引导工作区

持有一份通过 Steam 购买的游戏安装即可从零建出私有工作区。整条链只读游戏目录，不启动游戏，也不修改安装文件。`--game` 缺省为私有工作区同级的安装根；`FARLAND_WORK` 指向别处时必须显式传 `--game`。

```bash
python tools/localize.py unpack --game <游戏根目录>
python tools/localize.py assets freeze --game <游戏根目录>
python tools/localize.py extract-unity
python tools/localize.py build-first --output zh-CN-seed01 --game <游戏根目录>
python tools/localize.py verify --stage zh-CN-seed01
```

`unpack` 依次解出四个 SSF 容器（`FarlandSaga.cue`、`FarlandSaga_Steam.ini`、`FarlandSaga.bin`、`Texture.tex`）到 `decoded/`，还原数据轨得到 `FarlandSaga_track1.bin`，再按 ISO9660 解出 `extracted/steam/`，并把分析用可执行文件放到 `analysis/FarlandSaga.exe`。重复运行幂等；目标 exe 与游戏不一致时需 `--force`。

`assets freeze` 冻结构建要校验身份的原版：`Texture.tex` 按 SSF 容器原样复制（图集解码需要 2048 字节头，补丁配方钉的也是容器哈希），五个 Unity 原版文件复制到 `assets/sources/unity/S-Tribute_Data/`，`S-Tribute_Data/Managed/*.dll` 打成字节可复现的 `unity-managed.zip`（固定时间戳与 deflate 级别），并写 `unity_original_files.json` 清单。

`unpack` 与 `assets freeze` 都带原版守卫：检测到游戏目录安装过补丁时拒绝执行，提示先 `restore-original`、用 `--game` 指向干净安装，或显式 `--force`。已打补丁的安装解包出来的是补丁后的资源，候选会建立在错误的基础上。

`extract-unity` 从冻结的 Unity 原版重建 `assets/sources/unity.json`。扫描规则是在 MonoBehaviour 原始数据上滑动读取定长 UTF-8 段，按文字系统保留日文与繁体中文槽，排除谚文、其他拉丁语言槽、声音列表元数据与含换行的编辑器备注；命中行按 id 与 `translation/corpus.json` 的 `kind == "unity"` 行对齐，未被收录的写入 `unity_unmatched.json` 待人工确认。语料库有而扫描未命中的行直接报错，表示冻结原版与译稿已经漂移。`--check` 只校验不写文件。行序保持扫描顺序，重建结果与历史冻结表字节级一致。

`build-first` 从 `extracted/steam` 直接构建首个候选，不依赖任何既有候选，因此可以在 `build full --base` 之前跑通。它只构建 Saturn 光盘侧；要得到含 Steam 外壳（Unity 文字）的完整候选，在首个候选之上继续 `build full`。镜像编码的 CD parity 自检优先用私有工作区的原版光盘裸镜像 `reference_original.bin`，缺失时退回仓库内的已知答案向量（Steam 版不带原始压制盘的 EDC/ECC，已实测），报告里的 `cd_parity_reference` 记的是实际用了哪一种。

## 构建与补丁

基础候选按当前存储策略选择，输出必须是新目录。以下命令只准备资源，不生成可安装成品：

```bash
bash flow.sh build full --base zh-CN-unifont07 --output zh-text-review --language zh-CN --resources-only
bash flow.sh build full --base ko-KR-unifont-full07 --output ko-text-review --language ko-KR --resources-only
bash flow.sh build full --base en-US-unifont-full07 --output en-text-review --language en-US --resources-only
```

去掉 `--resources-only` 才构建完整候选。成功的完整 `build/patch` 更新当前候选并整理历史文件；资源构建不更新候选，也不能安装。完整文字重建保留基础候选的图块、字距、状态标签、运行时存档标题与外层图集修正。

当前中文稿的 `EVE/EVE09_1.EVT` 字表与脚本超过原镜像的 8192 字节分配，准确长度见构建输出的 `event_capacity.json`。资源模式保留准确译文并在容量报告中标明不可打包；完整构建明确拒绝超限。原镜像没有足够的未占用全零扇区可安全搬移，审计记录在私有工作区 `work/maintenance/iso-free-extents.json`。这不影响已安装的 07 版本。继续修改该事件时，需结合容量报告审阅字表总容量，不能只看单行字数。

要重跑这份容量审计，用 `tools/iso_extents.py` 只读扫描数据轨，它会复现审计记录里的全部关键数值：

```bash
bash uv.sh run --locked python tools/iso_extents.py work/decoded/FarlandSaga_track1.bin
bash uv.sh run --locked python tools/iso_extents.py work/decoded/FarlandSaga_track1.bin --expected-sha256 <轨道哈希> --min-run 5
```

`free_zero_mode1.runs` 为空即表示没有可安全搬移的连续空闲零扇区。`plan_relocations` 只在目录记录相互一致、且与冻结资源清单吻合时才允许搬移，扫描不完整时直接拒绝。

图片主题通过同一入口单独构建，沿用基础候选的其他资源：

```bash
bash flow.sh build titles --base zh-CN-unifont07 --output zh-title-review --language zh-CN --resources-only
bash flow.sh build ui --base ko-KR-unifont-full07 --output ko-ui-review --language ko-KR --resources-only
bash flow.sh build equipment --base en-US-unifont-full07 --output en-equipment-review --language en-US --resources-only
```

新修正优先使用 [config/patches/](../config/patches/README.md)，由公共处理器执行。新编码格式才增加处理器，避免每个版本复制脚本。

```bash
bash flow.sh patch --base en-US-unifont-full07 --output en-menu-review --language en-US --recipe frontend-submenus
bash flow.sh patch --base zh-CN-unifont07 --output zh-price-review --language zh-CN --recipe item-prices
bash flow.sh verify --stage en-menu-review
```

图集从 `assets/sources/Texture.tex` 冻结原版生成，该冻结原版位于私有工作区；候选保存输入快照与回读报告。当前累计菜单覆盖 371 个文字块及 7 个提示面板；存档标题补丁保护指令、编号和存读档逻辑，商品名称只写前 18 字节并保护价格与属性。

## 回归与整理

```bash
bash flow.sh test --cli-only
bash flow.sh test
bash flow.sh test --pack
bash flow.sh assets verify
bash flow.sh storage verify
bash flow.sh clean
bash flow.sh storage compact
```

入口修改使用 `test --cli-only`，公共资源修改使用 `test`，涉及最终镜像编码再加 `--pack`。测试仅写 `work/` 内临时文件，可能按需展开历史候选。结束后用存储工具重新归档。

`clean --apply` 只移除独立校验通过的可重建打包中间镜像；`storage compact --apply` 归档历史大文件；`assets cleanup --apply` 核对后去重预览。历史小文件包的盘点、整理与恢复由私有工作区 `work/storage/` 承担，使用 `flow.sh storage` 管理。不得手动删除恢复链中的备份、对象或版本清单。

## 安装与恢复

只有收到用户明确安装或恢复指令才使用本节。先从 Windows 退出游戏，然后预检；预检不会修改游戏或安装记录。

```bash
bash flow.sh install --language ko-KR --check-only
bash flow.sh install --language ko-KR --game-is-closed
bash flow.sh restore-original --game-is-closed
```

`--stage` 与 `--language` 二选一。实际写入须显式传入 `--game-is-closed`，安装工具验证候选、备份原版并支持失败回滚。WSL 无法检查 Windows 原生游戏进程。工作流不会自动启动游戏，资源构建也不会安装。

## 发布打包

```bash
bash flow.sh package-release --stage zh-CN-unifont08 --patcher /path/to/FarlandSagaPatcher-windows-amd64.exe
bash flow.sh package-release --stage ko-KR-unifont-full07
```

`package-release` 先按候选报告逐文件核对身份，再把每个文件相对原版备份的 4 KiB 差异块写成载荷 ZIP（默认 `work/release/farland-patch-<版本>.zip`），差异对象进入冷存储并与历史归档共享；`--output` 可改输出目录。带 `--patcher` 时同时生成终端用户整合包 `FarlandSagaPatcher-<版本>-win64.zip`（补丁器、载荷与使用说明），挂到 GitHub Release 即可分发。重复打包同一候选得到字节一致的结果。补丁器本身的构建、命令与安全边界见 [../patcher/README.md](../patcher/README.md)。

## 公共模块

`localize.py` 分发命令；`translation_data.py` 提供三语数据视图与校验；`candidate.py` 管理候选复制、资源校验、字库同步和打包；`native_patches.py` 处理声明式补丁；`container_atlases.py` 负责加密图集；`build_alpha.py` 与镜像模块负责原生文字及 Saturn 镜像编码；`unity_resources.py` 负责 Unity 文字；`bootstrap.py` 从已购游戏安装解包并冻结原版素材；`extract_unity_text.py` 从冻结 Unity 原版重建 `unity.json` 文字行表（`--check` 只校验）；`cd_parity.py` 提供 CD 扇区校验的已知答案向量与压制盘参考；`iso_extents.py` 只读扫描 ISO9660 数据轨的空闲零扇区扩展区并规划安全搬移；`version_storage.py` 管理大小文件归档与恢复；`package_release.py` 把候选打包成终端用户差异载荷；`workspace_assets.py` 维护共享预览与历史别名；`path_compat.py` 提供 REPO/WORK 双根与路径兼容。

各资源处理器保留独立职责。带版本号的一次性构建和旧翻译任务工具已归档，不作为新工作入口。
