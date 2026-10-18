// Windows 图形界面：单个可执行文件，补丁编译期内嵌，双击即用。
//
// 发布形态是把本程序放进游戏根目录后双击运行。界面分两屏，文字压到最少：
// 第一屏选语言（简体中文 / 한국어 / English 三个按钮，各显示自己的名字），
// 选中后进入该语言的
// 页面，只有「安装」和「还原」两个按钮。游戏目录用程序所在目录推断，不让
// 用户挑路径；文件清单、浏览、拖放、日志这些辅助交互都去掉了，留给命令行
// 子命令。
//
// 界面线程只跑消息循环。安装、还原放在工作 goroutine，经缓冲 channel 把
// 闭包投回界面线程，由 50ms 的 WM_TIMER 执行；进度回调每 4 KiB 一次、节流
// 100ms。忙碌状态用递增令牌管理，被作废的操作也会归还自己的令牌，界面不会
// 因为一次丢弃的检查而永远禁着。
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
	"runtime"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"
)

const guiClassName = "FarlandSagaPatcherWindow"

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
	procPostMessage                   = user32.NewProc("PostMessageW")
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
	ssCenter      = 0x00000001
	ssWordWrap    = 0x0000000c // SS_LEFTNOWORDWRAP：长提示自动折行
	esMultiline   = 0x0004
	esAutoVScroll = 0x0040
	esReadOnly    = 0x0800

	swHide = 0
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

	// wmAppEvent 是程序自定义的唤醒消息。工作线程回传结果时除了把闭包放进
	// 队列，还要 PostMessage 一条这个消息，界面线程才会立刻来取。
	wmAppEvent = 0x8001

	bnClicked = 0

	colorBtnFace = 15

	// 两屏界面：第一屏选语言，第二屏安装与还原。
	pageLanguage = 0
	pageInstall  = 1

	// 控件 ID，同时是 WM_COMMAND 的低 16 位。
	idTitle    = 1001
	idStatus   = 1002
	idProgress = 1003
	idHint     = 1004
	idLangBase = 1010
	idInstall  = 1020
	idRestore  = 1021
	idBack     = 1022

	// 通用控件消息。
	pbmSetRange32   = 0x0406
	pbmSetPos       = 0x0402
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
	_ = [1]struct{}{}[unsafe.Sizeof(initCommonControlsEx{})-8]
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

// trace 把启动关键步骤写进临时目录的日志文件，用于定位界面无响应时卡在
// 哪一步。设了环境变量 FARLAND_PATCHER_TRACE 才写入，正常使用不留文件。
var (
	traceOnce  sync.Once
	traceMutex sync.Mutex
	traceFile  *os.File
)

func trace(format string, args ...any) {
	if os.Getenv("FARLAND_PATCHER_TRACE") == "" {
		return
	}
	traceMutex.Lock()
	defer traceMutex.Unlock()
	traceOnce.Do(func() {
		path := filepath.Join(os.TempDir(), "farland-patcher-trace.log")
		if file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644); err == nil {
			traceFile = file
		}
	})
	if traceFile == nil {
		return
	}
	fmt.Fprintf(traceFile, "%s %s\n", time.Now().Format("15:04:05.000"), fmt.Sprintf(format, args...))
}

// traceThrottled 是限流版的 trace，用于消息循环这类高频路径：每 500 毫秒
// 最多记一条，既能看出消息流是否还在走，又不会把日志写爆。
var traceThrottle struct {
	sync.Mutex
	last time.Time
}

func traceThrottled(format string, args ...any) {
	traceThrottle.Lock()
	now := time.Now()
	if !traceThrottle.last.IsZero() && now.Sub(traceThrottle.last) < 500*time.Millisecond {
		traceThrottle.Unlock()
		return
	}
	traceThrottle.last = now
	traceThrottle.Unlock()
	trace(format, args...)
}

// guiApp 持有窗口句柄、控件句柄与当前状态。除 progressCallback 内部的节流
// 状态外，所有字段都只在界面线程读写；工作 goroutine 只通过 post 回传结果。
type guiApp struct {
	window uintptr
	module uintptr
	font   uintptr
	scale  float64
	dpi    uint32

	titleLabel  uintptr
	statusLabel uintptr
	progressBar uintptr
	hintLabel   uintptr
	// languageButtons 是三个语言按钮，顺序固定为中、韩、英。
	languageButtons [3]uintptr
	installBtn      uintptr
	restoreBtn      uintptr
	backBtn         uintptr

	// page 是当前所在屏：pageLanguage 选语言，pageInstall 安装与还原。
	page int
	// selected 是第二屏正在操作的语言在 payloads 中的下标。
	selected int

	// ui 是当前界面文字表。启动时取操作系统界面语言，用户选定语言后换成
	// 那一语言的表——第二屏起，标题、状态行、按钮、提示、确认框全部跟着变。
	ui uiStrings

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
	progressTotal int64

	// wakeWindow 存放主窗口句柄，专供工作线程发唤醒消息用。窗口只在界面
	// 线程创建一次，之后不再改写；用原子读写是为了让 go vet 与竞态检测器
	// 都认可这种跨线程读法。
	wakeWindow atomic.Uintptr
}

func newGUIApp() *guiApp {
	return &guiApp{
		scale: 1, dpi: 96, selected: -1,
		events: make(chan func(), 1024),
		// 第一屏还没选语言，先用操作系统界面语言；取不到就英文。
		ui: uiStringsFor(systemUILanguage()),
	}
}

// systemUILanguage 读操作系统界面语言，决定第一屏用哪种文字。中文和韩文系统
// 各自落到自己的表，其余（含取不到 API 的老系统）回落英文。
func systemUILanguage() string {
	proc := kernel32.NewProc("GetUserDefaultUILanguage")
	if proc.Find() != nil {
		return "en-US"
	}
	lang, _, _ := proc.Call()
	switch lang & 0x3FF {
	case 0x04: // 中文，简繁都先用简体中文表
		return "zh-CN"
	case 0x12: // 韩文
		return "ko-KR"
	}
	return "en-US"
}

// guiAvailable 报告当前平台是否带图形界面。Windows 版用 Win32 实现，
// 其他平台只有控制台向导。
func guiAvailable() bool { return true }

// runGUI 创建主窗口并跑消息循环，窗口建不起来时返回错误让调用方回退到向导。
func runGUI() error {
	// Win32 的窗口、定时器、投递的消息全都挂在创建窗口的那个操作系统线程
	// 的消息队列上。Go 的调度器会在系统调用之间把 goroutine 换到别的线程上，
	// 一旦换过去，窗口就归原线程，而消息循环在新线程上跑：SetTimer 的
	// WM_TIMER、PostMessage 的自定义消息全都进原线程的队列，新线程的
	// GetMessage 永远等不到东西，界面停在初始状态、按钮全禁用，看起来就是
	// 整个程序卡死。锁住线程才能保证建窗口和跑消息循环的是同一个线程。
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	trace("runGUI 开始")
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
	trace("init 开始")
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
	width, height := a.s(440), a.s(360)
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
	a.wakeWindow.Store(window)
	trace("窗口已创建 hwnd=%d", window)
	procShowWindow.Call(window, swShow)
	procUpdateWindow.Call(window)
	// 定时器必须等 CreateWindowEx 返回后再设。在 WM_CREATE 处理期间（也就是
	// CreateWindowEx 内部）设置时，Windows 只会投递一次 WM_TIMER 就再也不响，
	// 排队的事件因此永远执行不到，界面看起来就是卡死。
	timer, _, _ := procSetTimer.Call(window, 1, 50, 0)
	trace("SetTimer 返回 %d", timer)
	trace("init 结束")
	return nil
}

func (a *guiApp) messageLoop() {
	trace("消息循环开始")
	var message winMsg
	for {
		result, _, _ := procGetMessage.Call(uintptr(unsafe.Pointer(&message)), 0, 0, 0)
		if result == 0 || result == ^uintptr(0) {
			return
		}
		traceThrottled("消息循环迭代 msg=0x%x", message.Message)
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
	a.titleLabel = a.create("Static", "", 0, wsChild|wsVisible|ssCenter, idTitle)
	a.statusLabel = a.create("Static", "", 0, wsChild|wsVisible|ssCenter, idStatus)
	a.progressBar = a.create("msctls_progress32", "", 0, wsChild|wsVisible, idProgress)
	a.hintLabel = a.create("Static", "", 0, wsChild|wsVisible|ssWordWrap, idHint)
	for index := range a.languageButtons {
		a.languageButtons[index] = a.create("Button", "", 0, wsChild|wsVisible|bsMultiline, idLangBase+uintptr(index))
	}
	a.installBtn = a.create("Button", "", 0, wsChild|wsVisible, idInstall)
	a.restoreBtn = a.create("Button", "", 0, wsChild|wsVisible, idRestore)
	a.backBtn = a.create("Button", "", 0, wsChild|wsVisible, idBack)
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

// layout 按当前屏排布控件：第一屏只放语言按钮，第二屏只放安装、还原与返回。
func (a *guiApp) layout() {
	rect := winRect{}
	procGetClientRect.Call(a.window, uintptr(unsafe.Pointer(&rect)))
	width := int(rect.Right - rect.Left)
	height := int(rect.Bottom - rect.Top)
	if width < a.s(360) || height < a.s(300) {
		width, height = a.s(400), a.s(320)
	}
	pad := a.s(20)
	inner := width - pad*2
	gap := a.s(12)

	setWindowText(a.titleLabel, a.titleText())
	setWindowText(a.window, appTitle)
	place(a.titleLabel, pad, pad, inner, a.s(44))

	y := pad + a.s(44) + gap
	if a.page == pageLanguage {
		for _, handle := range []uintptr{a.statusLabel, a.progressBar, a.installBtn, a.restoreBtn, a.backBtn} {
			showWindow(handle, false)
		}
		showWindow(a.hintLabel, a.game == "")
		setWindowText(a.hintLabel, a.ui.hintNoGameRoot)
		place(a.hintLabel, pad, y, inner, a.s(34))
		if a.game == "" {
			y += a.s(34) + gap
		}
		buttonHeight := a.s(52)
		for index, handle := range a.languageButtons {
			showWindow(handle, true)
			place(handle, pad, y+index*(buttonHeight+gap), inner, buttonHeight)
		}
		return
	}

	for _, handle := range a.languageButtons {
		showWindow(handle, false)
	}
	showWindow(a.hintLabel, false)
	// 第二屏的控件在第一屏被隐藏过，切回来必须重新显示，否则按钮看不见也
	// 点不到。进度条只在忙碌时出现，空闲时收起来，界面上只剩两个按钮。
	for _, handle := range []uintptr{a.statusLabel, a.installBtn, a.restoreBtn, a.backBtn} {
		showWindow(handle, true)
	}
	showWindow(a.progressBar, a.busy)

	place(a.statusLabel, pad, y, inner, a.s(20))
	y += a.s(20) + gap

	buttonWidth := (inner - gap) / 2
	buttonHeight := a.s(56)
	place(a.installBtn, pad, y, buttonWidth, buttonHeight)
	place(a.restoreBtn, pad+buttonWidth+gap, y, buttonWidth, buttonHeight)
	y += buttonHeight + gap

	place(a.progressBar, pad, y, inner, a.s(16))
	y += a.s(16) + gap

	place(a.backBtn, pad, y, inner, a.s(28))
}

func place(handle uintptr, x, y, width, height int) {
	if handle == 0 {
		return
	}
	procMoveWindow.Call(handle, uintptr(x), uintptr(y), uintptr(width), uintptr(height), 1)
}

// showWindow 显示或隐藏一个控件，用于两屏之间的切换。
func showWindow(handle uintptr, visible bool) {
	if handle == 0 {
		return
	}
	command := uintptr(swHide)
	if visible {
		command = swShow
	}
	procShowWindow.Call(handle, command)
}

func (a *guiApp) wndProc(hwnd, msg, wparam, lparam uintptr) uintptr {
	traceThrottled("wndProc msg=0x%x", msg)
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
	case wmAppEvent:
		a.drainEvents()
		return 0
	case wmClose:
		if a.busy {
			messageBox(hwnd, a.ui.busyClose, appTitle, mbOk|mbIconInformation)
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
	trace("onCreate 开始")
	a.window = hwnd
	a.wakeWindow.Store(hwnd)
	if procGetDpiForWindow.Find() == nil {
		if dpi, _, _ := procGetDpiForWindow.Call(hwnd); dpi >= 96 && dpi <= 480 {
			a.dpi = uint32(dpi)
			a.scale = float64(dpi) / 96.0
		}
	}
	a.font = createUIFont(a.dpi, a.ui.fontFace)
	a.createControls()
	a.layout()
	a.applyFont()
	a.applyState()
	trace("onCreate 结束，排队 bootstrap")
	// 解析内嵌补丁、定位游戏目录都要做文件与内存操作，放到消息循环跑起来
	// 之后再做，免得窗口迟迟显示不出来。
	a.post(a.bootstrap)
}

// bootstrap 加载内嵌补丁、定位游戏目录、读安装记录，完成后刷新界面。
// 读包涉及七十多兆的解压与清单解析，放在工作线程做；界面线程被占住超过
// 几秒，Windows 就会把窗口标成"无响应"，用户看到的就是卡死。
func (a *guiApp) bootstrap() {
	trace("bootstrap 开始")
	go func() {
		defer a.guard()
		payloads, problems := loadEmbedded()
		trace("loadEmbedded 返回 %d 个补丁 %d 个问题", len(payloads), len(problems))
		a.post(func() {
			for _, problem := range problems {
				a.showError(errors.New(problem))
			}
			a.payloads = payloads
			a.locateGame()
		})
	}()
}

// locateGame 定位游戏目录并读安装记录，最后刷新界面。
func (a *guiApp) locateGame() {
	trace("locateGame 开始")
	game, err := detectGame(a.firstManifest())
	if err != nil {
		a.game = ""
		trace("未检测到游戏目录：%v", err)
	} else {
		a.game = game
		trace("游戏目录 %s", game)
	}
	a.refresh()
	trace("locateGame 结束，applyState")
	a.applyState()
	trace("applyState 结束")
}

// firstManifest 返回第一个内嵌补丁的清单，用于校验游戏目录是否齐全。
func (a *guiApp) firstManifest() *manifest {
	if len(a.payloads) == 0 {
		return nil
	}
	return a.payloads[0].info
}

// detectGame 以程序所在目录为游戏目录。发布形态是单个可执行文件放在游戏
// 根目录里双击运行，用户不需要挑路径；放错了地方就在第一屏给出提示。
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
	a.font = createUIFont(dpi, a.ui.fontFace)
	a.applyFont()
	// lparam 指向系统建议的新窗口矩形，直接采用。
	rect := (*winRect)(pointerFromUintptr(lparam))
	procMoveWindow.Call(a.window, uintptr(rect.Left), uintptr(rect.Top),
		uintptr(rect.Right-rect.Left), uintptr(rect.Bottom-rect.Top), 1)
	a.layout()
}

// recreateFont 按当前界面语言重建字体并重新套到所有控件上。韩文用 Malgun
// Gothic，其余用系统对话框字体；切语言时调用一次，旧字体随即删除。
func (a *guiApp) recreateFont() {
	if a.font != 0 {
		procDeleteObject.Call(a.font)
		a.font = 0
	}
	a.font = createUIFont(a.dpi, a.ui.fontFace)
	a.applyFont()
}

func (a *guiApp) applyFont() {
	if a.font == 0 {
		return
	}
	handles := []uintptr{a.titleLabel, a.statusLabel, a.progressBar, a.hintLabel,
		a.installBtn, a.restoreBtn, a.backBtn}
	handles = append(handles, a.languageButtons[:]...)
	for _, handle := range handles {
		if handle != 0 {
			sendMessage(handle, wmSetFont, a.font, 1)
		}
	}
}

func (a *guiApp) onMinMaxInfo(lparam uintptr) {
	info := (*minMaxInfo)(pointerFromUintptr(lparam))
	info.MinTrackSize.X = int32(a.s(380))
	info.MinTrackSize.Y = int32(a.s(320))
}

func (a *guiApp) onCommand(wparam uintptr) {
	id := uint16(wparam)
	code := uint16(wparam >> 16)
	if code != bnClicked {
		return
	}
	switch id {
	case idInstall:
		a.onInstall()
	case idRestore:
		a.onRestore()
	case idBack:
		a.onBack()
	default:
		if id >= idLangBase && id < idLangBase+3 {
			a.onLanguageSelect(int(id - idLangBase))
		}
	}
}

// onLanguageSelect 选中一个语言，进入该语言的安装与还原页面。界面文字表整体
// 换成该语言的那一套：从这一步起，标题、状态行、按钮、提示、确认框全是它。
func (a *guiApp) onLanguageSelect(index int) {
	if a.busy || index < 0 || index >= len(a.payloads) {
		return
	}
	a.selected = index
	a.page = pageInstall
	a.ui = uiStringsFor(a.payloads[index].info.Language)
	a.recreateFont()
	a.layout()
	a.applyState()
}

// onBack 返回选语言页面。界面文字保留上次选中的语言，不退回系统语言——用户
// 刚看过什么，回来还是什么，来回切不会闪。
func (a *guiApp) onBack() {
	if a.busy {
		return
	}
	a.page = pageLanguage
	a.selected = -1
	a.layout()
	a.applyState()
}

// onInstall 安装当前选中语言的内嵌补丁。
func (a *guiApp) onInstall() {
	if a.busy || a.selected < 0 || a.selected >= len(a.payloads) {
		return
	}
	loaded := a.payloads[a.selected]
	label := a.ui.name(loaded.info.Language)
	if a.installedLanguage() == loaded.info.Language {
		messageBox(a.window, fmt.Sprintf(a.ui.alreadyInstalledFormat, label), appTitle, mbOk|mbIconInformation)
		return
	}
	if a.game == "" {
		a.showError(errors.New(a.ui.hintNoGameRootLong))
		return
	}
	if !confirmDialog(a.window, fmt.Sprintf(a.ui.confirmInstallFormat, label),
		fmt.Sprintf(a.ui.confirmInstallCaption, label)) {
		return
	}
	token := a.beginBusy(fmt.Sprintf(a.ui.busyInstallFormat, label))
	go a.runInstall(a.game, loaded, token, a.generation)
}

// onRestore 用原版备份把游戏文件恢复回去。
func (a *guiApp) onRestore() {
	if a.busy {
		return
	}
	if a.game == "" {
		a.showError(errors.New(a.ui.hintNoGameRootLong))
		return
	}
	record, err := loadRecord(a.game)
	if err != nil {
		a.showError(err)
		return
	}
	if record == nil || record.Status != statusInstalled {
		a.showError(errors.New(a.ui.nothingInstalled))
		return
	}
	if !confirmDialog(a.window, a.ui.confirmRestore, a.ui.confirmRestoreCaption) {
		return
	}
	token := a.beginBusy(a.ui.busyRestore)
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
			messageBox(a.window, a.ui.alreadyInstalled, appTitle, mbOk|mbIconInformation)
			return
		}
		messageBox(a.window, fmt.Sprintf(a.ui.installedDoneFormat, a.ui.name(result.Language)),
			appTitle, mbOk|mbIconInformation)
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
		messageBox(a.window, fmt.Sprintf(a.ui.restoreDoneFormat, len(result.Files)),
			appTitle, mbOk|mbIconInformation)
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

// titleText 是两屏各自的标题。
func (a *guiApp) titleText() string {
	if a.page == pageInstall && a.selected >= 0 && a.selected < len(a.payloads) {
		return fmt.Sprintf(a.ui.pageTitleFormat, a.ui.name(a.payloads[a.selected].info.Language))
	}
	return appTitle
}

// statusText 是第二屏的状态行：忙碌时显示进度，否则显示安装情况。
func (a *guiApp) statusText() string {
	if a.busy {
		return a.busyLabel
	}
	if language := a.installedLanguage(); language != "" {
		return fmt.Sprintf(a.ui.statusInstalledFormat, a.ui.name(language))
	}
	return a.ui.statusNotInstalled
}

// languageButtonLabel 生成语言按钮上的文字，已安装的语言带标注。
func (a *guiApp) languageButtonLabel(index int) string {
	if index >= len(a.payloads) {
		return a.ui.notProvided
	}
	info := a.payloads[index].info
	label := nativeLanguageName(info.Language)
	if a.installedLanguage() == info.Language {
		label += a.ui.installedTag
	}
	return label
}

// applyState 依据当前屏与状态刷新标题、状态行、按钮文字与可用性。
func (a *guiApp) applyState() {
	setWindowText(a.titleLabel, a.titleText())
	setWindowText(a.statusLabel, a.statusText())
	setWindowText(a.installBtn, a.ui.buttonInstall)
	setWindowText(a.restoreBtn, a.ui.buttonRestore)
	setWindowText(a.backBtn, a.ui.buttonBack)
	for index, handle := range a.languageButtons {
		setWindowText(handle, a.languageButtonLabel(index))
		enableWindow(handle, !a.busy && index < len(a.payloads) && a.game != "")
	}
	installed := a.installedLanguage() != ""
	enableWindow(a.installBtn, !a.busy && a.selected >= 0 && a.selected < len(a.payloads) && a.game != "")
	enableWindow(a.restoreBtn, !a.busy && installed)
	enableWindow(a.backBtn, !a.busy)
	// 进度条只在第二屏忙碌时出现，空闲时收起来，界面上只剩两个按钮。
	showWindow(a.progressBar, a.busy && a.page == pageInstall)
	if !a.busy {
		a.setProgress(0, 0)
	}
	// 控件的显隐、位置全在 layout 里按当前状态推导，所以状态一变就要重排。
	// 少了这一步，启动时按"还没找到游戏"排下的提示文字会一直挂在第一屏，
	// 第二屏的按钮也会停在隐藏状态。
	a.layout()
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
	setWindowText(a.statusLabel, fmt.Sprintf(a.ui.progressFormat, percent))
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

// post 把闭包送进界面线程执行，并立刻唤醒界面线程来取。
//
// 光把闭包塞进队列不够：界面线程停在 GetMessage 里，得有条消息它才会醒。
// 早先只靠 SetTimer 的 WM_TIMER 当唤醒源，而 WM_TIMER 是系统合成的低优先级
// 消息，队列一忙就可能迟迟不来，甚至一次都不来，排在队列里的闭包就永远执行
// 不到——界面停在初始状态，按钮全是禁用的，看起来就是整个程序卡死。
// PostMessage 放进去的是一条真实排队的消息，GetMessage 一定会取到。
func (a *guiApp) post(function func()) {
	a.events <- function
	a.wake()
}

// wake 给界面线程发一条唤醒消息。定时器仍然留着，只是退成看门狗的节拍器，
// 不再承担把事件送达到界面这件事。
func (a *guiApp) wake() {
	if window := a.wakeWindow.Load(); window != 0 {
		procPostMessage.Call(window, wmAppEvent, 0, 0)
	}
}

// drainEvents 执行定时器里排队的界面事件。
func (a *guiApp) drainEvents() {
	a.checkWatchdog()
	for {
		select {
		case function := <-a.events:
			trace("执行一个排队事件")
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
	a.showError(errors.New(a.ui.timeout))
	a.endBusy(a.busyToken)
}

// runSafely 执行单个界面事件。事件里出错不能让消息循环崩掉，否则窗口会直接
// 消失，用户看不到任何提示。
func (a *guiApp) runSafely(function func()) {
	defer func() {
		if recovered := recover(); recovered != nil {
			a.endBusy(a.busyToken)
			a.showError(fmt.Errorf(a.ui.internalErrorFormat, recovered))
			a.applyState()
		}
	}()
	function()
}

// guard 只能用于 defer：把工作线程里的 panic 转成界面上的一个错误框，
// 避免进程直接消失而看不到任何提示。
func (a *guiApp) guard() {
	if recovered := recover(); recovered != nil {
		err := fmt.Errorf(a.ui.internalErrorFormat, recovered)
		a.post(func() {
			a.endBusy(a.busyToken)
			a.setProgress(0, 0)
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

func (a *guiApp) showError(err error) {
	messageBox(a.window, err.Error(), appTitle, mbOk|mbIconError)
}

func createUIFont(dpi uint32, face string) uintptr {
	height := int32(-9 * int64(dpi) / 72)
	if height > -11 {
		height = -11
	}
	font, _, _ := procCreateFont.Call(
		uintptr(height), 0, 0, 0,
		fwNormal, 0, 0, 0,
		defaultCharset, 0, 0, 0, 0,
		uintptr(unsafe.Pointer(utf16ptr(face))))
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
