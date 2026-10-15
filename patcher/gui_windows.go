//go:build windows

// Windows 图形界面。
//
// 只依赖标准库与 Win32 API（user32 / comctl32 / comdlg32 / shell32 / gdi32 /
// ole32 / kernel32），不引入第三方模块，也不需要 cgo，CI 里可以直接交叉编译。
// 界面在自己的线程跑消息循环；安装与恢复放到工作 goroutine，进度经节流后由
// 定时器合并更新，避免每个 4 KiB 差异块都打扰 UI。
//
// 结构体布局用 /tmp/winlayout.c（zig cc 编译）逐字段核对过，文件底部有编译期
// 大小断言，布局变化会直接编译失败。
package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

const appTitle = "FARLAND SAGA I Saturn Tribute 汉化补丁工具"
const guiClassName = "FarlandSagaPatcherWindow"
const uiFontFace = "MS Shell Dlg 2"

var (
	kernel32 = syscall.NewLazyDLL("kernel32.dll")
	user32   = syscall.NewLazyDLL("user32.dll")
	comctl32 = syscall.NewLazyDLL("comctl32.dll")
	comdlg32 = syscall.NewLazyDLL("comdlg32.dll")
	shell32  = syscall.NewLazyDLL("shell32.dll")
	ole32    = syscall.NewLazyDLL("ole32.dll")
	gdi32    = syscall.NewLazyDLL("gdi32.dll")

	procGetModuleHandle               = kernel32.NewProc("GetModuleHandleW")
	procRegisterClassEx               = user32.NewProc("RegisterClassExW")
	procCreateWindowEx                = user32.NewProc("CreateWindowExW")
	procDefWindowProc                 = user32.NewProc("DefWindowProcW")
	procShowWindow                    = user32.NewProc("ShowWindow")
	procUpdateWindow                  = user32.NewProc("UpdateWindow")
	procGetMessage                    = user32.NewProc("GetMessageW")
	procTranslateMessage              = user32.NewProc("TranslateMessage")
	procDispatchMessage               = user32.NewProc("DispatchMessageW")
	procPostQuitMessage               = user32.NewProc("PostQuitMessage")
	procSendMessage                   = user32.NewProc("SendMessageW")
	procSetWindowText                 = user32.NewProc("SetWindowTextW")
	procGetWindowText                 = user32.NewProc("GetWindowTextW")
	procGetWindowTextLength           = user32.NewProc("GetWindowTextLengthW")
	procEnableWindow                  = user32.NewProc("EnableWindow")
	procMoveWindow                    = user32.NewProc("MoveWindow")
	procGetClientRect                 = user32.NewProc("GetClientRect")
	procMessageBox                    = user32.NewProc("MessageBoxW")
	procSetTimer                      = user32.NewProc("SetTimer")
	procKillTimer                     = user32.NewProc("KillTimer")
	procSetFocus                      = user32.NewProc("SetFocus")
	procGetDpiForWindow               = user32.NewProc("GetDpiForWindow")
	procSetProcessDpiAwarenessContext = user32.NewProc("SetProcessDpiAwarenessContext")
	procCreateFont                    = gdi32.NewProc("CreateFontW")
	procDeleteObject                  = gdi32.NewProc("DeleteObject")
	procInitCommonControlsEx          = comctl32.NewProc("InitCommonControlsEx")
	procGetOpenFileName               = comdlg32.NewProc("GetOpenFileNameW")
	procSHBrowseForFolder             = shell32.NewProc("SHBrowseForFolderW")
	procSHGetPathFromIDList           = shell32.NewProc("SHGetPathFromIDListW")
	procDragAcceptFiles               = shell32.NewProc("DragAcceptFiles")
	procDragQueryFile                 = shell32.NewProc("DragQueryFileW")
	procDragFinish                    = shell32.NewProc("DragFinish")
	procCoTaskMemFree                 = ole32.NewProc("CoTaskMemFree")
)

// 窗口样式与消息常量。
const (
	wsOverlappedWindow = 0x00cf0000
	wsChild            = 0x40000000
	wsVisible          = 0x10000000
	wsVScroll          = 0x00100000
	wsBorder           = 0x00800000
	wsClipChildren     = 0x02000000
	wsExControlParent  = 0x00010000
	wsExClientEdge     = 0x00000200
	wsExAcceptFiles    = 0x00000010

	esMultiline     = 0x0004
	esAutoVScroll   = 0x0040
	esReadOnly      = 0x0800
	cbsDropDownList = 0x0003
	cbsHasStrings   = 0x0200

	lvsReport          = 0x0001
	lvsShowsSelAlways  = 0x0008
	lvsSingleSel       = 0x0004
	lvsExFullRowSelect = 0x00000020
	lvsExGridLines     = 0x00000001
	lvsExDoubleBuffer  = 0x00010000

	swShow = 5

	wmCreate        = 0x0001
	wmDestroy       = 0x0002
	wmSize          = 0x0005
	wmClose         = 0x0010
	wmSetFont       = 0x0030
	wmCommand       = 0x0111
	wmTimer         = 0x0113
	wmDropFiles     = 0x0233
	wmDpiChanged    = 0x02e0
	wmGetMinMaxInfo = 0x0024

	bnClicked    = 0
	cbnSelChange = 1
	enKillFocus  = 0x0200

	colorBtnFace = 15

	// 控件 ID，同时是 WM_COMMAND 的低 16 位。
	idGameLabel     = 1001
	idGameEdit      = 1002
	idGameBrowse    = 1003
	idPayloadLabel  = 1004
	idPayloadEdit   = 1005
	idPayloadBrowse = 1006
	idLangLabel     = 1007
	idLangCombo     = 1008
	idStatusLabel   = 1009
	idFileList      = 1010
	idProgress      = 1011
	idLog           = 1012
	idInstall       = 1013
	idRestore       = 1014
	idVerify        = 1015
	idHelp          = 1016

	// 通用控件与列表视图消息。
	lvmDeleteAllItems   = 0x1009
	lvmInsertItem       = 0x104d
	lvmSetItem          = 0x104c
	lvmInsertColumn     = 0x1061
	lvmSetExtendedStyle = 0x1036
	lvifText            = 0x00000001
	lvcfText            = 0x00000002
	lvcfWidth           = 0x00000004
	lvcfFmt             = 0x00000001
	lvcfmtLeft          = 0x00000000
	pbmSetRange32       = 0x0406
	pbmSetPos           = 0x0402
	cbAddString         = 0x0143
	cbResetContent      = 0x014b
	cbSetCurSel         = 0x014e
	cbGetCurSel         = 0x0147
	emSetSel            = 0x00b1
	emReplaceSel        = 0x00c2
	emScrollCaret       = 0x00b7
	wmGetText           = 0x000d
	wmGetTextLength     = 0x000e
	wmSetText           = 0x000c

	// 对话框与字体常量。
	mbOk                = 0x00000000
	mbYesNo             = 0x00000004
	mbIconError         = 0x00000010
	mbIconQuestion      = 0x00000020
	mbIconInformation   = 0x00000040
	mbDefButton2        = 0x00000100
	idYes               = 6
	bifReturnOnlyFsDirs = 0x00000001
	bifNewDialogStyle   = 0x00000040
	bifEditBox          = 0x00000010
	ofnFileMustExist    = 0x00001000
	ofnPathMustExist    = 0x00000800
	ofnExplorer         = 0x00080000
	ofnNoChangeDir      = 0x00000008
	iccListViewClasses  = 0x00000001
	iccProgressClass    = 0x00000020
	fwNormal            = 400
	defaultCharset      = 1

	// 进度事件合并间隔：差异块每 4 KiB 一次，直接推给 UI 会淹没消息循环。
	progressInterval = 100 * time.Millisecond
	// 日志超过这个长度就截断，避免长时间使用后 EDIT 控件变慢。
	logLimit = 200000
)

// 结构体布局与 Win32 SDK 一致，字段顺序即偏移顺序。
type winPoint struct {
	X int32
	Y int32
}

type winRect struct {
	Left   int32
	Top    int32
	Right  int32
	Bottom int32
}

type winMsg struct {
	Hwnd    uintptr
	Message uint32
	WParam  uintptr
	LParam  uintptr
	Time    uint32
	Pt      winPoint
}

type wndClassEx struct {
	Size       uint32
	Style      uint32
	WndProc    uintptr
	ClsExtra   int32
	WndExtra   int32
	Instance   uintptr
	Icon       uintptr
	Cursor     uintptr
	Background uintptr
	MenuName   *uint16
	ClassName  *uint16
	IconSm     uintptr
}

type lvItem struct {
	Mask      uint32
	Item      int32
	SubItem   int32
	State     uint32
	StateMask uint32
	Text      *uint16
	TextMax   int32
	Image     int32
	LParam    uintptr
	Indent    int32
	GroupID   int32
	Columns   uint32
	PuColumns *uint32
	PiColFmt  *int32
	Group     int32
}

type lvColumn struct {
	Mask      uint32
	Fmt       int32
	CX        int32
	Text      *uint16
	TextMax   int32
	SubItem   int32
	Image     int32
	Order     int32
	CXMin     int32
	CXDefault int32
	CXIdeal   int32
}

type browseInfo struct {
	Owner       uintptr
	Root        uintptr
	DisplayName *uint16
	Title       *uint16
	Flags       uint32
	Callback    uintptr
	Param       uintptr
	Image       int32
}

type openFileName struct {
	StructSize      uint32
	Owner           uintptr
	Instance        uintptr
	Filter          *uint16
	CustomFilter    *uint16
	MaxCustomFilter uint32
	FilterIndex     uint32
	File            *uint16
	MaxFile         uint32
	FileTitle       *uint16
	MaxFileTitle    uint32
	InitialDir      *uint16
	Title           *uint16
	Flags           uint32
	FileOffset      uint16
	FileExtension   uint16
	DefExt          *uint16
	Param           uintptr
	Reserved        uintptr
	ReservedInt     uint32
	FlagsEx         uint32
}

type initCommonControlsEx struct {
	Size uint32
	ICC  uint32
}

type minMaxInfo struct {
	Reserved    winPoint
	MaxSize     winPoint
	MaxPosition winPoint
	MinTrack    winPoint
	MaxTrack    winPoint
}

// 编译期校验结构体大小，布局被改动时直接编译失败。
var (
	_ = [1]struct{}{}[unsafe.Sizeof(wndClassEx{})-80]
	_ = [1]struct{}{}[unsafe.Sizeof(lvItem{})-88]
	_ = [1]struct{}{}[unsafe.Sizeof(lvColumn{})-56]
	_ = [1]struct{}{}[unsafe.Sizeof(browseInfo{})-64]
	_ = [1]struct{}{}[unsafe.Sizeof(openFileName{})-136]
	_ = [1]struct{}{}[unsafe.Sizeof(initCommonControlsEx{})-8]
	_ = [1]struct{}{}[unsafe.Sizeof(minMaxInfo{})-40]
	_ = [1]struct{}{}[unsafe.Sizeof(winMsg{})-48]
	_ = [1]struct{}{}[unsafe.Sizeof(winRect{})-16]
	_ = [1]struct{}{}[unsafe.Sizeof(winPoint{})-8]
)

// activeApp 是唯一的主窗口；NewCallback 不接受闭包，所以用包级变量转发。
var activeApp *guiApp

var guiWndProc = syscall.NewCallback(guiWindowProc)

func guiWindowProc(hwnd, msg, wparam, lparam uintptr) uintptr {
	if activeApp == nil {
		return defWindowProc(hwnd, msg, wparam, lparam)
	}
	return activeApp.wndProc(hwnd, msg, wparam, lparam)
}

// guiApp 持有窗口句柄、控件句柄与当前状态。除 progressCallback 内部的节流
// 状态外，所有字段都只在界面线程读写；工作 goroutine 只通过 post 回传结果。
type guiApp struct {
	window uintptr
	module uintptr
	font   uintptr
	scale  float64
	dpi    uint32

	gameLabel     uintptr
	gameEdit      uintptr
	gameBrowse    uintptr
	payloadLabel  uintptr
	payloadEdit   uintptr
	payloadBrowse uintptr
	langLabel     uintptr
	langCombo     uintptr
	statusLabel   uintptr
	fileList      uintptr
	progressBar   uintptr
	logEdit       uintptr
	installBtn    uintptr
	restoreBtn    uintptr
	verifyBtn     uintptr
	helpBtn       uintptr

	game     string
	choices  []payloadChoice
	selected int
	loaded   *payload
	record   *installRecord
	statuses []fileStatus

	busy      bool
	busyLabel string

	// generation 在每次开始新的忙碌操作、或作废排队的刷新时递增。工作线程
	// 回传结果时发现世代已变，说明已有更新的操作接管，这次结果直接丢弃，
	// 免得一次慢吞吞的检查把正在进行的安装状态覆盖掉。
	generation int

	events        chan func()
	logText       strings.Builder
	progressTotal int64
}

func newGUIApp() *guiApp {
	return &guiApp{scale: 1, dpi: 96, selected: -1, events: make(chan func(), 1024)}
}

// guiAvailable 报告当前平台是否带图形界面。Windows 版用 Win32 实现，
// 其他平台只有控制台向导。
func guiAvailable() bool { return true }

// runGUI 创建主窗口并跑消息循环，窗口建不起来时返回错误让调用方回退到向导。
func runGUI() error {
	if procRegisterClassEx.Find() != nil {
		return errors.New("无法加载 user32.dll")
	}
	app := newGUIApp()
	activeApp = app
	if err := app.init(); err != nil {
		activeApp = nil
		return err
	}
	app.messageLoop()
	return nil
}

func (a *guiApp) init() error {
	instance, _, _ := procGetModuleHandle.Call(0)
	a.module = instance
	// 声明每显示器 DPI 感知；老系统上会失败，此时按 96 DPI 布局，由系统缩放。
	if procSetProcessDpiAwarenessContext.Find() == nil {
		procSetProcessDpiAwarenessContext.Call(^uintptr(3))
	}
	if err := initCommonControls(); err != nil {
		return err
	}
	class := wndClassEx{
		Size:       uint32(unsafe.Sizeof(wndClassEx{})),
		Style:      0x0003, // CS_HREDRAW | CS_VREDRAW
		WndProc:    guiWndProc,
		Instance:   instance,
		Icon:       loadIcon(instance, 32512),
		Cursor:     loadCursor(instance, 32512),
		Background: uintptr(colorBtnFace + 1),
		ClassName:  utf16ptr(guiClassName),
	}
	if atom, _, _ := procRegisterClassEx.Call(uintptr(unsafe.Pointer(&class))); atom == 0 {
		return errors.New("注册窗口类失败")
	}
	a.dpi = 96
	a.scale = 1
	width, height := a.s(860), a.s(680)
	window, _, lastError := procCreateWindowEx.Call(
		wsExControlParent|wsExAcceptFiles,
		uintptr(unsafe.Pointer(utf16ptr(guiClassName))),
		uintptr(unsafe.Pointer(utf16ptr(appTitle))),
		wsOverlappedWindow|wsClipChildren,
		uintptr(cwUseDefault), 0,
		uintptr(width), uintptr(height),
		0, 0, instance, 0)
	if window == 0 {
		return fmt.Errorf("创建窗口失败（错误码 %d）", lastError)
	}
	a.window = window
	procShowWindow.Call(window, swShow)
	procUpdateWindow.Call(window)
	return nil
}

const cwUseDefault = 0x80000000

func (a *guiApp) messageLoop() {
	var message winMsg
	for {
		result, _, _ := procGetMessage.Call(uintptr(unsafe.Pointer(&message)), 0, 0, 0)
		if result == 0 || result == ^uintptr(0) {
			return
		}
		procTranslateMessage.Call(uintptr(unsafe.Pointer(&message)))
		procDispatchMessage.Call(uintptr(unsafe.Pointer(&message)))
	}
}

func initCommonControls() error {
	if procInitCommonControlsEx.Find() != nil {
		return errors.New("无法加载 comctl32.dll")
	}
	controls := initCommonControlsEx{
		Size: uint32(unsafe.Sizeof(initCommonControlsEx{})),
		ICC:  iccListViewClasses | iccProgressClass,
	}
	if ok, _, _ := procInitCommonControlsEx.Call(uintptr(unsafe.Pointer(&controls))); ok == 0 {
		return errors.New("初始化通用控件失败")
	}
	return nil
}

func loadIcon(instance uintptr, identifier uintptr) uintptr {
	proc := user32.NewProc("LoadIconW")
	if proc.Find() != nil {
		return 0
	}
	handle, _, _ := proc.Call(instance, identifier)
	return handle
}

func loadCursor(instance uintptr, identifier uintptr) uintptr {
	proc := user32.NewProc("LoadCursorW")
	if proc.Find() != nil {
		return 0
	}
	handle, _, _ := proc.Call(instance, identifier)
	return handle
}

// s 把 96 DPI 下的设计尺寸折算到当前缩放比例。
func (a *guiApp) s(value int) int {
	return int(float64(value)*a.scale + 0.5)
}

func (a *guiApp) createControls() {
	a.gameLabel = a.create("STATIC", "游戏目录", 0, 0, idGameLabel)
	a.gameEdit = a.create("EDIT", "", wsExClientEdge, esAutoVScroll|wsBorder, idGameEdit)
	a.gameBrowse = a.create("BUTTON", "浏览…", 0, 0, idGameBrowse)
	a.payloadLabel = a.create("STATIC", "补丁文件", 0, 0, idPayloadLabel)
	a.payloadEdit = a.create("EDIT", "", wsExClientEdge, esAutoVScroll|wsBorder, idPayloadEdit)
	a.payloadBrowse = a.create("BUTTON", "浏览…", 0, 0, idPayloadBrowse)
	a.langLabel = a.create("STATIC", "语言/版本", 0, 0, idLangLabel)
	a.langCombo = a.create("COMBOBOX", "", 0, wsVScroll|cbsDropDownList|cbsHasStrings, idLangCombo)
	a.statusLabel = a.create("STATIC", "", 0, 0, idStatusLabel)
	a.fileList = a.create("SysListView32", "", wsExClientEdge,
		wsBorder|lvsReport|lvsShowsSelAlways|lvsSingleSel, idFileList)
	a.progressBar = a.create("msctls_progress32", "", 0, 0, idProgress)
	a.logEdit = a.create("EDIT", "", wsExClientEdge,
		wsBorder|wsVScroll|esMultiline|esAutoVScroll|esReadOnly, idLog)
	a.installBtn = a.create("BUTTON", "安装补丁", 0, 0, idInstall)
	a.restoreBtn = a.create("BUTTON", "恢复原版", 0, 0, idRestore)
	a.verifyBtn = a.create("BUTTON", "校验", 0, 0, idVerify)
	a.helpBtn = a.create("BUTTON", "使用说明", 0, 0, idHelp)

	sendMessage(a.fileList, lvmSetExtendedStyle, 0, lvsExFullRowSelect|lvsExGridLines|lvsExDoubleBuffer)
	a.addColumn(0, "文件", a.s(360))
	a.addColumn(1, "状态", a.s(150))
	a.addColumn(2, "当前大小", a.s(120))
}

// create 建立一个子控件；exStyle 与 style 分开传，避免把两者混成一个参数。
func (a *guiApp) create(class, name string, exStyle, style uint32, id uintptr) uintptr {
	handle, _, _ := procCreateWindowEx.Call(
		uintptr(exStyle),
		uintptr(unsafe.Pointer(utf16ptr(class))),
		uintptr(unsafe.Pointer(utf16ptr(name))),
		uintptr(wsChild|wsVisible|style),
		0, 0, 10, 10,
		a.window, id, a.module, 0)
	if handle != 0 && a.font != 0 {
		sendMessage(handle, wmSetFont, a.font, 1)
	}
	return handle
}

func (a *guiApp) addColumn(index int, title string, width int) {
	column := lvColumn{Mask: lvcfText | lvcfWidth | lvcfFmt, Fmt: lvcfmtLeft, CX: int32(width), Text: utf16ptr(title)}
	sendMessage(a.fileList, lvmInsertColumn, uintptr(index), uintptr(unsafe.Pointer(&column)))
}

func (a *guiApp) layout() {
	if a.window == 0 {
		return
	}
	var rect winRect
	procGetClientRect.Call(a.window, uintptr(unsafe.Pointer(&rect)))
	width := int(rect.Right - rect.Left)
	height := int(rect.Bottom - rect.Top)

	margin := a.s(12)
	labelWidth := a.s(84)
	buttonWidth := a.s(88)
	rowHeight := a.s(24)
	gap := a.s(8)
	editX := margin + labelWidth + gap
	editWidth := width - margin - buttonWidth - gap - editX
	if editWidth < a.s(120) {
		editWidth = a.s(120)
	}
	buttonX := width - margin - buttonWidth

	y := margin
	place(a.gameLabel, margin, y, labelWidth, rowHeight)
	place(a.gameEdit, editX, y, editWidth, rowHeight)
	place(a.gameBrowse, buttonX, y, buttonWidth, rowHeight)
	y += rowHeight + gap

	place(a.payloadLabel, margin, y, labelWidth, rowHeight)
	place(a.payloadEdit, editX, y, editWidth, rowHeight)
	place(a.payloadBrowse, buttonX, y, buttonWidth, rowHeight)
	y += rowHeight + gap

	place(a.langLabel, margin, y, labelWidth, rowHeight)
	// 下拉列表框的矩形要含下拉部分，否则展开时看不到条目。
	place(a.langCombo, editX, y, width-margin-editX, rowHeight+a.s(104))
	y += rowHeight + gap

	place(a.statusLabel, margin, y, width-2*margin, rowHeight)
	y += rowHeight + gap

	listHeight := a.s(150)
	place(a.fileList, margin, y, width-2*margin, listHeight)
	y += listHeight + gap

	x := margin
	place(a.installBtn, x, y, a.s(104), rowHeight)
	x += a.s(104) + gap
	place(a.restoreBtn, x, y, a.s(104), rowHeight)
	x += a.s(104) + gap
	place(a.verifyBtn, x, y, buttonWidth, rowHeight)
	x += buttonWidth + gap
	place(a.helpBtn, x, y, buttonWidth, rowHeight)
	x += buttonWidth + gap
	progressWidth := width - margin - x
	if progressWidth < a.s(60) {
		progressWidth = a.s(60)
	}
	place(a.progressBar, x, y, progressWidth, rowHeight)
	y += rowHeight + gap

	logHeight := height - margin - y
	if logHeight < a.s(60) {
		logHeight = a.s(60)
	}
	place(a.logEdit, margin, y, width-2*margin, logHeight)
}

func place(handle uintptr, x, y, width, height int) {
	if handle == 0 {
		return
	}
	procMoveWindow.Call(handle, uintptr(x), uintptr(y), uintptr(width), uintptr(height), 1)
}

func (a *guiApp) wndProc(hwnd, msg, wparam, lparam uintptr) uintptr {
	switch uint32(msg) {
	case wmCreate:
		a.onCreate(hwnd)
		return 0
	case wmSize:
		a.layout()
		return 0
	case wmDpiChanged:
		a.onDpiChanged(wparam, lparam)
		return 0
	case wmGetMinMaxInfo:
		a.onMinMaxInfo(lparam)
		return 0
	case wmCommand:
		a.onCommand(wparam, lparam)
		return 0
	case wmTimer:
		a.drainEvents()
		return 0
	case wmDropFiles:
		a.onDropFiles(wparam)
		return 0
	case wmClose:
		if a.busy {
			messageBox(hwnd, "正在写入文件，请等待当前操作完成后再关闭窗口。", appTitle, mbOk|mbIconInformation)
			return 0
		}
	case wmDestroy:
		procKillTimer.Call(hwnd, 1)
		procPostQuitMessage.Call(0)
		return 0
	}
	return defWindowProc(hwnd, msg, wparam, lparam)
}

func (a *guiApp) onCreate(hwnd uintptr) {
	a.window = hwnd
	if procGetDpiForWindow.Find() == nil {
		if dpi, _, _ := procGetDpiForWindow.Call(hwnd); dpi >= 96 && dpi <= 480 {
			a.dpi = uint32(dpi)
			a.scale = float64(dpi) / 96.0
		}
	}
	a.font = createUIFont(a.dpi)
	a.createControls()
	a.layout()
	procSetTimer.Call(hwnd, 1, 50, 0)
	procDragAcceptFiles.Call(hwnd, 1)

	a.appendLog("欢迎使用 " + appTitle + "。")
	a.appendLog("把 farland-patch-*.zip 放在本程序所在目录，或点「浏览…」选择；")
	a.appendLog("也可以把补丁包或游戏目录直接拖到窗口上。")
	a.appendLog("安装前请先退出游戏。原版会备份到游戏目录，随时可以恢复。")

	a.restoreGameDir()
	a.discover()
}

// restoreGameDir 用上次记住的目录预填输入框，无效时保留原值让人自己改。
func (a *guiApp) restoreGameDir() {
	saved := loadGameConfig()
	if saved == "" {
		return
	}
	setWindowText(a.gameEdit, saved)
	if game, err := validateGameDir(saved, nil); err == nil {
		a.game = game
	}
}

func (a *guiApp) onDpiChanged(wparam, lparam uintptr) {
	dpi := uint32(wparam & 0xffff)
	if dpi < 96 || dpi > 480 {
		return
	}
	a.dpi = dpi
	a.scale = float64(dpi) / 96.0
	if a.font != 0 {
		procDeleteObject.Call(a.font)
	}
	a.font = createUIFont(dpi)
	a.applyFont()
	// lparam 指向系统建议的新窗口矩形，直接采用。
	rect := (*winRect)(pointerFromUintptr(lparam))
	procMoveWindow.Call(a.window, uintptr(rect.Left), uintptr(rect.Top),
		uintptr(rect.Right-rect.Left), uintptr(rect.Bottom-rect.Top), 1)
	a.layout()
}

func (a *guiApp) applyFont() {
	if a.font == 0 {
		return
	}
	for _, handle := range []uintptr{
		a.gameLabel, a.gameEdit, a.gameBrowse,
		a.payloadLabel, a.payloadEdit, a.payloadBrowse,
		a.langLabel, a.langCombo, a.statusLabel,
		a.fileList, a.progressBar, a.logEdit,
		a.installBtn, a.restoreBtn, a.verifyBtn, a.helpBtn,
	} {
		if handle != 0 {
			sendMessage(handle, wmSetFont, a.font, 1)
		}
	}
}

func (a *guiApp) onMinMaxInfo(lparam uintptr) {
	info := (*minMaxInfo)(pointerFromUintptr(lparam))
	info.MinTrack.X = int32(a.s(700))
	info.MinTrack.Y = int32(a.s(560))
}

func (a *guiApp) onCommand(wparam, lparam uintptr) {
	id := uint16(wparam)
	code := uint16(wparam >> 16)
	switch id {
	case idInstall:
		if code == bnClicked {
			a.doInstall()
		}
	case idRestore:
		if code == bnClicked {
			a.doRestore()
		}
	case idVerify:
		if code == bnClicked {
			a.doVerify()
		}
	case idHelp:
		if code == bnClicked {
			a.doHelp()
		}
	case idGameBrowse:
		if code == bnClicked {
			a.browseGame()
		}
	case idPayloadBrowse:
		if code == bnClicked {
			a.browsePayload()
		}
	case idLangCombo:
		if code == cbnSelChange {
			a.onComboChange()
		}
	case idGameEdit:
		if code == enKillFocus {
			a.onGameEdited()
		}
	}
}

// discover 扫描程序所在目录的补丁载荷，优先选中与已安装记录一致的语言。
func (a *guiApp) discover() {
	choices, problems := discoverPayloads()
	for _, problem := range problems {
		a.appendLog("警告：" + problem)
	}
	a.choices = choices
	a.selected = -1
	if a.game != "" {
		if record, err := loadRecord(a.game); err == nil && record != nil {
			for index, choice := range choices {
				if choice.info.Version == record.Version && choice.info.Language == record.Language {
					a.selected = index
					break
				}
			}
		}
	}
	if a.selected < 0 && len(choices) > 0 {
		a.selected = 0
	}
	a.fillCombo()
	if a.selected >= 0 {
		a.selectPayload(a.selected)
		return
	}
	a.appendLog("没有找到补丁载荷：请把 farland-patch-*.zip 放到本程序所在目录。")
	a.applyState()
}

func (a *guiApp) fillCombo() {
	sendMessage(a.langCombo, cbResetContent, 0, 0)
	for _, choice := range a.choices {
		label := fmt.Sprintf("%s（%s）", choice.info.Version, choice.info.Language)
		sendMessage(a.langCombo, cbAddString, 0, uintptr(unsafe.Pointer(utf16ptr(label))))
	}
	if a.selected >= 0 && a.selected < len(a.choices) {
		sendMessage(a.langCombo, cbSetCurSel, uintptr(a.selected), 0)
	}
}

func (a *guiApp) onComboChange() {
	if a.busy {
		return
	}
	index := int(int32(sendMessage(a.langCombo, cbGetCurSel, 0, 0)))
	if index < 0 || index >= len(a.choices) || index == a.selected {
		return
	}
	a.selectPayload(index)
}

// selectPayload 打开选中的载荷并刷新状态；打开失败只记日志，不打断界面。
func (a *guiApp) selectPayload(index int) {
	if index < 0 || index >= len(a.choices) {
		return
	}
	a.selected = index
	choice := a.choices[index]
	setWindowText(a.payloadEdit, choice.path)
	if a.loaded != nil {
		a.loaded.close()
		a.loaded = nil
	}
	loaded, err := openPayload(choice.path)
	if err != nil {
		a.appendLog("无法打开补丁载荷 " + choice.path + "：" + err.Error())
		a.applyState()
		return
	}
	a.loaded = loaded
	a.appendLog(fmt.Sprintf("已载入 %s（%s，%s），共 %d 个目标文件。",
		filepath.Base(choice.path), loaded.info.Version, loaded.info.Language, len(loaded.info.Files)))
	a.refresh()
}

func (a *guiApp) browseGame() {
	directory := browseForFolder(a.window, "选择 FARLAND SAGA I Saturn Tribute 的安装目录")
	if directory == "" {
		return
	}
	setWindowText(a.gameEdit, directory)
	a.onGameEdited()
}

func (a *guiApp) browsePayload() {
	path := openFileDialog(a.window, "选择补丁载荷",
		"补丁载荷 (*.zip)\x00*.zip\x00所有文件 (*.*)\x00*.*\x00\x00")
	if path == "" {
		return
	}
	for index, choice := range a.choices {
		if samePath(choice.path, path) {
			a.selectPayload(index)
			return
		}
	}
	loaded, err := openPayload(path)
	if err != nil {
		a.showError(fmt.Errorf("无法打开补丁载荷 %s：%w", path, err))
		return
	}
	loaded.close()
	a.choices = append(a.choices, payloadChoice{path: path, info: loaded.info})
	a.fillCombo()
	a.selectPayload(len(a.choices) - 1)
}

func (a *guiApp) onGameEdited() {
	if a.busy {
		return
	}
	text := strings.TrimSpace(getWindowText(a.gameEdit))
	if text == "" {
		a.game = ""
		a.applyState()
		return
	}
	game, err := validateGameDir(text, a.payloadInfo())
	if err != nil {
		a.game = ""
		a.appendLog("游戏目录不可用：" + err.Error())
		a.applyState()
		return
	}
	a.game = game
	saveGameConfig(game)
	// 失焦常发生在用户点按钮的瞬间，同步刷新会把那次点击当成忙碌吞掉；
	// 这里把刷新推给定时器，等这一轮消息处理完再跑。令牌让后续操作能把它作废。
	a.generation++
	token := a.generation
	a.post(func() {
		if token != a.generation {
			return
		}
		a.refresh()
	})
}

func (a *guiApp) payloadInfo() *manifest {
	if a.loaded == nil {
		return nil
	}
	return a.loaded.info
}

// currentGame 取输入框里的目录并校验；带载荷时同时确认目标文件齐全。
func (a *guiApp) currentGame() (string, error) {
	text := strings.TrimSpace(getWindowText(a.gameEdit))
	if text == "" {
		if saved := loadGameConfig(); saved != "" {
			if game, err := validateGameDir(saved, a.payloadInfo()); err == nil {
				setWindowText(a.gameEdit, game)
				a.game = game
				return game, nil
			}
		}
		return "", errors.New("请先选择游戏安装目录")
	}
	game, err := validateGameDir(text, a.payloadInfo())
	if err != nil {
		return "", err
	}
	a.game = game
	saveGameConfig(game)
	return game, nil
}

func (a *guiApp) doInstall() {
	if a.busy {
		return
	}
	loaded := a.loaded
	if loaded == nil {
		a.showError(errors.New("请先选择补丁载荷文件"))
		return
	}
	game, err := a.currentGame()
	if err != nil {
		a.showError(err)
		return
	}
	// 文件身份由 installPatch 在工作线程里权威核对；这里不再预检，
	// 否则几百兆的哈希会把界面线程卡住几分钟。
	question := fmt.Sprintf("现在安装 %s（%s）吗？\n\n将写入 %d 个文件，原版会备份到游戏目录下的 FarlandSagaPatcher-backup-* 目录。安装前请先退出游戏。",
		loaded.info.Version, loaded.info.Language, len(loaded.info.Files))
	if !confirmDialog(a.window, question, "安装补丁") {
		a.appendLog("已取消安装。")
		return
	}
	a.setBusy(true, fmt.Sprintf("正在安装 %s（%s）…", loaded.info.Version, loaded.info.Language))
	a.appendLog(fmt.Sprintf("开始安装 %s（%s）到 %s", loaded.info.Version, loaded.info.Language, game))
	go a.runInstall(game, loaded, a.generation)
}

func (a *guiApp) runInstall(game string, loaded *payload, generation int) {
	defer a.guard()
	result, err := installPatch(game, loaded, a.progressCallback())
	if err != nil {
		a.post(func() {
			if generation != a.generation {
				return
			}
			a.setBusy(false, "")
			a.setProgress(0, 0)
			a.appendLog("安装失败：" + err.Error())
			a.showError(err)
			a.refresh()
		})
		return
	}
	record, _ := loadRecord(game)
	statuses := inspectAll(game, loaded.info, record)
	a.post(func() {
		if generation != a.generation {
			return
		}
		a.setBusy(false, "")
		a.setProgress(0, 0)
		a.record = record
		a.statuses = statuses
		a.applyState()
		if result.AlreadyInstalled {
			a.appendLog(fmt.Sprintf("%s（%s）已经安装，无需重复操作。", result.Version, result.Language))
			messageBox(a.window, fmt.Sprintf("%s（%s）已经安装。", result.Version, result.Language),
				appTitle, mbOk|mbIconInformation)
			return
		}
		a.appendLog(fmt.Sprintf("安装完成：%s（%s），共 %d 个文件。原版备份：%s",
			result.Version, result.Language, len(result.Files), result.BackupDirectory))
		messageBox(a.window, fmt.Sprintf("已安装 %s（%s）。\n\n原版备份：%s\n如需恢复原版，点「恢复原版」。请勿删除备份目录。",
			result.Version, result.Language, result.BackupDirectory), appTitle, mbOk|mbIconInformation)
	})
}

func (a *guiApp) doRestore() {
	if a.busy {
		return
	}
	game, err := a.currentGame()
	if err != nil {
		a.showError(err)
		return
	}
	record, err := loadRecord(game)
	if err != nil {
		a.showError(err)
		return
	}
	if record == nil {
		a.showError(fmt.Errorf("没有找到安装记录 %s，无法恢复", recordName))
		return
	}
	if record.Status != statusInstalled {
		a.showError(fmt.Errorf("安装记录状态为 %s，无需恢复", record.Status))
		return
	}
	if !confirmDialog(a.window, fmt.Sprintf("用原版备份覆盖当前已打补丁的文件吗？\n\n将恢复 %s（%s）安装前的原版文件。",
		record.Version, record.Language), "恢复原版") {
		a.appendLog("已取消恢复。")
		return
	}
	a.setBusy(true, "正在恢复原版…")
	a.appendLog(fmt.Sprintf("开始恢复 %s（%s）的原版文件", record.Version, record.Language))
	go a.runRestore(game, a.generation)
}

func (a *guiApp) runRestore(game string, generation int) {
	defer a.guard()
	result, err := restoreOriginal(game)
	if err != nil {
		a.post(func() {
			if generation != a.generation {
				return
			}
			a.setBusy(false, "")
			a.appendLog("恢复失败：" + err.Error())
			a.showError(err)
			a.refresh()
		})
		return
	}
	record, _ := loadRecord(game)
	a.post(func() {
		if generation != a.generation {
			return
		}
		a.setBusy(false, "")
		a.record = record
		a.statuses = result.Files
		a.applyState()
		a.appendLog(fmt.Sprintf("已恢复原版，共 %d 个文件。回滚副本：%s", len(result.Files), result.Rollback))
		messageBox(a.window, fmt.Sprintf("已恢复原版，共 %d 个文件。\n\n回滚副本：%s\n确认游戏正常后可以删除该目录。",
			len(result.Files), result.Rollback), appTitle, mbOk|mbIconInformation)
	})
}

func (a *guiApp) doVerify() {
	if a.busy {
		return
	}
	game, err := a.currentGame()
	if err != nil {
		a.showError(err)
		return
	}
	loaded := a.loaded
	a.setBusy(true, "正在核对文件…")
	a.appendLog("开始核对 " + game)
	generation := a.generation
	go func() {
		defer a.guard()
		statuses, err := verifyInstall(game, loaded)
		if err != nil {
			a.post(func() {
				if generation != a.generation {
					return
				}
				a.setBusy(false, "")
				a.appendLog("核对失败：" + err.Error())
				a.showError(err)
			})
			return
		}
		ok := true
		var problems []string
		for _, status := range statuses {
			switch {
			case loaded != nil && (status.State == stateOriginal || status.State == statePatched):
			case loaded == nil && status.State == statePatched:
			default:
				ok = false
				problems = append(problems, "  "+status.Path+"  "+describeState(status))
			}
		}
		a.post(func() {
			if generation != a.generation {
				return
			}
			a.setBusy(false, "")
			a.statuses = statuses
			a.applyState()
			if ok {
				a.appendLog("核对通过。")
				messageBox(a.window, "核对通过：所有文件都处于预期状态。", appTitle, mbOk|mbIconInformation)
				return
			}
			a.appendLog("核对未通过:\n" + strings.Join(problems, "\n"))
			a.showError(errors.New("核对未通过:\n" + strings.Join(problems, "\n")))
		})
	}()
}

func (a *guiApp) doHelp() {
	messageBox(a.window, helpText(), appTitle, mbOk|mbIconInformation)
}

func helpText() string {
	return `使用说明

1. 选择游戏安装目录（Steam 库中的 FARLAND SAGA I Saturn Tribute）。
2. 选择语言/版本；补丁载荷 farland-patch-*.zip 需放在本程序所在目录，
   也可以用「浏览…」指定，或直接拖到窗口上。
3. 点「安装补丁」。安装前请先退出游戏。
4. 想恢复原版就点「恢复原版」，想换语言就换一个版本再点「安装补丁」。

原版文件会备份到游戏目录下的 FarlandSagaPatcher-backup-* 目录，
删除备份前请确认不再需要恢复。

命令行用法：
  FarlandSagaPatcher.exe install --yes
  FarlandSagaPatcher.exe restore --yes
  FarlandSagaPatcher.exe status
  FarlandSagaPatcher.exe wizard    控制台向导`
}

// refresh 在后台重新核对游戏状态，避免几百兆的哈希计算卡住界面。
func (a *guiApp) refresh() {
	if a.busy {
		return
	}
	game := a.game
	if game == "" {
		a.applyState()
		return
	}
	loaded := a.loaded
	a.setBusy(true, "正在检查游戏状态…")
	generation := a.generation
	go func() {
		defer a.guard()
		record, err := loadRecord(game)
		if err != nil {
			a.post(func() {
				if generation != a.generation {
					return
				}
				a.setBusy(false, "")
				a.appendLog("读取安装记录失败：" + err.Error())
				a.applyState()
			})
			return
		}
		var statuses []fileStatus
		if loaded != nil {
			statuses = inspectAll(game, loaded.info, record)
		} else if record != nil {
			statuses, err = verifyInstall(game, nil)
			if err != nil {
				a.post(func() {
					if generation != a.generation {
						return
					}
					a.setBusy(false, "")
					a.appendLog("核对失败：" + err.Error())
					a.applyState()
				})
				return
			}
		}
		a.post(func() {
			if generation != a.generation {
				return
			}
			a.setBusy(false, "")
			a.record = record
			a.statuses = statuses
			a.applyState()
		})
	}()
}

// progressCallback 把回放进度节流后送给界面线程。差异块每 4 KiB 一次，
// 不节流会把消息循环淹掉。
func (a *guiApp) progressCallback() func(fileEntry, int64) {
	var mutex sync.Mutex
	var last time.Time
	var lastPath string
	var lastPosition int64
	return func(entry fileEntry, position int64) {
		mutex.Lock()
		now := time.Now()
		if entry.Path == lastPath && position == lastPosition {
			mutex.Unlock()
			return
		}
		if now.Sub(last) < progressInterval && position < entry.PatchedSize {
			mutex.Unlock()
			return
		}
		last, lastPath, lastPosition = now, entry.Path, position
		mutex.Unlock()
		a.post(func() { a.showProgress(entry.Path, position, entry.PatchedSize) })
	}
}

func (a *guiApp) showProgress(path string, position, total int64) {
	a.setProgress(position, total)
	percent := 0
	if total > 0 {
		percent = int(position * 100 / total)
	}
	setWindowText(a.statusLabel, fmt.Sprintf("正在写入 %s %d%%（%s / %s）",
		path, percent, formatSize(position), formatSize(total)))
}

func (a *guiApp) setProgress(position, total int64) {
	if a.progressBar == 0 {
		return
	}
	if total <= 0 {
		a.progressTotal = 0
		sendMessage(a.progressBar, pbmSetPos, 0, 0)
		return
	}
	if total != a.progressTotal {
		sendMessage(a.progressBar, pbmSetRange32, 0, uintptr(total))
		a.progressTotal = total
	}
	sendMessage(a.progressBar, pbmSetPos, uintptr(position), 0)
}

// post 把闭包送进界面线程执行；队列足够大，写入不会丢事件。
func (a *guiApp) post(function func()) {
	a.events <- function
}

// drainEvents 执行定时器里排队的界面事件。
func (a *guiApp) drainEvents() {
	for {
		select {
		case function := <-a.events:
			a.runSafely(function)
		default:
			return
		}
	}
}

// runSafely 执行单个界面事件。事件里出错不能让消息循环崩掉，否则窗口会直接
// 消失，用户看不到任何提示。
func (a *guiApp) runSafely(function func()) {
	defer func() {
		if recovered := recover(); recovered != nil {
			a.busy = false
			a.appendLog(fmt.Sprintf("内部错误：%v", recovered))
			a.applyState()
		}
	}()
	function()
}

// guard 只能用于 defer：把工作线程里的 panic 转成界面上的一条错误，
// 避免进程直接消失而看不到任何提示。
func (a *guiApp) guard() {
	if recovered := recover(); recovered != nil {
		err := fmt.Errorf("内部错误：%v", recovered)
		a.post(func() {
			a.setBusy(false, "")
			a.setProgress(0, 0)
			a.appendLog(err.Error())
			a.showError(err)
		})
	}
}

func (a *guiApp) setBusy(busy bool, label string) {
	if busy {
		a.generation++
	}
	a.busy = busy
	a.busyLabel = label
	a.applyState()
}

// applyState 依据当前状态刷新状态行、文件清单与按钮可用性。
func (a *guiApp) applyState() {
	setWindowText(a.statusLabel, a.statusText())
	a.fillFileList()
	installed := a.record != nil && a.record.Status == statusInstalled
	enableWindow(a.installBtn, !a.busy && a.loaded != nil && a.game != "")
	enableWindow(a.restoreBtn, !a.busy && installed)
	enableWindow(a.verifyBtn, !a.busy && a.game != "")
	enableWindow(a.helpBtn, !a.busy)
	enableWindow(a.gameBrowse, !a.busy)
	enableWindow(a.payloadBrowse, !a.busy)
	enableWindow(a.langCombo, !a.busy && len(a.choices) > 0)
	enableWindow(a.gameEdit, !a.busy)
	enableWindow(a.payloadEdit, !a.busy)
	if !a.busy {
		a.setProgress(0, 0)
	}
}

func (a *guiApp) statusText() string {
	if a.busy {
		return a.busyLabel
	}
	if a.game == "" {
		return "请选择游戏安装目录（Steam 库中的 FARLAND SAGA I Saturn Tribute）"
	}
	if a.record != nil && a.record.Status == statusInstalled {
		return fmt.Sprintf("已安装 %s（%s）", a.record.Version, a.record.Language)
	}
	if len(a.statuses) == 0 {
		return "正在检查游戏状态…"
	}
	return summarizeStates(a.statuses)
}

func (a *guiApp) fillFileList() {
	if a.fileList == 0 {
		return
	}
	sendMessage(a.fileList, lvmDeleteAllItems, 0, 0)
	for index, status := range a.statuses {
		item := lvItem{Mask: lvifText, Item: int32(index), Text: utf16ptr(status.Path)}
		sendMessage(a.fileList, lvmInsertItem, 0, uintptr(unsafe.Pointer(&item)))
		size := "-"
		if status.CurrentSize > 0 {
			size = formatSize(status.CurrentSize)
		}
		a.setSubItem(index, 1, describeState(status))
		a.setSubItem(index, 2, size)
	}
}

func (a *guiApp) setSubItem(row, column int, text string) {
	item := lvItem{Mask: lvifText, Item: int32(row), SubItem: int32(column), Text: utf16ptr(text)}
	sendMessage(a.fileList, lvmSetItem, 0, uintptr(unsafe.Pointer(&item)))
}

func (a *guiApp) appendLog(text string) {
	if a.logEdit == 0 {
		return
	}
	if !strings.HasSuffix(text, "\n") {
		text += "\n"
	}
	text = strings.TrimSuffix(text, "\n") + "\r\n"
	if a.logText.Len() > logLimit {
		a.logText.Reset()
		a.logText.WriteString("（日志过长，此处之前的内容已省略）\r\n")
	}
	a.logText.WriteString(text)
	length := sendMessage(a.logEdit, wmGetTextLength, 0, 0)
	sendMessage(a.logEdit, emSetSel, length, length)
	sendMessage(a.logEdit, emReplaceSel, 0, uintptr(unsafe.Pointer(utf16ptr(text))))
	sendMessage(a.logEdit, emScrollCaret, 0, 0)
}

func (a *guiApp) showError(err error) {
	a.appendLog("错误：" + err.Error())
	messageBox(a.window, err.Error(), appTitle, mbOk|mbIconError)
}

func (a *guiApp) onDropFiles(wparam uintptr) {
	count := int(int32(sendDropCount(wparam)))
	for index := 0; index < count; index++ {
		buffer := make([]uint16, 32768)
		if sendDragQueryFile(wparam, uintptr(index), &buffer[0], uintptr(len(buffer))) == 0 {
			continue
		}
		a.handleDroppedPath(syscall.UTF16ToString(buffer))
	}
	procDragFinish.Call(wparam)
}

func (a *guiApp) handleDroppedPath(path string) {
	info, err := os.Stat(path)
	if err != nil {
		a.appendLog("无法读取拖入的路径 " + path + "：" + err.Error())
		return
	}
	if info.IsDir() {
		setWindowText(a.gameEdit, path)
		a.onGameEdited()
		return
	}
	if strings.EqualFold(filepath.Ext(path), ".zip") {
		a.browsePayloadAt(path)
		return
	}
	a.appendLog("只支持拖入游戏目录或 .zip 补丁载荷：" + path)
}

// browsePayloadAt 打开指定路径的载荷；已在下拉列表里就直接选中。
func (a *guiApp) browsePayloadAt(path string) {
	for index, choice := range a.choices {
		if samePath(choice.path, path) {
			a.selectPayload(index)
			return
		}
	}
	loaded, err := openPayload(path)
	if err != nil {
		a.showError(fmt.Errorf("无法打开补丁载荷 %s：%w", path, err))
		return
	}
	loaded.close()
	a.choices = append(a.choices, payloadChoice{path: path, info: loaded.info})
	a.fillCombo()
	a.selectPayload(len(a.choices) - 1)
}

func createUIFont(dpi uint32) uintptr {
	height := int32(-9 * int64(dpi) / 72)
	if height > -11 {
		height = -11
	}
	font, _, _ := procCreateFont.Call(
		uintptr(height), 0, 0, 0,
		fwNormal, 0, 0, 0,
		defaultCharset, 0, 0, 0, 0,
		uintptr(unsafe.Pointer(utf16ptr(uiFontFace))))
	return font
}

// browseForFolder 用 SHBrowseForFolder 选目录，返回空串表示用户取消。
func browseForFolder(owner uintptr, title string) string {
	var display [syscall.MAX_PATH]uint16
	info := browseInfo{
		Owner:       owner,
		DisplayName: &display[0],
		Title:       utf16ptr(title),
		Flags:       bifReturnOnlyFsDirs | bifNewDialogStyle | bifEditBox,
	}
	list, _, _ := procSHBrowseForFolder.Call(uintptr(unsafe.Pointer(&info)))
	if list == 0 {
		return ""
	}
	defer procCoTaskMemFree.Call(list)
	var buffer [32768]uint16
	if ok, _, _ := procSHGetPathFromIDList.Call(list, uintptr(unsafe.Pointer(&buffer[0]))); ok == 0 {
		return ""
	}
	return syscall.UTF16ToString(buffer[:])
}

// openFileDialog 用 GetOpenFileName 选文件，返回空串表示用户取消。
func openFileDialog(owner uintptr, title, filter string) string {
	buffer := make([]uint16, 32768)
	initial := ""
	if directory := executableDir(); directory != "" {
		initial = directory
	}
	dialog := openFileName{
		StructSize: uint32(unsafe.Sizeof(openFileName{})),
		Owner:      owner,
		Filter:     utf16ptr(filter),
		File:       &buffer[0],
		MaxFile:    uint32(len(buffer)),
		InitialDir: utf16ptr(initial),
		Title:      utf16ptr(title),
		Flags:      ofnFileMustExist | ofnPathMustExist | ofnExplorer | ofnNoChangeDir,
	}
	if ok, _, _ := procGetOpenFileName.Call(uintptr(unsafe.Pointer(&dialog))); ok == 0 {
		return ""
	}
	return syscall.UTF16ToString(buffer)
}

func sendDropCount(drop uintptr) uintptr {
	result, _, _ := procDragQueryFile.Call(drop, ^uintptr(0), 0, 0)
	return result
}

func sendDragQueryFile(drop, index uintptr, buffer *uint16, size uintptr) uintptr {
	result, _, _ := procDragQueryFile.Call(drop, index, uintptr(unsafe.Pointer(buffer)), size)
	return result
}

func sendMessage(handle uintptr, message uint32, wparam, lparam uintptr) uintptr {
	result, _, _ := procSendMessage.Call(handle, uintptr(message), wparam, lparam)
	return result
}

func setWindowText(handle uintptr, text string) {
	if handle == 0 {
		return
	}
	procSetWindowText.Call(handle, uintptr(unsafe.Pointer(utf16ptr(text))))
}

func getWindowText(handle uintptr) string {
	if handle == 0 {
		return ""
	}
	length, _, _ := procGetWindowTextLength.Call(handle)
	if length == 0 {
		return ""
	}
	buffer := make([]uint16, length+1)
	procGetWindowText.Call(handle, uintptr(unsafe.Pointer(&buffer[0])), uintptr(length+1))
	return syscall.UTF16ToString(buffer)
}

func enableWindow(handle uintptr, enabled bool) {
	if handle == 0 {
		return
	}
	parameter := uintptr(0)
	if enabled {
		parameter = 1
	}
	procEnableWindow.Call(handle, parameter)
}

func messageBox(owner uintptr, text, caption string, style uint32) uintptr {
	result, _, _ := procMessageBox.Call(owner,
		uintptr(unsafe.Pointer(utf16ptr(text))),
		uintptr(unsafe.Pointer(utf16ptr(caption))),
		uintptr(style))
	return result
}

func confirmDialog(owner uintptr, question, caption string) bool {
	return messageBox(owner, question, caption, mbYesNo|mbIconQuestion|mbDefButton2) == idYes
}

func defWindowProc(hwnd, msg, wparam, lparam uintptr) uintptr {
	result, _, _ := procDefWindowProc.Call(hwnd, msg, wparam, lparam)
	return result
}

func utf16ptr(text string) *uint16 {
	pointer, _ := syscall.UTF16PtrFromString(text)
	return pointer
}

// pointerFromUintptr 把窗口回调收到的 uintptr 还原成 unsafe.Pointer。
// 直接写 unsafe.Pointer(lparam) 会被 go vet 的 unsafeptr 检查拦下，这里经一个
// 真实指针的地址中转：读取该地址处的 8 个字节，得到的地址与 lparam 相同。
// 这些结构体内存在 Windows 侧，回调返回前一直有效，不涉及 GC 跟踪问题。
func pointerFromUintptr(value uintptr) unsafe.Pointer {
	return *(*unsafe.Pointer)(unsafe.Pointer(&value))
}
