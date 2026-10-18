# FARLAND SAGA I Saturn Tribute 本地化项目

<p align="center">
  <b>语言:</b>
  <a href="README.md">English</a> •
  简体中文 •
  <a href="README_ko-KR.md">한국어</a>
</p>

<p align="center">
  <a href="https://github.com/ukjent7/Farland-Saga-localization/actions/workflows/ci.yml"><img src="https://github.com/ukjent7/Farland-Saga-localization/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://goreportcard.com/report/github.com/ukjent7/Farland-Saga-localization"><img src="https://goreportcard.com/badge/github.com/ukjent7/Farland-Saga-localization" alt="Go Report Card"></a>
</p>

> [!WARNING]
> **当前处于 Alpha 阶段**：本补丁仅供尝鲜体验与测试，不可视为正式版本。遇到问题欢迎提交反馈。

Steam 版《**FARLAND SAGA I Saturn Tribute**》的多语言本地化与自动化注入补丁工具，支持**简体中文**、**한국어 (韩文)** 与 **English (英文)**。

项目采用纯声明式注入引擎，公开仓库零侵权、完全自包含，补丁器单文件开箱即用。

---

## 实机效果展示

| 简体中文 | 한국어 | English |
| :---: | :---: | :---: |
| ![中文实机预览](docs/images/preview_zh-CN.webp) | ![韩文实机预览](docs/images/preview_ko-KR.webp) | ![英文实机预览](docs/images/preview_en-US.webp) |

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

- **原生级注入**：内置自研 CD-ROM MODE1 扇区 ECC/EDC 重算引擎与 SSF 容器加解密，无需繁重第三方工具。
- **高密度压缩**：采用 Format 2 变长稀疏回放协议，消除虚假膨胀与商业字体依赖，全三语载荷内嵌单文件仅 ~16MB。
- **安全备份体系**：单生命周期备份机制，安装前后进行 SHA-256 校验，任何环节校验不符即刻原子回滚。
- **仓库完全自包含**：云端 GitHub Actions CI 无需依赖任何未公开二进制，秒级全自动构建并发布全功能补丁。

---

## 开发者指南

环境要求：Linux/WSL2、Python 3.14+（由 `uv` 纳管）、Go 1.23+。

```bash
# 运行单元与集成测试套件
bash flow.sh test --cli-only
bash flow.sh test --quick

# 翻译合并与审阅页生成
bash flow.sh merge
bash flow.sh review

# 校验当前构建候选与编译独立补丁器
bash flow.sh status
bash flow.sh build-patcher
```

---

## 免责声明

- 本项目由玩家社区独立开发维护，仅供游戏交流学习使用，不包含原版商业游戏代码或受版权保护的商业字体；
- 运行补丁需持有合法已购的 Steam 正版《FARLAND SAGA I Saturn Tribute》游戏。

---

## 🤝 社区协助与算力支持

本项目为非营利开源业余项目，非常欢迎大家通过任何形式参与贡献（提交 PR、改进翻译、测试反馈或分享技术思路）。

> **特别说明与算力协助**：  
> 本项目涉及老旧主机（Saturn SH-2）反汇编逆向、扇区纠错码重算及 Unity 底层二进制注入，技术链路长且复杂。目前开发中所使用的 AI 模型推理性能有限，在排查深层偶发 Bug 及继续完善项目时效率极低（两天内消耗了约 5~6 亿 Token 依然难以定位某些核心症结）。  
> 如果您手头有多余的 **ChatGPT Plus / GPT Team** 账号额度、高阶大模型算力或 API 资源愿意提供给项目开发使用/测试，我们将万分感激！无论以何种方式协助，哪怕只是提供排查思路，都对本项目有着莫大的帮助。
