# FARLAND SAGA I Saturn Tribute 本地化项目

[![CI](https://github.com/ukjent7/Farland-Saga-localization/actions/workflows/release.yml/badge.svg)](https://github.com/ukjent7/Farland-Saga-localization/actions/workflows/release.yml)
[![Go Report Card](https://goreportcard.com/badge/github.com/ukjent7/Farland-Saga-localization)](https://goreportcard.com/report/github.com/ukjent7/Farland-Saga-localization)

Steam 版《**FARLAND SAGA I Saturn Tribute**》的多语言本地化与自动化注入补丁工具，支持**简体中文**、**한국어 (韩文)** 与 **English (英文)**。

项目采用纯声明式注入引擎，公开仓库零侵权、完全自包含，补丁器单文件开箱即用。

---

## 快速上手（玩家指南）

1. 从 [Releases 页面](https://github.com/ukjent7/Farland-Saga-localization/releases) 下载最新的 `FarlandSagaPatcher.exe`（或 zip 整合包）。
2. 将可执行文件放置在 Steam 游戏根目录（与 `FarlandSaga.exe` 处于同级）。
3. 双击运行补丁器：
   - **安装**：选择目标语言（简体中文 / 한국어 / English），点击「安装」；
   - **切换**：点击「返回」切换其他语言重新安装，程序会自动处理变更；
   - **还原**：随时点击「还原」，即可 100% 恢复为原始原版游戏文件。

> [!NOTE]
> 安装前请确保已完全退出游戏。程序首次安装时会自动建立只读原版备份目录，绝不污染系统环境。

---

## 项目特性

- **原生级注入**：内置自研 CD-ROM MODE1 扇区 ECC/EDC 重算引擎与 SSF 容器加解密，无需繁重第三方重型工具。
- **高密度压缩**：采用 Format 2 变长稀疏回放协议，消除虚假膨胀与商业字体依赖，全三语载荷内嵌单文件仅 ~16MB。
- **安全备份体系**：单生命周期备份机制，安装前后进行 SHA-256 校验，任何环节校验不符即刻原子回滚。
- **仓库完全自包含**：云端 GitHub Actions CI 无需依赖任何未公开二进制，秒级全自动构建并发布全功能补丁。

---

## 开发者与贡献者指南

### 环境要求

- **OS**：Linux / WSL2 (Ubuntu 22.04+)
- **Python**：3.14+（由 `uv` 自动纳管）
- **Go**：1.23+

### 常用命令

所有日常维护与验证统一由 `flow.sh` 入口驱动：

```bash
# 运行单元与集成测试套件
bash flow.sh test --cli-only
bash flow.sh test --quick

# 翻译合并与审阅页生成
bash flow.sh merge
bash flow.sh review

# 校验当前构建候选与工作区状态
bash flow.sh status
bash flow.sh verify --stage zh-CN-unifont08

# 编译独立 Windows 补丁器
bash flow.sh build-patcher
```

### 目录结构

```text
├── config/patches/         # 声明式补丁配方（UI 布局、字距、价格等）
├── fonts/                  # 开源点阵字库（GNU Unifont）
├── patcher/                # Go 原生跨平台单文件补丁器源码与内嵌载荷
│   ├── payload/            # 预生成的轻量化 Format 2 三语载荷
│   ├── cdrom.go            # Saturn 光盘扇区 EDC/ECC 引擎
│   ├── ssf.go              # SSF 容器加解密引擎
│   └── overlay.go          # Steam 物品说明 RGBA 字体生成与 LZSS 压缩
├── tools/                  # 核心本地化与构建工具链
│   ├── fls/                # 领域分层包（core, disc, containers, text, builders, storage）
│   └── localize.py         # 统一 CLI 入口
├── translation/            # 核心翻译语料（三语对齐 JSON 与术语表）
└── flow.sh                 # 工程流水线总调度脚本
```

---

## 参与翻译

翻译数据集中管理于 `translation/` 目录下：
- [`corpus.json`](translation/corpus.json)：主语料库（剧情剧本、菜单、道具与魔法说明）；
- [`glossary.json`](translation/glossary.json)：术语表与统一校对规范；
- 修改译稿后运行 `bash flow.sh review` 即可在本地生成网页端对照审阅页。

---

## 鸣谢与免责声明

- 本项目由玩家社区独立开发维护，仅供游戏交流学习使用，不包含原版商业游戏代码或受版权保护的商业字体；
- 运行补丁需持有合法已购的 Steam 正版《FARLAND SAGA I Saturn Tribute》游戏。
