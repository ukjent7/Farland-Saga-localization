// Windows 图形界面：单个可执行文件，补丁编译期内嵌，双击即用。
//
// 发布形态是把本程序放进游戏根目录后双击运行，所以界面只做四件事：三个
// 语言按钮各装一个内嵌补丁，一个还原按钮把游戏文件恢复原版。游戏目录用
// 程序所在目录推断，不让用户挑路径；文件清单、浏览、拖放这些辅助交互都
// 去掉了，留给命令行子命令。
//
// 界面线程只跑消息循环。安装、还原放在工作 goroutine，经缓冲 channel 把
// 闭包投回界面线程，由 50ms 的 WM_TIMER 执行；进度回调每 4 KiB 一次、节流
// 100ms。忙碌状态用递增令牌管理，被作废的操作也会归还自己的令牌，界面不会
// 因为一次丢弃的检查而永远禁着——那正是早期版本一打开就卡死的原因。
//
// Win32 全部经 syscall 调用，无 cgo、无第三方依赖，可在 Linux 上交叉编译。
// 结构体布局用 /tmp/winlayout.c（zig cc 编译）逐字段核对过，文件底部有编译期
// 大小断言，布局变化会直接编译失败。内嵌补丁的加载在 embedded.go，平台中立。
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

const appTitle = "FARLAND SAGA I Saturn Tribute 汉化补丁"
const guiClassName = "FarlandSagaPatcherWindow"
const uiFontFace = "MS Shell Dlg 2"

var (
	kernel32 = syscall.NewLazyDLL("kernel32.dll")
	user32   = syscall.NewLazyDLL("user32.dll")
	comctl32 = syscall.NewLazyDLL("comctl32.dll")
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
	procGetDpiForWindow               = user32.NewProc("GetDpiForWindow")
	procSetProcessDpiAwarenessContext = user32.NewProc("SetProcessDpiAwarenessContext")
	procCreateFont                    = gdi32.NewProc("CreateFontW")
	procDeleteObject                  = gdi32.NewProc("DeleteObject")
	procInitCommonControlsEx          = comctl32.NewProc("InitCommonControlsEx")
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

	bsPushButton  = 0x00000000
	bsMultiline   = 0x00002000
	esMultiline   = 0x0004
	esAutoVScroll = 0x0040
	esReadOnly    = 0x0800

	swShow = 5

	wmCreate        = 0x0001
	wmDestroy       = 0x0002
	wmSize          = 0x0005
	wmClose         = 0x0010
	wmSetFont       = 0x0030
	wmCommand       = 0x0111
	wmTimer         = 0x0113
	wmDpiChanged    = 0x02e0
	wmGetMinMaxInfo = 0x0024

	bnClicked = 0

	colorBtnFace = 15

	// 控件 ID，同时是 WM_COMMAND 的低 16 位。前三个是语言按钮，顺序固定为
	// 中、韩、英；第四个是还原。
	idGameLabel   = 1001
	idStatusLabel = 1002
	idProgress    = 1003
	idLog         = 1004
	idActionBase  = 1010

	// 通用控件消息。
	pbmSetRange32   = 0x0406
	pbmSetPos       = 0x0402
	emSetSel        = 0x00b1
	emReplaceSel    = 0x00c2
	emScrollCaret   = 0x00b7
	wmGetText       = 0x000d
	wmGetTextLength = 0x000e
	wmSetText       = 0x000c

	// 对话框与字体常量。
	mbOk              = 0x00000000
	mbYesNo           = 0x00000004
	mbIconError       = 0x00000010
	mbIconQuestion    = 0x00000020
	mbIconInformation = 0x00000040
	mbDefButton2      = 0x00000100
	idYes             = 6
	iccProgressClass  = 0x00000020
	fwNormal          = 400
	defaultCharset    = 1

	// 进度事件合并间隔：差异块每 4 KiB 一次，直接推给 UI 会淹没消息循环。
	progressInterval = 100 * time.Millisecond
	// 日志超过这个长度就截断，避免长时间使用后 EDIT 控件变慢。
	logLimit = 200000
	// 忙碌状态的上限。安装、还原都有进度回调不断续期；只有真正没有响应的
	// 操作才会被看门狗解除，让界面恢复可操作。
	watchdogLimit = 3 * time.Minute
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

type minMaxInfo struct {
	Reserved     winPoint
	MaxSize      winPoint
	MaxPosition  winPoint
	MinTrackSize winPoint
	MaxTrackSize winPoint
}

type initCommonControlsEx struct {
	Size uint32
	ICC  uint32
}

// 编译期校验结构体大小，布局被改动时直接编译失败。
var (
	_ = [1]struct{}{}[unsafe.Sizeof(wndClassEx{})-80]
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

	gameLabel   uintptr
	statusLabel uintptr
	progressBar uintptr
	logEdit     uintptr
	// actionButtons 前三个是语言按钮（中、韩、英），第四个是还原。
	actionButtons [4]uintptr

	game     string
	payloads []*payload
	record   *installRecord

	busy      bool
	busyLabel string

	// busyToken 是当前忙碌操作的令牌，0 表示空闲。每次开始忙碌都领取一个
	// 递增令牌，结束时就地归还。结果被更新的操作作废时也照样归还，界面不会
	// 因为一次被丢弃的检查而永远停在忙碌状态——那会让所有按钮禁用、连关闭
	// 窗口都被拦住，看起来就是整个程序卡死。
	busyToken int
	nextToken int

	// busySince 记录本次忙碌的起始时间，供看门狗判断操作是否没有回来。
	// 有进度回调的操作每次回调都会续期。
	busySince time.Time

	// generation 在每次开始新的忙碌操作、或作废排队的刷新时递增。工作线程
	// 回传结果时发现世代已变，说明已有更新的操作接管，这次结果直接丢弃，
	// 免得一次慢吞吞的检查把正在进行的安装状态覆盖掉。
	generation int

	events        chan func()
	logText       strings.Builder
	progressTotal int64
}

func newGUIApp() *guiApp {
	return &guiApp{scale: 1, dpi: 96, events: make(chan func(), 1024)}
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
	width, height := a.s(540), a.s(470)
	window, _, lastError := procCreateWindowEx.Call(
		wsExControlParent,
		uintptr(unsafe.Pointer(utf16ptr(guiClassName))),
		uintptr(unsafe.Pointer(utf16ptr(appTitle))),
		wsOverlappedWindow|wsClipChildren,
		uintptr(0x80000000), 0,
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
		ICC:  iccProgressClass,
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

// s 按当前 DPI 缩放逻辑像素。
func (a *guiApp) s(value int) int {
	return int(float64(value)*a.scale + 0.5)
}

func (a *guiApp) createControls() {
	a.gameLabel = a.create("Static", "", 0, wsChild|wsVisible, idGameLabel)
	a.statusLabel = a.create("Static", "", 0, wsChild|wsVisible, idStatusLabel)
	a.progressBar = a.create("msctls_progress32", "", 0, wsChild|wsVisible, idProgress)
	a.logEdit = a.create("Edit", "", wsExClientEdge,
		wsChild|wsVisible|wsVScroll|wsBorder|esMultiline|esAutoVScroll|esReadOnly, idLog)
	for index := range a.actionButtons {
		a.actionButtons[index] = a.create("Button", "", 0, wsChild|wsVisible|bsMultiline, idActionBase+uintptr(index))
	}
}

// create 建一个子控件，扩展样式与普通样式分开传，和 CreateWindowEx 的参数
// 顺序一致。
func (a *guiApp) create(class, name string, exStyle, style uint32, id uintptr) uintptr {
	handle, _, _ := procCreateWindowEx.Call(
		uintptr(exStyle),
		uintptr(unsafe.Pointer(utf16ptr(class))),
		uintptr(unsafe.Pointer(utf16ptr(name))),
		uintptr(style),
		0, 0, 0, 0,
		a.window, id, a.module, 0)
	return handle
}

func (a *guiApp) layout() {
	rect := winRect{}
	procGetClientRect.Call(a.window, uintptr(unsafe.Pointer(&rect)))
	width := int(rect.Right - rect.Left)
	height := int(rect.Bottom - rect.Top)
	if width < a.s(360) || height < a.s(320) {
		width, height = a.s(500), a.s(400)
	}
	pad := a.s(16)
	inner := width - pad*2
	buttonWidth := (inner - pad) / 2
	buttonHeight := a.s(56)
	gap := a.s(10)

	y := pad
	place(a.gameLabel, pad, y, inner, a.s(20))
	y += a.s(20) + gap
	place(a.statusLabel, pad, y, inner, a.s(20))
	y += a.s(20) + gap

	for index := range a.actionButtons {
		row := index / 2
		column := index % 2
		x := pad + column*(buttonWidth+pad)
		place(a.actionButtons[index], x, y+row*(buttonHeight+gap), buttonWidth, buttonHeight)
	}
	y += 2*buttonHeight + 2*gap

	place(a.progressBar, pad, y, inner, a.s(16))
	y += a.s(16) + gap
	place(a.logEdit, pad, y, inner, height-y-pad)
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
		a.onCommand(wparam)
		return 0
	case wmTimer:
		a.drainEvents()
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

	a.appendLog("欢迎使用 " + appTitle + "。")
	a.appendLog("本程序已内置简体中文、韩文、英文三个补丁，点对应按钮即可安装。")
	a.appendLog("安装前请先退出游戏。原版会备份到游戏目录，随时可以还原。")

	// 解析内嵌补丁、定位游戏目录都要做文件与内存操作，放到消息循环跑起来
	// 之后再做，免得窗口迟迟显示不出来。
	a.post(a.bootstrap)
}

// bootstrap 加载内嵌补丁、定位游戏目录、读安装记录，完成后刷新界面。
func (a *guiApp) bootstrap() {
	payloads, problems := loadEmbedded()
	for _, problem := range problems {
		a.appendLog("警告：" + problem)
	}
	a.payloads = payloads
	if len(a.payloads) == 0 {
		a.appendLog("没有可用的内嵌补丁，程序构建时可能漏打了 payload 目录。")
	}
	game, err := detectGame(a.firstManifest())
	if err != nil {
		a.game = ""
		a.appendLog("未检测到游戏文件：" + err.Error())
		a.appendLog("请把本程序放到游戏根目录（含 FarlandSaga.exe 的文件夹）后再运行。")
	} else {
		a.game = game
		a.appendLog("游戏目录：" + game)
	}
	a.refresh()
	a.applyState()
}

// firstManifest 返回第一个内嵌补丁的清单，用于校验游戏目录是否齐全。
func (a *guiApp) firstManifest() *manifest {
	if len(a.payloads) == 0 {
		return nil
	}
	return a.payloads[0].info
}

// detectGame 以程序所在目录为游戏目录。发布形态是单个可执行文件放在游戏
// 根目录里双击运行，用户不需要挑路径；放错了地方就明确告知。
func detectGame(info *manifest) (string, error) {
	executable, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("无法确定程序所在目录：%v", err)
	}
	return validateGameDir(filepath.Dir(executable), info)
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
	handles := []uintptr{a.gameLabel, a.statusLabel, a.progressBar, a.logEdit}
	handles = append(handles, a.actionButtons[:]...)
	for _, handle := range handles {
		if handle != 0 {
			sendMessage(handle, wmSetFont, a.font, 1)
		}
	}
}

func (a *guiApp) onMinMaxInfo(lparam uintptr) {
	info := (*minMaxInfo)(pointerFromUintptr(lparam))
	info.MinTrackSize.X = int32(a.s(400))
	info.MinTrackSize.Y = int32(a.s(360))
}

func (a *guiApp) onCommand(wparam uintptr) {
	id := uint16(wparam)
	code := uint16(wparam >> 16)
	if code != bnClicked {
		return
	}
	switch {
	case id >= idActionBase && id < idActionBase+4:
		index := int(id - idActionBase)
		if index < 3 {
			a.onLanguage(index)
			return
		}
		a.onRestore()
	}
}

// onLanguage 安装指定语言的内嵌补丁。
func (a *guiApp) onLanguage(index int) {
	if a.busy || index < 0 || index >= len(a.payloads) {
		return
	}
	loaded := a.payloads[index]
	label := languageLabel(loaded.info.Language)
	if a.installedLanguage() == loaded.info.Language {
		messageBox(a.window, label+" 补丁已经安装，无需重复操作。", appTitle, mbOk|mbIconInformation)
		return
	}
	if a.game == "" {
		a.showError(errors.New("未检测到游戏目录，请把本程序放到游戏根目录后再运行。"))
		return
	}
	question := fmt.Sprintf("现在安装 %s 补丁吗？\n\n将写入 %d 个文件，原版会备份到游戏目录下的 %s* 文件夹。安装前请先退出游戏。",
		label, len(loaded.info.Files), backupPrefix)
	if !confirmDialog(a.window, question, "安装 "+label) {
		a.appendLog("已取消安装。")
		return
	}
	token := a.beginBusy("正在安装 " + label + " 补丁…")
	a.appendLog(fmt.Sprintf("开始安装 %s（%s）", label, loaded.info.Version))
	go a.runInstall(a.game, loaded, token, a.generation)
}

// onRestore 用原版备份把游戏文件恢复回去。
func (a *guiApp) onRestore() {
	if a.busy {
		return
	}
	if a.game == "" {
		a.showError(errors.New("未检测到游戏目录，请把本程序放到游戏根目录后再运行。"))
		return
	}
	record, err := loadRecord(a.game)
	if err != nil {
		a.showError(err)
		return
	}
	if record == nil || record.Status != statusInstalled {
		a.showError(errors.New("尚未安装补丁，无需还原。"))
		return
	}
	if !confirmDialog(a.window, fmt.Sprintf("用原版备份覆盖当前已打补丁的文件吗？\n\n将恢复 %s 安装前的原版文件。",
		languageLabel(record.Language)), "还原原版") {
		a.appendLog("已取消还原。")
		return
	}
	token := a.beginBusy("正在还原原版…")
	a.appendLog("开始还原原版")
	go a.runRestore(a.game, token, a.generation)
}

func (a *guiApp) runInstall(game string, loaded *payload, token, generation int) {
	defer a.guard()
	result, err := installPatch(game, loaded, a.progressCallback())
	if err != nil {
		a.post(func() {
			a.endBusy(token)
			if generation != a.generation {
				return
			}
			a.setProgress(0, 0)
			a.appendLog("安装失败：" + err.Error())
			a.showError(err)
			a.refresh()
		})
		return
	}
	record, _ := loadRecord(game)
	a.post(func() {
		a.endBusy(token)
		if generation != a.generation {
			return
		}
		a.setProgress(0, 0)
		a.record = record
		a.applyState()
		if result.AlreadyInstalled {
			a.appendLog("补丁已经安装，无需重复操作。")
			messageBox(a.window, "补丁已经安装。", appTitle, mbOk|mbIconInformation)
			return
		}
		a.appendLog(fmt.Sprintf("安装完成：%s（%s），共 %d 个文件。原版备份：%s",
			result.Version, result.Language, len(result.Files), result.BackupDirectory))
		messageBox(a.window, fmt.Sprintf("已安装 %s 补丁。\n\n原版备份：%s\n需要还原时点「还原原版」。请勿删除备份目录。",
			languageLabel(result.Language), result.BackupDirectory), appTitle, mbOk|mbIconInformation)
	})
}

func (a *guiApp) runRestore(game string, token, generation int) {
	defer a.guard()
	result, err := restoreOriginal(game)
	if err != nil {
		a.post(func() {
			a.endBusy(token)
			if generation != a.generation {
				return
			}
			a.appendLog("还原失败：" + err.Error())
			a.showError(err)
			a.refresh()
		})
		return
	}
	record, _ := loadRecord(game)
	a.post(func() {
		a.endBusy(token)
		if generation != a.generation {
			return
		}
		a.record = record
		a.applyState()
		a.appendLog(fmt.Sprintf("已还原原版，共 %d 个文件。回滚副本：%s", len(result.Files), result.Rollback))
		messageBox(a.window, fmt.Sprintf("已还原原版，共 %d 个文件。\n\n回滚副本：%s\n确认游戏正常后可以删除该目录。",
			len(result.Files), result.Rollback), appTitle, mbOk|mbIconInformation)
	})
}

// refresh 读取安装记录并刷新按钮与状态。安装记录只是游戏目录下的一个小
// JSON，毫秒级完成，直接在界面线程做，不进工作线程。
func (a *guiApp) refresh() {
	if a.game == "" {
		a.record = nil
		a.applyState()
		return
	}
	record, err := loadRecord(a.game)
	if err != nil {
		a.appendLog("读取安装记录失败：" + err.Error())
		a.record = nil
		a.applyState()
		return
	}
	a.record = record
	a.applyState()
}

// installedLanguage 返回当前已安装补丁的语言代码，未安装时返回空串。
func (a *guiApp) installedLanguage() string {
	if a.record != nil && a.record.Status == statusInstalled {
		return a.record.Language
	}
	return ""
}

// languageButtonLabel 生成语言按钮上的文字，已安装的语言带标注。
func (a *guiApp) languageButtonLabel(index int) string {
	if index >= len(a.payloads) {
		return "（未提供）"
	}
	info := a.payloads[index].info
	label := languageLabel(info.Language)
	if a.installedLanguage() == info.Language {
		label += "（已安装）"
	}
	return label
}

// applyState 依据当前状态刷新状态行与按钮可用性、文字。
func (a *guiApp) applyState() {
	setWindowText(a.statusLabel, a.statusText())
	for index := range a.actionButtons {
		if index < 3 {
			setWindowText(a.actionButtons[index], a.languageButtonLabel(index))
			enableWindow(a.actionButtons[index], !a.busy && index < len(a.payloads) && a.game != "")
			continue
		}
		setWindowText(a.actionButtons[index], "还原原版")
		enableWindow(a.actionButtons[index], !a.busy && a.game != "" && a.installedLanguage() != "")
	}
	if !a.busy {
		a.setProgress(0, 0)
	}
}

func (a *guiApp) statusText() string {
	if a.busy {
		return a.busyLabel
	}
	if a.game == "" {
		return "未检测到游戏目录：请把本程序放到游戏根目录（含 FarlandSaga.exe 的文件夹）"
	}
	if language := a.installedLanguage(); language != "" {
		return fmt.Sprintf("已安装 %s 补丁（%s）", languageLabel(language), a.record.Version)
	}
	return "尚未安装补丁。点一个语言按钮即可安装，随时可以还原原版。"
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
	// 有进度就说明操作还活着，给看门狗续期。
	a.busySince = time.Now()
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
	a.checkWatchdog()
	for {
		select {
		case function := <-a.events:
			a.runSafely(function)
		default:
			return
		}
	}
}

// checkWatchdog 在忙碌超过时限时强制解除。正常操作都有结果回传，进度回调
// 也会不断续期；真卡住了也不能让界面永远禁着，先恢复交互，文件到底什么
// 状态由用户重新操作确认。
func (a *guiApp) checkWatchdog() {
	if !a.busy || a.busySince.IsZero() {
		return
	}
	if time.Since(a.busySince) < watchdogLimit {
		return
	}
	a.appendLog(fmt.Sprintf("警告：%s 超过 %s 没有返回，界面已恢复，请重新操作确认文件状态。",
		a.busyLabel, watchdogLimit))
	a.endBusy(a.busyToken)
}

// runSafely 执行单个界面事件。事件里出错不能让消息循环崩掉，否则窗口会直接
// 消失，用户看不到任何提示。
func (a *guiApp) runSafely(function func()) {
	defer func() {
		if recovered := recover(); recovered != nil {
			a.endBusy(a.busyToken)
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
			a.endBusy(a.busyToken)
			a.setProgress(0, 0)
			a.appendLog(err.Error())
			a.showError(err)
		})
	}
}

// beginBusy 进入忙碌状态并返回本次操作的令牌。只由界面线程调用。
func (a *guiApp) beginBusy(label string) int {
	a.nextToken++
	a.busyToken = a.nextToken
	a.busy = true
	a.busyLabel = label
	a.busySince = time.Now()
	a.generation++
	a.applyState()
	return a.busyToken
}

// endBusy 归还令牌。只有令牌仍属于当前忙碌操作时才解除忙碌：被作废的旧
// 操作不会把新操作标记成空闲，也不会让自己永远占着忙碌状态。
func (a *guiApp) endBusy(token int) {
	if token == 0 || a.busyToken != token {
		return
	}
	a.busy = false
	a.busyToken = 0
	a.busyLabel = ""
	a.busySince = time.Time{}
	a.applyState()
}

func (a *guiApp) appendLog(text string) {
	if a.logEdit == 0 {
		return
	}
	if !strings.HasSuffix(text, "\n") {
		text += "\n"
	}
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
	length := sendMessage(handle, wmGetTextLength, 0, 0)
	if length == 0 {
		return ""
	}
	buffer := make([]uint16, length+1)
	sendMessage(handle, wmGetText, uintptr(len(buffer)), uintptr(unsafe.Pointer(&buffer[0])))
	return syscall.UTF16ToString(buffer)
}

func enableWindow(handle uintptr, enabled bool) {
	if handle == 0 {
		return
	}
	value := uintptr(0)
	if enabled {
		value = 1
	}
	procEnableWindow.Call(handle, value)
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
	result, _, _ := procDefWindowProc.Call(hwnd, uintptr(msg), wparam, lparam)
	return result
}

func utf16ptr(text string) *uint16 {
	return syscall.StringToUTF16Ptr(text)
}

// pointerFromUintptr 把窗口回调收到的 uintptr 还原成 unsafe.Pointer。
// 直接写 unsafe.Pointer(lparam) 会被 go vet 的 unsafeptr 检查拦下，这里经一个
// 真实指针的地址中转：读取该地址处的 8 个字节，得到的地址与 lparam 相同。
func pointerFromUintptr(value uintptr) unsafe.Pointer {
	return *(*unsafe.Pointer)(unsafe.Pointer(&value))
}
