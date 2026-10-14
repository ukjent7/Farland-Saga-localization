# 补丁配置格式

配置由 localize.py patch 读取，语言、字段位置和字形数据都放在JSON，执行代码只保留资源类型的通用规则。新问题先增补配置；现有成品用 --base，输出用新的 --output。

顶层 schema 为1，id为配置名，languages列出支持的语言，report_key指定候选报告中的字段。普通文字在fields数组中配置：file、offset、capacity_bytes、mode、source和text。

| 字段 | 含义 |
| --- | --- |
| file / offset | 原生资源名和固定偏移；十六进制偏移可用字符串 |
| capacity_bytes | 完整字段容量，包括mode=nul时的结尾零 |
| mode=fixed | 固定字节数，无终止符；不足部分使用原全角空字格 |
| mode=nul | 文字后填零，必须给终止符留空间 |
| source | 原版CP932文字，必须与extracted/steam对应字段完全一致 |
| before | 按语言列出基础候选的预期译文；不提供时预期是原文 |
| text | 按语言列出目标文字；允许重复应用同一配置 |
| new_text=true | 仅在首次替换原文时增加已识别条目数 |

内置条件标签配置通过 `translation_corpus` 引用 `translation/corpus.json`，字段 ID 与原定位逐项核对，文字从该行 `translations` 读取；旧 `translation_file` 仅保留兼容显式历史配置。width_checks保留参照选项并检查字格宽度；mirror_checks扫描原始资源并要求副本位置与清单相同。glyphs通过character、source_hex、bitmap_hex指定一个32字节字形替换。

目标仍使用原16×16字格。英语一个字格最多两个ASCII字符，韩语和中文一个字格一个字符。超出容量、基础字段不匹配、出现不支持的语言或字形，构建立即停止；不会自动截断或搬移程序指针。

配置不会直接安装。统一流程与命令见 [工具说明](../tools/README.md)。

`original_ranges` 恢复固定记录中被旧补丁覆盖的原版数值。每组声明 `file/source_sha256/offset/stride/count/field_offset/size/before_hex`；逐记录只接受声明的旧字节或原版字节，核对冻结资源身份及边界后恢复。字段可以仅在部分语言的 `text` 中声明，其他语言跳过。

`item-prices` 恢复 `ITEM.DAT` 及 `MAIN.DAT` 副本中全部 200 条记录的两字节原价，并核对价格与其余属性。名称真实容量为 18 字节（含终止符），旧译稿的 20 字节容量仅作为历史输入保留；公共构建器使用真实边界。八条过长英语名称通过配置中的 `before/text` 缩写至 16 个 ASCII 字符以内，完整重建也使用同一配置，原译稿和既有审阅记录不覆盖。

`container_atlases` 处理 SSF 加密的 `Texture/Parts` PNG 图集。`source` 指向私有工作区内冻结的原版（如 `assets/sources/Texture.tex`），`original_sha256` 固定其身份；`image_size`、`part_count` 和各 `labels` 的 `part/box` 固定图集布局。文字放在 `text`，每种语言可指定 `font_size`。`panel_parts` / `panel_text` 处理同结构的选项提示面板。字号过大或缺字会停止构建；图标、文字区以外像素、Parts/Anime 元数据和加密容器头保持原样，编码后解密回读逐像素核对。

`frontend-menu` 覆盖八项外层菜单及七个语言槽的 View 按钮提示，候选增加第六个安装文件 `Texture.tex`。安装器自动纳入原版备份、预检、回滚和恢复；仅生成候选不会修改游戏目录。后续通用补丁和完整译稿重建保留这份图集。

当前累计配置为 `frontend-submenus`，保留主菜单并覆盖子选项、确认弹窗、操作说明和警告、加载文字、无存档提示，累计 371 个文字块及 7 个 View 面板。译稿组 ID 与原语言槽在 `translation/frontend_submenus.json` 中记录；内置配置的 `translation_source` 与标签 `translation_id` 指向统一主题译稿，面板用 `panel_translation_ids` 指向同一表。当前译文与配置布局去除空白后相同时，沿用原字号和换行；文字改动后使用主题表的新文本与新换行，由现有渲染和容量规则检查，构建时不自动截断。显式自定义 JSON 配置继续使用其中的 `text`，用于复放冻结版本或验证布局。

图集标签可声明相对 `region`、背景 RGBA `background`、对齐 `align`（left/center/right）、文字 RGBA `fill`、描边 `stroke`、允许行数 `max_lines` 及顺时针旋转 `rotation`（0/90/180/270）。区域必须在冻结的 sprite 内；仅清除该区域，以保护加载动画、徽章边框等相邻内容。换行仅在允许的行数内有效，其他控制字符和空行均拒绝。`preview_sections` 指定分组预览的 ID 与 sprite 列表。

图集始终从冻结原版生成，新增子选项配置须包含已有主菜单与面板，避免回退覆盖。当前累计配置可重复应用；早期 `frontend-menu` 仅覆盖主菜单，保留用于复放旧 04 候选，不用于生成当前子菜单候选。

`runtime_word_writes` 处理模拟器运行时向原生内存写入的标题文字。`original_sha256` 固定原版可执行文件；每个标签声明 `source`、三语 `text`、`capacity_cells` 和逐字 `word_writes`。每个写入记录固定 4 字节立即数 `offset`、内存写入地址 `memory_address` 及完整 18 字节原版指令 `instruction_hex`。只有文字立即数允许改变，指令操作码、内存地址、调用目标和其他字节必须一致；超出字格容量或基础指令不匹配时拒绝构建。

`save-folder` 将六处“フォルダ”写入改为“文件夹／폴더／Folder”，复用原生字库映射并填充原空白字格。全部编号、前后空白和存读档代码保持原样，候选仍为六文件；完整三语重建按保留的报告回放这些文字常量。
