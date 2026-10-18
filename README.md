# FARLAND SAGA I Saturn Tribute Localization

<p align="center">
  <b>Language / 语言 / 언어:</b><br>
  <a href="#english">English</a> •
  <a href="#-简体中文">简体中文</a> •
  <a href="#-한국어">한국어</a> •
  <a href="#-community--compute-support">🤝 Support / 协助</a>
</p>

<p align="center">
  <a href="https://github.com/ukjent7/Farland-Saga-localization/actions/workflows/release.yml"><img src="https://github.com/ukjent7/Farland-Saga-localization/actions/workflows/release.yml/badge.svg" alt="CI"></a>
  <a href="https://goreportcard.com/report/github.com/ukjent7/Farland-Saga-localization"><img src="https://goreportcard.com/badge/github.com/ukjent7/Farland-Saga-localization" alt="Go Report Card"></a>
</p>

---

## English

> [!WARNING]
> **Alpha Preview Stage**: This patch is currently in early testing for preview purposes only and should not be considered a final release. Please report any issues you encounter.

A lightweight, fully self-contained localization tool and patch engine for Steam's **FARLAND SAGA I Saturn Tribute**, supporting **English**, **Simplified Chinese (简体中文)**, and **Korean (한국어)**.

Built with a declarative in-place injection engine: 100% free of commercial copyright infringement, self-contained repository with zero external binary dependencies, and a single-executable patcher (~16MB).

### Quick Start (Player Guide)

1. Download the latest `FarlandSagaPatcher.exe` (or zip bundle) from [Releases](https://github.com/ukjent7/Farland-Saga-localization/releases).
2. Place the executable into your Steam game directory (alongside `FarlandSaga.exe`).
3. Launch the patcher:
   - **Install**: Select your language and click **Install**;
   - **Switch**: Click **Back** to select another language;
   - **Restore**: Click **Restore** anytime to return to the pristine, unmodified game.

> [!NOTE]
> Please completely exit the game before patching. The patcher automatically creates a protected backup on first run.

### Key Features

- **Native Injection Engine**: Built-in Saturn CD-ROM MODE1 EDC/ECC recalculation, SSF Steam container cryptography, and RGBA overlay font stream compressor.
- **Format 2 Sparse Compression**: Eliminates bloating and proprietary font embedding; the single executable embeds all 3 languages in just ~16MB.
- **Safe Single-Snapshot Backup**: Atomically verifies SHA-256 hashes before and after writing, rolling back instantly on any mismatch.
- **CI Ready & 100% Compliant**: GitHub Actions automatically builds release binaries in the cloud without proprietary assets.

### Developer Guide

Requires: Linux/WSL2, Python 3.14+ (via `uv`), Go 1.23+.

```bash
# Run unit & regression test suites
bash flow.sh test --cli-only
bash flow.sh test --quick

# Check translation & review page
bash flow.sh merge
bash flow.sh review

# Verify candidates & build standalone Windows patcher
bash flow.sh status
bash flow.sh build-patcher
```

### Disclaimer

- This fan localization is independently developed for educational and research purposes. It contains no proprietary commercial game binaries or copyrighted fonts.
- A legitimate, purchased copy of *FARLAND SAGA I Saturn Tribute* on Steam is required.

---

## 简体中文

> [!WARNING]
> **当前处于 Alpha 阶段**：本补丁仅供尝鲜体验与测试，不可视为正式版本。遇到问题欢迎提交反馈。

Steam 版《**FARLAND SAGA I Saturn Tribute**》的多语言本地化与自动化注入补丁工具，支持**简体中文**、**한국어 (韩文)** 与 **English (英文)**。

项目采用纯声明式注入引擎，公开仓库零侵权、完全自包含，补丁器单文件开箱即用。

### 快速上手（玩家指南）

1. 从 [Releases 页面](https://github.com/ukjent7/Farland-Saga-localization/releases) 下载最新的 `FarlandSagaPatcher.exe`（或 zip 整合包）。
2. 将可执行文件放置在 Steam 游戏根目录（与 `FarlandSaga.exe` 处于同级）。
3. 双击运行补丁器：
   - **安装**：选择目标语言（简体中文 / 한국어 / English），点击「安装」；
   - **切换**：点击「返回」切换其他语言重新安装，程序会自动处理变更；
   - **还原**：随时点击「还原」，即可 100% 恢复为原始原版游戏文件。

> [!NOTE]
> 安装前请确保已完全退出游戏。程序首次安装时会自动建立只读原版备份目录，绝不污染系统环境。

### 项目特性

- **原生级注入**：内置自研 CD-ROM MODE1 扇区 ECC/EDC 重算引擎与 SSF 容器加解密，无需繁重第三方工具。
- **高密度压缩**：采用 Format 2 变长稀疏回放协议，消除虚假膨胀与商业字体依赖，全三语载荷内嵌单文件仅 ~16MB。
- **安全备份体系**：单生命周期备份机制，安装前后进行 SHA-256 校验，任何环节校验不符即刻原子回滚。
- **仓库完全自包含**：云端 GitHub Actions CI 无需依赖任何未公开二进制，秒级全自动构建并发布全功能补丁。

### 开发者指南

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

### 免责声明

- 本项目由玩家社区独立开发维护，仅供游戏交流学习使用，不包含原版商业游戏代码或受版权保护的商业字体；
- 运行补丁需持有合法已购的 Steam 正版《FARLAND SAGA I Saturn Tribute》游戏。

---

## 한국어

> [!WARNING]
> **현재 알파(Alpha) 테스트 단계입니다**: 본 패치는 테스트 및 사전 체험 목적으로 제공되며, 정식 버전이 아닙니다. 문제 발생 시 이슈로 제보해 주시기 바랍니다.

Steam 버전 《**FARLAND SAGA I Saturn Tribute**》를 위한 다국어 현지화 및 자동 인젝션 패치 도구입니다. **한국어**, **간체 중국어 (简体中文)**, **영어 (English)**를 지원합니다.

순수 선언적 패치 엔진을 기반으로 설계되어 상업적 저작권 침해 파일이 없으며, 단일 실행 파일(~16MB)로 즉시 적용 가능합니다.

### 빠른 시작 (사용자 가이드)

1. [Releases 페이지](https://github.com/ukjent7/Farland-Saga-localization/releases)에서 최신 `FarlandSagaPatcher.exe`(또는 zip 압축 파일)를 다운로드합니다.
2. 실행 파일을 Steam 게임 설치 폴더(`FarlandSaga.exe`가 있는 위치)에 넣습니다.
3. 패처를 실행합니다:
   - **설치**: 원하는 언어(한국어 / 简体中文 / English)를 선택한 후 「설치」를 클릭합니다.
   - **언어 변경**: 「뒤로」를 눌러 다른 언어를 선택하여 다시 설치합니다.
   - **원상 복구**: 언제든지 「원래대로 복구」를 클릭하여 원본 파일로 100% 되돌릴 수 있습니다.

> [!NOTE]
> 패치를 진행하기 전에 게임을 완전히 종료해 주십시오. 첫 설치 시 자동으로 안전한 원본 백업이 생성됩니다.

### 주요 특징

- **자체 네이티브 인젝션**: Saturn CD-ROM MODE1 섹터 ECC/EDC 재생성 및 SSF 컨테이너 암호화/복호화 엔진 내장.
- **Format 2 가변 길이 압축**: 불필요한 용량 낭비와 상용 폰트 의존성을 제거하여, 3개 국어 전체를 내장한 단일 실행 파일 용량이 약 16MB에 불과합니다.
- **안전한 백업 및 롤백**: 설치 전후 SHA-256 해시를 검증하며, 오류 발생 시 즉시 원상태로 원자적 롤백을 수행합니다.
- **완전 자립형 CI 빌드**: 외부 바이너리 없이 GitHub Actions 클라우드에서 자동 빌드 및 릴리스를 수행합니다.

### 개발자 가이드

요구 사항: Linux/WSL2, Python 3.14+ (`uv` 관리), Go 1.23+.

```bash
# 단위 및 통합 테스트 실행
bash flow.sh test --cli-only
bash flow.sh test --quick

# 번역 병합 및 리뷰 페이지 생성
bash flow.sh merge
bash flow.sh review

# 빌드 상태 확인 및 독립형 패처 빌드
bash flow.sh status
bash flow.sh build-patcher
```

### 면책 조항

- 본 프로젝트는 유저 커뮤니티에서 연구 및 학습 목적으로 제작한 비공식 패치입니다. 원본 상용 게임 바이너리나 저작권 보호 폰트를 포함하지 않습니다.
- 본 패치를 사용하려면 Steam에서 정식 구매한 《FARLAND SAGA I Saturn Tribute》 게임이 필요합니다.

---

## 🤝 Community & Compute Support

**中文说明**：  
本项目为非营利开源业余项目，非常欢迎大家通过任何形式参与贡献（提交 PR、改进翻译、测试反馈或分享技术思路）。  
> **特别说明与算力协助**：  
> 本项目涉及老旧主机（Saturn SH-2）反汇编逆向、扇区纠错码重算及 Unity 底层二进制注入，技术链路长且复杂。目前开发中所使用的 AI 模型推理性能有限，在排查深层偶发 Bug 及继续完善项目时效率极低（两天内消耗了约 5~6 亿 Token 依然难以定位某些核心症结）。  
> 如果您手头有多余的 **ChatGPT Plus / GPT Team** 账号额度、高阶大模型算力或 API 资源愿意提供给项目开发使用/测试，我们将万分感激！无论以何种方式协助，哪怕只是提供排查思路，都对本项目有着莫大的帮助。

**English**:  
This is a non-profit, hobbyist open-source project. Any form of contribution is warmly welcomed—submitting PRs, refining translations, testing releases, or sharing technical advice.  
> **A Note on Compute & AI Support**:  
> The project involves reverse engineering legacy Sega Saturn (SH-2) code, CD-ROM parity recalculation, and Unity binary patching. Currently, resolving intricate edge-case bugs using lightweight AI models has been heavily bottlenecked (burning through 500~600M tokens over two days with little progress on subtle issues).  
> If anyone has spare **ChatGPT Plus / GPT Team** seats or advanced AI reasoning compute willing to share/lend for project development, it would be immensely appreciated! Any assistance, from debugging insights to compute sharing, makes a huge difference.

**한국어**:  
본 프로젝트는 비영리 오픈소스 프로젝트로, 코드 기여(PR), 번역 감수, 테스트 피드백, 기술 자문 등 어떠한 형태의 참여도 진심으로 환영합니다.  
> **AI 연산 지원 및 협력 안내**:  
> 현재 세가 새턴(SH-2) 어셈블리 역공학, CD-ROM 섹터 패리티 재계산 및 Unity 바이너리 패치 등 복잡한 저수준 작업을 다루고 있습니다. 현재 사용 중인 AI 모델의 한계로 인해 원인 불명의 심층 버그를 추적하고 프로젝트를 개선하는 데 상당한 병목을 겪고 있습니다(이틀간 5~6억 토큰을 소모했음에도 까다로운 이슈 해결에 큰 진전이 없었습니다).  
> 혹시 여유가 있는 **ChatGPT Plus / GPT Team** 계정이나 고급 추론 모델 자원을 프로젝트 개발 지원용으로 공유/대여해 주실 수 있는 분이 계시다면 진심으로 감사하겠습니다! 어떤 형태의 도움과 조언도 프로젝트에 큰 힘이 됩니다.
