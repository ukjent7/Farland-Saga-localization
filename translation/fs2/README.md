# Farland Saga II 翻译数据规范

本目录存放项目的多语言翻译数据，纯文本结构，零版权风险。

## 文件结构

- `glossary.json`: 四语统一术语库（日文原名、简体中文、한국어、English）及角色背景说明。
- `corpus.json`: 全量 2,660 条剧情对白的主语料库，整合日文原句与中、韩、英三语对应译文及槽位状态。
- `ui_labels.json`: UI 菜单、系统界面与物品说明译稿。
- `zh-CN/story.json`: 简体中文按 Bank 对齐的剧情主文件。
- `ko-KR/story.json`: 韩文按 Bank 对齐的剧情主文件。
- `en-US/story.json`: 英文按 Bank 对齐的剧情主文件。

## 排版约束

所有翻译须遵守 Saturn 硬件 196×48 2bpp 限制：
- 最多 3 行，每行高度 16 像素；
- 字体缩放阶梯：16px / 14px / 12px；
- 英文支持 word wrap 自动折行；韩文支持空格词间折行与超长音节折行；中文支持字符级断行。
