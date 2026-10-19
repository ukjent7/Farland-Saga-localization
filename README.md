# FARLAND SAGA Series Saturn Tribute Localization

<p align="center">
  <b>Language:</b>
  English •
  <a href="README_zh-CN.md">简体中文</a> •
  <a href="README_ko-KR.md">한국어</a>
</p>

<p align="center">
  <a href="https://github.com/ukjent7/Farland-Saga-localization/actions/workflows/ci.yml"><img src="https://github.com/ukjent7/Farland-Saga-localization/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://goreportcard.com/report/github.com/ukjent7/Farland-Saga-localization"><img src="https://goreportcard.com/badge/github.com/ukjent7/Farland-Saga-localization" alt="Go Report Card"></a>
</p>

> [!WARNING]
> **Alpha Preview Stage**: This patch is currently in early testing for preview purposes only and should not be considered a final release. Please report any issues you encounter.

A lightweight, fully self-contained localization tool and patch engine for Steam's **FARLAND SAGA I Saturn Tribute** and **FARLAND SAGA II Saturn Tribute**, supporting **English**, **Simplified Chinese (简体中文)**, and **Korean (한국어)**.

Built with declarative in-place injection engines: 100% free of commercial copyright infringement, self-contained repository with zero external binary dependencies, and standalone single-executable patchers for both games generated in a single release.

---

## In-Game Preview

| English | 简体中文 | 한국어 |
| :---: | :---: | :---: |
| ![English Preview](docs/images/preview_en-US.webp) | ![Chinese Preview](docs/images/preview_zh-CN.webp) | ![Korean Preview](docs/images/preview_ko-KR.webp) |

---

## Quick Start (Player Guide)

1. Download the appropriate patcher from [Releases](https://github.com/ukjent7/Farland-Saga-localization/releases) according to your game:
   - **For FARLAND SAGA I**: Download `FarlandSagaPatcher-windows-amd64.exe` and place it in your game root (alongside `FarlandSaga.exe`).
   - **For FARLAND SAGA II**: Download `FarlandSaga2Patcher-windows-amd64.exe` and place it in your game root (alongside `FarlandSaga2.exe`).
2. Launch the patcher:
   - **Install**: Select your language (English / 简体中文 / 한국어) and click **Install**;
   - **Switch**: Click **Back** to select another language;
   - **Restore**: Click **Restore** anytime to return 100% to the pristine, unmodified game.

> [!NOTE]
> Please completely exit the game before patching. The patcher automatically creates a protected backup on first run.

---

## Key Features

- **Native Injection Engine**: Built-in Saturn CD-ROM MODE1 EDC/ECC recalculation, SSF Steam container cryptography, and RGBA overlay font stream compressor.
- **Sparse Compression**: Eliminates bloating and proprietary font embedding; standalone executables embed all 3 languages ready for out-of-the-box use.
- **Safe Single-Snapshot Backup**: Atomically verifies SHA-256 hashes before and after writing, rolling back instantly on any mismatch.
- **CI Ready & 100% Compliant**: GitHub Actions automatically builds and releases both patchers in the cloud without proprietary assets.

---

## 📊 Project Progress & Status

| Title | Phase | Est. Progress | Current Status & Breakdown |
| :--- | :---: | :---: | :--- |
| **FARLAND SAGA I**<br>(First Game) | **Alpha 0.5 Preview** | **~40%** | • **Ready (~40%)**: In-place injection engine, baseline trilingual text, CI test suite, standalone GUI patcher<br>• **Remaining Core Work (~60%)**: Manual triage of runtime rendering/logic bugs (e.g. Korean Option button issue), dialogue polish, full-game playthrough verification |
| **FARLAND SAGA II**<br>(Sequel) | **Alpha Preview** | **~60%** | • **Complete Script & Label Alignment**: All 17 story banks (2,660 dialogue pages) fully aligned in EN/ZH/KO; 518 UI/equipment labels ready;<br>• **Dedicated Standalone Patcher**: `FarlandSaga2Patcher-windows-amd64.exe` ready; zero external dependencies, trilingual GUI with one-click install/restore;<br>• **Review Tool**: Interactive trilingual review page `review_fs2.html` included;<br>• **Next Milestones**: Full-game playthrough regressions and dialogue polishing |

---

## Developer Guide

Requires: Linux/WSL2, Python 3.14+ (via `uv`), Go 1.23+.

```bash
# FARLAND SAGA I testing & building
bash flow.sh test --cli-only
bash flow.sh test --quick
bash flow.sh merge
bash flow.sh review
bash flow.sh status
bash flow.sh build-patcher

# FARLAND SAGA II testing & building
uv run python3 -m unittest discover -s tests -p "test_fs2_*.py"
cd patcher2 && go test ./...
GOOS=windows GOARCH=amd64 go build -trimpath -ldflags='-s -w -H windowsgui' -o ../FarlandSaga2Patcher-windows-amd64.exe .
```

---

## Disclaimer

- This fan localization is independently developed for educational and research purposes. It contains no proprietary commercial game binaries or copyrighted fonts.
- A legitimate, purchased copy of *FARLAND SAGA I Saturn Tribute* or *FARLAND SAGA II Saturn Tribute* on Steam is required.

---

## 🤝 Community & Compute Support

This is a non-profit, hobbyist open-source project. Any form of contribution is warmly welcomed—submitting PRs, refining translations, testing releases, or sharing technical advice.

> **A Note on Compute & AI Support**:  
> The project involves reverse engineering legacy Sega Saturn (SH-2) code, CD-ROM parity recalculation, and Unity binary patching. Currently, resolving intricate edge-case bugs using lightweight AI models has been heavily bottlenecked (burning through 500~600M tokens over two days with little progress on subtle issues).  
> If anyone has spare **ChatGPT Plus / GPT Team** seats or advanced AI reasoning compute willing to share/lend for project development, it would be immensely appreciated! Any assistance, from debugging insights to compute sharing, makes a huge difference.
