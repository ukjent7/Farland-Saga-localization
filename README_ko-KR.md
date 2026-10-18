# 파랜드 사가 I 새턴 트리뷰트 한국어 패치 프로젝트

<p align="center">
  <b>언어:</b>
  <a href="README.md">English</a> •
  <a href="README_zh-CN.md">简体中文</a> •
  한국어
</p>

<p align="center">
  <a href="https://github.com/ukjent7/Farland-Saga-localization/actions/workflows/ci.yml"><img src="https://github.com/ukjent7/Farland-Saga-localization/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://goreportcard.com/report/github.com/ukjent7/Farland-Saga-localization"><img src="https://goreportcard.com/badge/github.com/ukjent7/Farland-Saga-localization" alt="Go Report Card"></a>
</p>

> [!WARNING]
> **현재 알파(Alpha) 테스트 단계입니다**: 본 패치는 테스트 및 사전 체험 목적으로 제공되며, 정식 버전이 아닙니다. 문제 발생 시 이슈로 제보해 주시기 바랍니다.

Steam 버전 《**FARLAND SAGA I Saturn Tribute**》를 위한 다국어 현지화 및 자동 인젝션 패치 도구입니다. **한국어**, **간체 중국어 (简体中文)**, **영어 (English)**를 지원합니다.

순수 선언적 패치 엔진을 기반으로 설계되어 상업적 저작권 침해 파일이 없으며, 단일 실행 파일(~16MB)로 즉시 적용 가능합니다.

---

## 인게임 미리보기

| 한국어 | 简体中文 | English |
| :---: | :---: | :---: |
| ![한국어 인게임 미리보기](docs/images/preview_ko-KR.webp) | ![중국어 인게임 미리보기](docs/images/preview_zh-CN.webp) | ![영어 인게임 미리보기](docs/images/preview_en-US.webp) |

---

## 빠른 시작 (사용자 가이드)

1. [Releases 페이지](https://github.com/ukjent7/Farland-Saga-localization/releases)에서 최신 `FarlandSagaPatcher.exe`(또는 zip 압축 파일)를 다운로드합니다.
2. 실행 파일을 Steam 게임 설치 폴더(`FarlandSaga.exe`가 있는 위치)에 넣습니다.
3. 패처를 실행합니다:
   - **설치**: 원하는 언어(한국어 / 简体中文 / English)를 선택한 후 「설치」를 클릭합니다.
   - **언어 변경**: 「뒤로」를 눌러 다른 언어를 선택하여 다시 설치합니다.
   - **원상 복구**: 언제든지 「원래대로 복구」를 클릭하여 원본 파일로 100% 되돌릴 수 있습니다.

> [!NOTE]
> 패치를 진행하기 전에 게임을 완전히 종료해 주십시오. 첫 설치 시 자동으로 안전한 원본 백업이 생성됩니다.

---

## 주요 특징

- **자체 네이티브 인젝션**: Saturn CD-ROM MODE1 섹터 ECC/EDC 재생성 및 SSF 컨테이너 암호화/복호화 엔진 내장.
- **Format 2 가변 길이 압축**: 불필요한 용량 낭비와 상용 폰트 의존성을 제거하여, 3개 국어 전체를 내장한 단일 실행 파일 용량이 약 16MB에 불과합니다.
- **안전한 백업 및 롤백**: 설치 전후 SHA-256 해시를 검증하며, 오류 발생 시 즉시 원상태로 원자적 롤백을 수행합니다.
- **완전 자립형 CI 빌드**: 외부 바이너리 없이 GitHub Actions 클라우드에서 자동 빌드 및 릴리스를 수행합니다.

---

## 개발자 가이드

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

---

## 면책 조항

- 본 프로젝트는 유저 커뮤니티에서 연구 및 학습 목적으로 제작한 비공식 패치입니다. 원본 상용 게임 바이너리나 저작권 보호 폰트를 포함하지 않습니다.
- 본 패치를 사용하려면 Steam에서 정식 구매한 《FARLAND SAGA I Saturn Tribute》 게임이 필요합니다.

---

## 🤝 커뮤니티 지원 및 연산 자원 협력

본 프로젝트는 비영리 오픈소스 프로젝트로, 코드 기여(PR), 번역 감수, 테스트 피드백, 기술 자문 등 어떠한 형태의 참여도 진심으로 환영합니다.

> **AI 연산 지원 및 협력 안내**:  
> 현재 세가 새턴(SH-2) 어셈블리 역공학, CD-ROM 섹터 패리티 재계산 및 Unity 바이너리 패치 등 복잡한 저수준 작업을 다루고 있습니다. 현재 사용 중인 AI 모델의 한계로 인해 원인 불명의 심층 버그를 추적하고 프로젝트를 개선하는 데 상당한 병목을 겪고 있습니다(이틀간 5~6억 토큰을 소모했음에도 까다로운 이슈 해결에 큰 진전이 없었습니다).  
> 혹시 여유가 있는 **ChatGPT Plus / GPT Team** 계정이나 고급 추론 모델 자원을 프로젝트 개발 지원용으로 공유/대여해 주실 수 있는 분이 계시다면 진심으로 감사하겠습니다! 어떤 형태의 도움과 조언도 프로젝트에 큰 힘이 됩니다.
