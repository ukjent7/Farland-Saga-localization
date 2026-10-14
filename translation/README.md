# 三语译稿

当前译稿按主题保存在本目录，日文定位与中、韩、英译文共用稳定 ID。旧提取表、AI 批次、交叉审校和冻结副本归入历史包；恢复旧记录不会自动覆盖当前译稿。

| 当前来源 | 内容 |
| --- | --- |
| `corpus.json` | 2,777 条原有普通文字与 9 条状态标签，共 2,786 个稳定 ID |
| `glossary.json` | 90 条术语、分类与审校意见，已有 17 条韩英术语按日文联表 |
| `titles.json` | 50 张图片关卡标题的三语文字与布局 |
| `ui_labels.json` | 地形、装备、界面与属性图片标签 |
| `frontend_submenus.json` | 外层菜单与提示面板的三语主题文字 |

普通文字行的 `translations` 保存 `zh-CN`、`ko-KR`、`en-US`；`reviews` 保存各语言与共同复核记录。源文、文件、偏移、容量、字位、Unity 对象位置等字段用于构建和校验，禁止当作译文编辑。缺少的术语译文留空，不根据旧中文快照或原文自动补写。

```json
{
  "id": "EVE/EVE01_1.EVT:0001",
  "translations": {
    "zh-CN": "久等了！",
    "ko-KR": "늦었지!",
    "en-US": "Sorry!"
  },
  "reviews": {
    "zh-CN": {"status": "draft", "notes": "审校记录"}
  }
}
```

以上仅展示可编辑字段，实际行还包含完整源文和定位元数据。各图片主题沿用自身编码器要求的行结构；无需强行改变稳定的图片布局格式。

中文虚拟 `events/catalog` 视图与韩英构建视图都从 `corpus.json` 读取，不维护重复可编辑副本。旧对齐输入有 1,714 条中文与当前中文不同，迁移时使用当前中文逐 ID 回填；韩英保持已审校完整译稿。原始行与历史意见保存在维护快照和历史译稿包内。

继续翻译时把临时 ID 键译稿放在仓库内 `translation/draft.json`，使用 `bash flow.sh merge --language ko-KR --draft translation/draft.json` 合并；需明确替换已有译文时加 `--overwrite`。无参数 `merge` 只校验当前表。`flow.sh review` 生成[统一审阅页](../review.html)。具体合并、构建和容量规则见 [tools/README.md](../tools/README.md)。

图片、图集、字位与 Unity 的冻结输入在私有工作区的 `assets/sources/`；候选 `inputs/` 保存当次完整输入快照。历史批次由私有工作区的 `flow.sh storage` 命令按原路径恢复，恢复后只作查阅或显式合并，不能重新运行原文提取来覆盖译稿。
