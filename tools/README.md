# 工具与统一流程

默认入口 `bash work/flow.sh` 从自身位置定位项目，并通过 `uv.sh run --locked` 运行；入口留在私有工作区，通过发现逻辑运行本仓库内的 `tools/localize.py`。命令、路径和环境均使用 WSL/Linux。三语共用处理器和译稿；历史脚本仅供恢复与追溯。

## 状态与环境

```bash
bash work/flow.sh status
bash work/flow.sh doctor
bash work/flow.sh --help
bash work/uv.sh sync --locked
```

`status` 只读取实际安装和候选元数据；`doctor` 核对锁定依赖、候选可用性、当前安装与原版备份哈希。安装记录以私有工作区的 `install_report.json` 为准，当前候选以私有工作区的 `config/storage_policy.json` 为准。

使用 Python 3.14。私有工作区的 `pyproject.toml`、`uv.lock` 与 `.venv` 构成运行环境（含专有 IDALib wheel）；公开仓库自带一份只含公开依赖的 `pyproject.toml`，IDALib 列为可选 extra `ida`，两份清单版本一致。环境与缓存为 `work/.venv`、`.uv-cache`、`.pycache` 和 `.tmp`。单独运行工具使用 `bash work/uv.sh run --locked python work/公开仓库/tools/工具.py`。依赖维护使用 `uv.sh add/lock/sync --locked`，不使用全局 pip。

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
bash work/flow.sh merge --language zh-CN --draft work/公开仓库/translation/draft.json --overwrite
bash work/flow.sh merge --language ko-KR --draft work/公开仓库/translation/draft.json --overwrite
bash work/flow.sh merge --language en-US --draft work/公开仓库/translation/draft.json --overwrite
bash work/flow.sh merge
bash work/flow.sh review
```

`--overwrite` 允许明确替换已有译文；没有此参数时拒绝覆盖不同文本。合并核对未知和重复 ID、容量、控制字符与语言编码；只更新指定语言的译文及审校意见。无参数 `merge` 校验现有统一译稿，不从历史批次回写。统一审阅页在 [review.html](../review.html)，包含日文与三语对照。

图片标题、界面标签和外层菜单分别使用浅层三语主题表。图集配置声明边界、字体、布局和保护区域；修改文字后须构建并检查容量报告。

## 构建与补丁

基础候选按当前存储策略选择，输出必须是新目录。以下命令只准备资源，不生成可安装成品：

```bash
bash work/flow.sh build full --base zh-CN-unifont07 --output zh-text-review --language zh-CN --resources-only
bash work/flow.sh build full --base ko-KR-unifont-full07 --output ko-text-review --language ko-KR --resources-only
bash work/flow.sh build full --base en-US-unifont-full07 --output en-text-review --language en-US --resources-only
```

去掉 `--resources-only` 才构建完整候选。成功的完整 `build/patch` 更新当前候选并整理历史文件；资源构建不更新候选，也不能安装。完整文字重建保留基础候选的图块、字距、状态标签、运行时存档标题与外层图集修正。

当前中文稿的 `EVE/EVE09_1.EVT` 字表与脚本超过原镜像的 8192 字节分配，准确长度见构建输出的 `event_capacity.json`。资源模式保留准确译文并在容量报告中标明不可打包；完整构建明确拒绝超限。原镜像没有足够的未占用全零扇区可安全搬移，审计记录在私有工作区 `work/maintenance/iso-free-extents.json`。这不影响已安装的 07 版本。继续修改该事件时，需结合容量报告审阅字表总容量，不能只看单行字数。

图片主题通过同一入口单独构建，沿用基础候选的其他资源：

```bash
bash work/flow.sh build titles --base zh-CN-unifont07 --output zh-title-review --language zh-CN --resources-only
bash work/flow.sh build ui --base ko-KR-unifont-full07 --output ko-ui-review --language ko-KR --resources-only
bash work/flow.sh build equipment --base en-US-unifont-full07 --output en-equipment-review --language en-US --resources-only
```

新修正优先使用 [config/patches/](../config/patches/README.md)，由公共处理器执行。新编码格式才增加处理器，避免每个版本复制脚本。

```bash
bash work/flow.sh patch --base en-US-unifont-full07 --output en-menu-review --language en-US --recipe frontend-submenus
bash work/flow.sh patch --base zh-CN-unifont07 --output zh-price-review --language zh-CN --recipe item-prices
bash work/flow.sh verify --stage en-menu-review
```

图集从 `assets/sources/Texture.tex` 冻结原版生成，该冻结原版位于私有工作区；候选保存输入快照与回读报告。当前累计菜单覆盖 371 个文字块及 7 个提示面板；存档标题补丁保护指令、编号和存读档逻辑，商品名称只写前 18 字节并保护价格与属性。

## 回归与整理

```bash
bash work/flow.sh test --cli-only
bash work/flow.sh test
bash work/flow.sh test --pack
bash work/flow.sh assets verify
bash work/flow.sh storage verify
bash work/flow.sh clean
bash work/flow.sh storage compact
```

入口修改使用 `test --cli-only`，公共资源修改使用 `test`，涉及最终镜像编码再加 `--pack`。测试仅写 `work/` 内临时文件，可能按需展开历史候选。结束后用存储工具重新归档。

`clean --apply` 只移除独立校验通过的可重建打包中间镜像；`storage compact --apply` 归档历史大文件；`assets cleanup --apply` 核对后去重预览。历史小文件包的盘点、整理与恢复由私有工作区 `work/storage/` 承担，使用 `flow.sh storage` 管理。不得手动删除恢复链中的备份、对象或版本清单。

## 安装与恢复

只有收到用户明确安装或恢复指令才使用本节。先从 Windows 退出游戏，然后预检；预检不会修改游戏或安装记录。

```bash
bash work/flow.sh install --language ko-KR --check-only
bash work/flow.sh install --language ko-KR --game-is-closed
bash work/flow.sh restore-original --game-is-closed
```

`--stage` 与 `--language` 二选一。实际写入须显式传入 `--game-is-closed`，安装工具验证候选、备份原版并支持失败回滚。WSL 无法检查 Windows 原生游戏进程。工作流不会自动启动游戏，资源构建也不会安装。

## 公共模块

`localize.py` 分发命令；`translation_data.py` 提供三语数据视图与校验；`candidate.py` 管理候选复制、资源校验、字库同步和打包；`native_patches.py` 处理声明式补丁；`container_atlases.py` 负责加密图集；`build_alpha.py` 与镜像模块负责原生文字及 Saturn 镜像编码；`unity_resources.py` 负责 Unity 文字；`version_storage.py` 管理大小文件归档与恢复；`workspace_assets.py` 维护共享预览与历史别名；`path_compat.py` 提供 REPO/WORK 双根与路径兼容。

各资源处理器保留独立职责。带版本号的一次性构建和旧翻译任务工具已归档，不作为新工作入口。
