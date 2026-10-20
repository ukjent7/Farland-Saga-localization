// 界面文字表。
//
// GUI 里所有面向用户的文字集中在这一个结构体里，按语言代码取表，取不到的语言
// 回落英文表。表本身不依赖 Windows，Linux 上也能跑测试逐字段核对，避免只在中
// 文 Windows 上才发现漏译。
//
// 两种「语言名」要分开，这是整个设计里唯一容易搞混的地方：
//   - nativeLanguageName 返回语言自己的名字（简体中文 / 한국어 / English），
//     只用于第一屏那三个按钮。用户在这一步还没选语言，按钮必须自己说自己，
//     写「韩文」韩国人看不懂。
//   - uiStrings.name 用「当前界面语言」称呼各语言（중국어 / 한국어 / 영어），
//     用于第二屏标题、状态行和安装结果——那时候界面已经是所选语言了。
package main

// uiStrings 是一套完整的界面文字。新增字段时三张表都要补齐，测试会检查。

// appTitle 是窗口标题，也是消息框的标题栏。三种语言共用同一个：程序本身是
// 「三语补丁器」，不属于任何一种语言，没有必要按语言各写一个，更不该在某一
// 语言里自称是那一语言的专用补丁。它按作品定义在 game_saga1.go / game_saga2.go，
// 不放进文字表——放表里三张表迟早会各自漂移。

type uiStrings struct {
	// nameChinese / nameKorean / nameEnglish 是用当前界面语言称呼三种语言。
	nameChinese string
	nameKorean  string
	nameEnglish string

	// fontFace 是这一屏使用的字体。韩文用 Malgun Gothic，其余用系统对话框
	// 字体；两者在所有受支持的 Windows 上都存在，缺了 CreateFont 也会替换。
	fontFace string

	pageTitleFormat        string // 第二屏标题，例如「%s 补丁」
	statusNotInstalled     string
	statusInstalledFormat  string // 「已安装 %s」
	buttonInstall          string
	buttonRestore          string
	buttonBack             string
	notProvided            string // 语言按钮：这个语言没有内嵌补丁
	installedTag           string // 语言按钮后缀：这个语言已安装
	hintNoGameRoot         string // 第一屏提示：程序不在游戏根目录
	hintNoGameRootLong     string // 安装/还原时才发现不在游戏根目录
	busyInstallFormat      string // 「正在安装 %s 补丁…」
	busyRestore            string
	progressFormat         string // 「正在写入 %d%%」
	confirmInstallFormat   string
	confirmInstallCaption  string // 确认框标题，例如「安装 %s」
	confirmRestore         string
	confirmRestoreCaption  string
	alreadyInstalled       string // 装完回来发现本来就已经装过
	alreadyInstalledFormat string // 点安装时发现已经装过
	installedDoneFormat    string
	restoreDoneFormat      string // 「已还原原版，共 %d 个文件。」
	nothingInstalled       string // 点还原时发现没装
	busyClose              string // 忙碌时关窗口
	timeout                string
	internalErrorFormat    string
}

// name 用当前界面语言返回某个语言代码的名字。
func (u uiStrings) name(code string) string {
	switch code {
	case "zh-CN":
		return u.nameChinese
	case "ko-KR":
		return u.nameKorean
	case "en-US":
		return u.nameEnglish
	}
	return code
}

var (
	uiChinese = uiStrings{
		nameChinese: "简体中文",
		nameKorean:  "韩文",
		nameEnglish: "英文",
		fontFace:    "MS Shell Dlg 2",

		pageTitleFormat:        "%s 补丁",
		statusNotInstalled:     "尚未安装",
		statusInstalledFormat:  "已安装 %s",
		buttonInstall:          "安装",
		buttonRestore:          "还原",
		buttonBack:             "返回",
		notProvided:            "（未提供）",
		installedTag:           "（已安装）",
		hintNoGameRoot:         "未检测到游戏目录，请把本程序放到游戏根目录",
		hintNoGameRootLong:     "未检测到游戏目录，请把本程序放到游戏根目录后再运行。",
		busyInstallFormat:      "正在安装 %s 补丁…",
		busyRestore:            "正在还原原版…",
		progressFormat:         "正在写入 %d%%",
		confirmInstallFormat:   "现在安装 %s 补丁吗？\n\n安装前请先退出游戏。",
		confirmInstallCaption:  "安装 %s",
		confirmRestore:         "用原版备份覆盖当前已打补丁的文件吗？",
		confirmRestoreCaption:  "还原原版",
		alreadyInstalled:       "补丁已经安装。",
		alreadyInstalledFormat: "%s 补丁已经安装，无需重复操作。",
		installedDoneFormat:    "已安装 %s 补丁。\n\n需要还原时点「还原」。",
		restoreDoneFormat:      "已还原原版，共 %d 个文件。",
		nothingInstalled:       "尚未安装补丁，无需还原。",
		busyClose:              "正在写入文件，请等待当前操作完成后再关闭窗口。",
		timeout:                "操作超时，界面已恢复，请重新操作确认文件状态。",
		internalErrorFormat:    "内部错误：%v",
	}

	uiKorean = uiStrings{
		nameChinese: "중국어(간체)",
		nameKorean:  "한국어",
		nameEnglish: "영어",
		fontFace:    "Malgun Gothic",

		pageTitleFormat:        "%s 패치",
		statusNotInstalled:     "설치되지 않음",
		statusInstalledFormat:  "%s 설치됨",
		buttonInstall:          "설치",
		buttonRestore:          "복원",
		buttonBack:             "뒤로",
		notProvided:            "(제공 안 됨)",
		installedTag:           "(설치됨)",
		hintNoGameRoot:         "게임 폴더를 찾지 못했습니다. 이 프로그램을 게임 폴더에 넣어 주세요.",
		hintNoGameRootLong:     "게임 폴더를 찾지 못했습니다. 프로그램을 게임 폴더에 넣고 다시 실행해 주세요.",
		busyInstallFormat:      "%s 패치를 설치하는 중…",
		busyRestore:            "원본 파일을 복원하는 중…",
		progressFormat:         "파일을 쓰는 중 %d%%",
		confirmInstallFormat:   "%s 패치를 지금 설치할까요?\n\n설치하기 전에 게임을 종료해 주세요.",
		confirmInstallCaption:  "%s 설치",
		confirmRestore:         "원본 백업으로 현재 패치된 파일을 덮어쓸까요?",
		confirmRestoreCaption:  "원본 복원",
		alreadyInstalled:       "패치가 이미 설치되어 있습니다.",
		alreadyInstalledFormat: "%s 패치는 이미 설치되어 있습니다.",
		installedDoneFormat:    "%s 패치를 설치했습니다.\n\n복원하려면 「복원」을 누르세요.",
		restoreDoneFormat:      "원본을 복원했습니다. 파일 %d개.",
		nothingInstalled:       "설치된 패치가 없어 복원할 것이 없습니다.",
		busyClose:              "파일을 쓰는 중입니다. 현재 작업이 끝난 뒤에 창을 닫아 주세요.",
		timeout:                "작업 시간이 초과되어 인터페이스를 복구했습니다. 파일 상태를 다시 확인해 주세요.",
		internalErrorFormat:    "내부 오류: %v",
	}

	uiEnglish = uiStrings{
		nameChinese: "Simplified Chinese",
		nameKorean:  "Korean",
		nameEnglish: "English",
		fontFace:    "MS Shell Dlg 2",

		pageTitleFormat:        "%s patch",
		statusNotInstalled:     "Not installed",
		statusInstalledFormat:  "Installed: %s",
		buttonInstall:          "Install",
		buttonRestore:          "Restore",
		buttonBack:             "Back",
		notProvided:            "(not available)",
		installedTag:           " (installed)",
		hintNoGameRoot:         "Game folder not found. Put this program in the game folder.",
		hintNoGameRootLong:     "Game folder not found. Put this program in the game folder and run it again.",
		busyInstallFormat:      "Installing the %s patch…",
		busyRestore:            "Restoring the original files…",
		progressFormat:         "Writing files… %d%%",
		confirmInstallFormat:   "Install the %s patch now?\n\nPlease quit the game first.",
		confirmInstallCaption:  "Install %s",
		confirmRestore:         "Overwrite the patched files with the original backup?",
		confirmRestoreCaption:  "Restore original",
		alreadyInstalled:       "The patch is already installed.",
		alreadyInstalledFormat: "The %s patch is already installed.",
		installedDoneFormat:    "The %s patch is installed.\n\nUse “Restore” to go back to the original files.",
		restoreDoneFormat:      "Original files restored, %d files in total.",
		nothingInstalled:       "No patch is installed, so there is nothing to restore.",
		busyClose:              "Files are being written. Please wait for the current operation to finish before closing the window.",
		timeout:                "The operation timed out and the interface has recovered. Please check the file state again.",
		internalErrorFormat:    "Internal error: %v",
	}
)

// uiStringsFor 取某个语言的界面文字，未知语言回落英文。
func uiStringsFor(code string) uiStrings {
	switch code {
	case "zh-CN":
		return uiChinese
	case "ko-KR":
		return uiKorean
	}
	return uiEnglish
}

// nativeLanguageName 返回语言自己的名字，只给第一屏的三个按钮用。
func nativeLanguageName(code string) string {
	switch code {
	case "zh-CN":
		return "简体中文"
	case "ko-KR":
		return "한국어"
	case "en-US":
		return "English"
	}
	return code
}
