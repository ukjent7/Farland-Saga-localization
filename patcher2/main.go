// FARLAND SAGA II Saturn Tribute 三语本地化补丁器。
//
// 独立可执行文件，不依赖 Python：读取 tools/package_release.py 生成的补丁
// 载荷（manifest.json + 相对原版的 4 KiB 差异对象），核对游戏文件身份后备份、
// 回放、原子替换，并可随时从备份恢复原版。Windows 版不带参数运行进入图形界面，
// 其他平台进入控制台向导；wizard 子命令在任何平台都走控制台向导。
package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

const gameEnvVar = "FARLAND_SAGA_GAME"

// humanSize 把字节数折算成便于阅读的单位。清扫旧备份动辄几百 MB，用 MiB/GiB
// 比一串数字更能说明腾出了多少空间。
func humanSize(size int64) string {
	units := []struct {
		divisor float64
		suffix  string
	}{
		{1 << 30, "GiB"},
		{1 << 20, "MiB"},
		{1 << 10, "KiB"},
	}
	for _, unit := range units {
		if float64(size) >= unit.divisor {
			return fmt.Sprintf("%.1f %s", float64(size)/unit.divisor, unit.suffix)
		}
	}
	return fmt.Sprintf("%d B", size)
}

type payloadChoice struct {
	path string
	info *manifest
}

func main() {
	enableUTF8Console()
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "错误：%v\n", err)
		os.Exit(1)
	}
}

func run(argv []string) error {
	if len(argv) == 0 {
		// Windows 版默认进图形界面；界面起不来时退回控制台向导。
		if guiAvailable() {
			if err := runGUI(); err != nil {
				fmt.Fprintf(os.Stderr, "图形界面启动失败：%v\n", err)
				if !interactive() {
					return err
				}
				return wizard()
			}
			return nil
		}
		if !interactive() {
			printUsage()
			return errors.New("非交互环境请使用 install / restore / status / verify 子命令")
		}
		return wizard()
	}
	switch argv[0] {
	case "install", "restore", "status", "verify":
		return runCommand(argv[0], argv[1:])
	case "gui":
		return runGUI()
	case "wizard", "cli":
		return wizard()
	case "help", "--help", "-h":
		printUsage()
		return nil
	default:
		printUsage()
		return fmt.Errorf("未知命令 %q", argv[0])
	}
}

func runCommand(command string, arguments []string) error {
	set := flag.NewFlagSet(command, flag.ContinueOnError)
	set.SetOutput(io.Discard)
	game := set.String("game", "", "游戏安装目录")
	payloadPath := set.String("payload", "", "补丁载荷 ZIP 路径")
	yes := set.Bool("yes", false, "跳过交互确认")
	asJSON := set.Bool("json", false, "输出 JSON")
	if err := set.Parse(arguments); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			printUsage()
			return nil
		}
		return err
	}
	if set.NArg() > 0 {
		return fmt.Errorf("无法识别的参数: %s", strings.Join(set.Args(), " "))
	}
	allowPrompt := interactive() && !*yes
	switch command {
	case "install":
		return commandInstall(*game, *payloadPath, *yes, *asJSON, allowPrompt)
	case "restore":
		return commandRestore(*game, *yes, *asJSON, allowPrompt)
	case "status":
		return commandStatus(*game, *payloadPath, *asJSON, allowPrompt)
	default:
		return commandVerify(*game, *payloadPath, *asJSON, allowPrompt)
	}
}

func commandInstall(game, payloadPath string, yes, asJSON, allowPrompt bool) error {
	loaded, err := resolvePayload(payloadPath)
	if err != nil {
		return err
	}
	defer loaded.close()
	game, err = resolveGame(game, loaded.info, allowPrompt)
	if err != nil {
		return err
	}
	prior, err := loadRecord(game)
	if err != nil {
		return err
	}
	statuses := inspectAll(game, loaded.info, prior)
	if !asJSON {
		fmt.Printf("补丁载荷：%s\n", filepath.Base(loaded.path))
		fmt.Printf("版本：%s（%s）\n", loaded.info.Version, loaded.info.Language)
		fmt.Printf("游戏目录：%s\n", game)
		printStatusTable(statuses)
	}
	if !yes && !allowPrompt {
		return errors.New("非交互环境请加 --yes 确认安装")
	}
	if !yes && !confirm(fmt.Sprintf("现在安装 %s（%s）吗？", loaded.info.Version, loaded.info.Language)) {
		return errors.New("已取消")
	}
	result, err := installPatch(game, loaded, progressReporter(asJSON))
	if err != nil {
		return err
	}
	if asJSON {
		return printJSON(result)
	}
	if result.AlreadyInstalled {
		fmt.Printf("\n%s（%s）已经安装，无需重复操作。\n", result.Version, result.Language)
		return nil
	}
	fmt.Printf("\n已安装 %s（%s），共 %d 个文件。\n", result.Version, result.Language, len(result.Files))
	fmt.Printf("原版备份：%s\n", result.BackupDirectory)
	if len(result.CleanedUp) > 0 {
		fmt.Printf("已清理残留目录 %d 个，腾出 %s：%s\n",
			len(result.CleanedUp), humanSize(result.ReclaimedBytes), strings.Join(result.CleanedUp, "、"))
	}
	fmt.Println("如需恢复原版，再次运行本程序并选择「恢复原版」。请勿删除备份目录。")
	return nil
}

func commandRestore(game string, yes, asJSON, allowPrompt bool) error {
	game, err := resolveGame(game, nil, allowPrompt)
	if err != nil {
		return err
	}
	record, err := loadRecord(game)
	if err != nil {
		return err
	}
	if record == nil {
		return fmt.Errorf("没有找到安装记录 %s，无法恢复", recordName)
	}
	if !asJSON {
		fmt.Printf("游戏目录：%s\n", game)
		fmt.Printf("已安装版本：%s（%s）\n", record.Version, record.Language)
		// 备份是否可用要在确认之前说清楚，别等用户按了确认才失败。
		health := inspectBackup(game, backupTargetsFromRecord(record))
		switch {
		case !health.Present:
			fmt.Printf("原版备份：%s 不存在，无法恢复\n", health.Directory)
		case !health.Complete:
			fmt.Printf("原版备份：%s 不完整，无法恢复\n%s\n", health.Directory, strings.Join(health.Problems, "\n"))
		default:
			fmt.Printf("原版备份：%s（完整）\n", effectiveBackupName(game, backupTargetsFromRecord(record), record.BackupDirectory))
		}
	}
	if !yes && !allowPrompt {
		return errors.New("非交互环境请加 --yes 确认恢复")
	}
	if !yes && !confirm("用原版备份覆盖当前已打补丁的文件吗？") {
		return errors.New("已取消")
	}
	result, err := restoreOriginal(game)
	if err != nil {
		return err
	}
	if asJSON {
		return printJSON(result)
	}
	fmt.Printf("\n已恢复原版，共 %d 个文件。\n", len(result.Files))
	fmt.Println("需要补丁时再次运行本程序并安装即可，原版备份仍保留着。")
	return nil
}

func commandStatus(game, payloadPath string, asJSON, allowPrompt bool) error {
	loaded, err := resolvePayloadOptional(payloadPath)
	if err != nil {
		return err
	}
	if loaded != nil {
		defer loaded.close()
	}
	game, err = resolveGame(game, payloadInfo(loaded), allowPrompt)
	if err != nil {
		return err
	}
	record, err := loadRecord(game)
	if err != nil {
		return err
	}
	statuses, err := verifyInstall(game, loaded)
	if err != nil {
		return err
	}
	var health backupHealth
	var targets []backupTarget
	if record != nil {
		targets = backupTargetsFromRecord(record)
		health = inspectBackup(game, targets)
	}
	if asJSON {
		return printJSON(map[string]any{
			"game": game, "payload": payloadSummary(loaded), "record": record, "files": statuses,
			"backup": health,
		})
	}
	fmt.Printf("游戏目录：%s\n", game)
	if loaded != nil {
		fmt.Printf("补丁载荷：%s（%s，%s）\n", filepath.Base(loaded.path), loaded.info.Version, loaded.info.Language)
	}
	if record != nil {
		fmt.Printf("安装记录：%s（%s），状态 %s\n", record.Version, record.Language, record.Status)
		if record.Status == statusInstalled {
			switch {
			case !health.Present:
				fmt.Printf("原版备份：%s 不存在，还原前必须先修复\n", health.Directory)
			case !health.Complete:
				fmt.Printf("原版备份：%s 不完整，还原前必须先修复\n%s\n", health.Directory, strings.Join(health.Problems, "\n"))
			default:
				fmt.Printf("原版备份：%s（完整）\n", health.Directory)
			}
		}
	} else {
		fmt.Println("安装记录：无")
	}
	printStatusTable(statuses)
	return nil
}

func commandVerify(game, payloadPath string, asJSON, allowPrompt bool) error {
	loaded, err := resolvePayloadOptional(payloadPath)
	if err != nil {
		return err
	}
	if loaded != nil {
		defer loaded.close()
	}
	game, err = resolveGame(game, payloadInfo(loaded), allowPrompt)
	if err != nil {
		return err
	}
	statuses, err := verifyInstall(game, loaded)
	if err != nil {
		return err
	}
	ok := true
	var problems []string
	for _, status := range statuses {
		switch {
		case loaded != nil && (status.State == stateOriginal || status.State == statePatched):
		case loaded == nil && status.State == statePatched:
		default:
			ok = false
			problems = append(problems, fmt.Sprintf("  %-44s %s", status.Path, describeState(status)))
		}
	}
	if asJSON {
		if err := printJSON(map[string]any{"game": game, "ok": ok, "files": statuses}); err != nil {
			return err
		}
	} else {
		fmt.Printf("游戏目录：%s\n", game)
		printStatusTable(statuses)
		if ok {
			if loaded != nil {
				fmt.Println("核对通过：所有文件都处于可安装状态（原版，或已是本补丁）。")
			} else {
				fmt.Println("核对通过：所有文件都与安装记录一致。")
			}
		} else {
			fmt.Println("核对未通过:")
			fmt.Println(strings.Join(problems, "\n"))
		}
	}
	if !ok {
		return errors.New("核对未通过")
	}
	return nil
}

// wizard 是不带参数运行时的交互流程：定位游戏、展示状态，再安装或恢复。
// 已经装过补丁时直接给出恢复/切换菜单，不必先选语言。
func wizard() error {
	choices, problems := discoverPayloads()
	for _, problem := range problems {
		fmt.Printf("警告：%s\n", problem)
	}
	game, err := resolveGame("", nil, true)
	if err != nil {
		return err
	}
	prior, err := loadRecord(game)
	if err != nil {
		return err
	}
	if prior != nil && prior.Status == statusInstalled {
		return wizardInstalled(game, prior, choices)
	}
	if len(choices) == 0 {
		return errors.New("没有找到补丁载荷：请把 farland-patch-*.zip 放到本程序所在目录")
	}
	chosen := choices[0]
	if len(choices) > 1 {
		fmt.Println("发现多个补丁载荷：")
		for index, choice := range choices {
			fmt.Printf("  %d) %s（%s）\n", index+1, choice.info.Version, choice.info.Language)
		}
		answer := prompt("选择要安装的语言/版本（输入序号）")
		index, err := strconv.Atoi(strings.TrimSpace(answer))
		if err != nil || index < 1 || index > len(choices) {
			return errors.New("无效选择")
		}
		chosen = choices[index-1]
	}
	loaded, err := openPayload(chosen.path)
	if err != nil {
		return err
	}
	defer loaded.close()
	// 记住的目录可能不是这款游戏的安装目录，按载荷再校验一次，不行就重新问。
	if _, err := validateGameDir(game, loaded.info); err != nil {
		game, err = resolveGame("", loaded.info, true)
		if err != nil {
			return err
		}
		prior, err = loadRecord(game)
		if err != nil {
			return err
		}
	}
	statuses := inspectAll(game, loaded.info, prior)
	fmt.Printf("\n补丁载荷：%s（%s，%s）\n", filepath.Base(loaded.path), loaded.info.Version, loaded.info.Language)
	fmt.Printf("游戏目录：%s\n", game)
	printStatusTable(statuses)
	if !confirm(fmt.Sprintf("现在安装 %s（%s）吗？", loaded.info.Version, loaded.info.Language)) {
		return errors.New("已取消")
	}
	result, err := installPatch(game, loaded, progressReporter(false))
	if err != nil {
		return err
	}
	fmt.Printf("\n已安装 %s（%s），共 %d 个文件。\n", result.Version, result.Language, len(result.Files))
	fmt.Printf("原版备份：%s\n", result.BackupDirectory)
	if len(result.CleanedUp) > 0 {
		fmt.Printf("已清理残留目录 %d 个，腾出 %s：%s\n",
			len(result.CleanedUp), humanSize(result.ReclaimedBytes), strings.Join(result.CleanedUp, "、"))
	}
	fmt.Println("如需恢复原版，再次运行本程序并选择「恢复原版」。请勿删除备份目录。")
	return nil
}

// wizardInstalled 在已安装补丁时提供恢复与切换。
func wizardInstalled(game string, prior *installRecord, choices []payloadChoice) error {
	fmt.Printf("游戏目录：%s\n", game)
	fmt.Printf("当前已安装：%s（%s）\n", prior.Version, prior.Language)
	statuses, err := verifyInstall(game, nil)
	if err != nil {
		return err
	}
	printStatusTable(statuses)
	fmt.Println("\n 1) 恢复原版")
	if len(choices) > 1 {
		fmt.Println(" 2) 切换到其他语言/版本")
	}
	fmt.Println(" 3) 退出")
	switch strings.TrimSpace(prompt("请选择")) {
	case "1":
		if !confirm("用原版备份覆盖当前已打补丁的文件吗？") {
			return errors.New("已取消")
		}
		result, err := restoreOriginal(game)
		if err != nil {
			return err
		}
		fmt.Printf("\n已恢复原版，共 %d 个文件。\n", len(result.Files))
		fmt.Println("需要补丁时再次运行本程序并安装即可，原版备份仍保留着。")
		return nil
	case "2":
		if len(choices) <= 1 {
			return errors.New("只有一个补丁载荷，无法切换")
		}
		fmt.Println("选择要切换到的语言/版本：")
		for index, choice := range choices {
			fmt.Printf("  %d) %s（%s）\n", index+1, choice.info.Version, choice.info.Language)
		}
		index, err := strconv.Atoi(strings.TrimSpace(prompt("请输入序号")))
		if err != nil || index < 1 || index > len(choices) {
			return errors.New("无效选择")
		}
		target := choices[index-1]
		switched, err := openPayload(target.path)
		if err != nil {
			return err
		}
		defer switched.close()
		if switched.info.Version == prior.Version && switched.info.Language == prior.Language {
			return errors.New("这已经是当前安装的版本")
		}
		if !confirm(fmt.Sprintf("安装 %s（%s）吗？", switched.info.Version, switched.info.Language)) {
			return errors.New("已取消")
		}
		result, err := installPatch(game, switched, progressReporter(false))
		if err != nil {
			return err
		}
		fmt.Printf("\n已切换到 %s（%s），共 %d 个文件。\n", result.Version, result.Language, len(result.Files))
		fmt.Printf("原版备份：%s\n", result.BackupDirectory)
		return nil
	default:
		return nil
	}
}

// resolvePayload 按显式路径或程序所在目录的 glob 取唯一载荷。
func resolvePayload(explicit string) (*payload, error) {
	if explicit != "" {
		return openPayload(explicit)
	}
	choices, problems := discoverPayloads()
	if len(choices) == 0 {
		if len(problems) > 0 {
			return nil, fmt.Errorf("没有可用的补丁载荷: %s", strings.Join(problems, "; "))
		}
		return nil, errors.New("没有找到补丁载荷：请把 farland-patch-*.zip 放到本程序所在目录，或用 --payload 指定")
	}
	if len(choices) > 1 {
		names := make([]string, 0, len(choices))
		for _, choice := range choices {
			names = append(names, fmt.Sprintf("%s（%s）", choice.info.Version, choice.info.Language))
		}
		return nil, fmt.Errorf("找到多个补丁载荷，请用 --payload 指定: %s", strings.Join(names, "、"))
	}
	return openPayload(choices[0].path)
}

// resolvePayloadOptional 允许没有载荷（恢复与安装后核对只需要安装记录）。
func resolvePayloadOptional(explicit string) (*payload, error) {
	if explicit != "" {
		return openPayload(explicit)
	}
	choices, _ := discoverPayloads()
	if len(choices) == 0 {
		return nil, nil
	}
	if len(choices) > 1 {
		return nil, errors.New("找到多个补丁载荷，请用 --payload 指定")
	}
	return openPayload(choices[0].path)
}

func payloadInfo(loaded *payload) *manifest {
	if loaded == nil {
		return nil
	}
	return loaded.info
}

func payloadSummary(loaded *payload) any {
	if loaded == nil {
		return nil
	}
	return map[string]any{
		"path": loaded.path, "version": loaded.info.Version,
		"language": loaded.info.Language, "files": len(loaded.info.Files),
	}
}

// discoverPayloads 在程序所在目录和当前目录查找 farland-patch-*.zip。
func discoverPayloads() ([]payloadChoice, []string) {
	seen := map[string]bool{}
	var choices []payloadChoice
	var problems []string
	directories := []string{executableDir(), "."}
	for _, directory := range directories {
		matches, err := filepath.Glob(filepath.Join(directory, "farland-patch-*.zip"))
		if err != nil {
			continue
		}
		for _, match := range matches {
			absolute, err := filepath.Abs(match)
			if err != nil {
				continue
			}
			if seen[absolute] {
				continue
			}
			seen[absolute] = true
			loaded, err := openPayload(absolute)
			if err != nil {
				problems = append(problems, fmt.Sprintf("%s: %v", absolute, err))
				continue
			}
			choices = append(choices, payloadChoice{path: absolute, info: loaded.info})
			loaded.close()
		}
	}
	sort.Slice(choices, func(i, j int) bool {
		if choices[i].info.Language != choices[j].info.Language {
			return choices[i].info.Language < choices[j].info.Language
		}
		return choices[i].info.Version < choices[j].info.Version
	})
	return choices, problems
}

func executableDir() string {
	executable, err := os.Executable()
	if err != nil {
		return "."
	}
	return filepath.Dir(executable)
}

// resolveGame 依次尝试显式参数、环境变量、上次记住的目录，最后交互输入。
func resolveGame(explicit string, info *manifest, allowPrompt bool) (string, error) {
	if explicit != "" {
		return validateGameDir(explicit, info)
	}
	if env := os.Getenv(gameEnvVar); env != "" {
		game, err := validateGameDir(env, info)
		if err == nil {
			return game, nil
		}
		if !allowPrompt {
			return "", err
		}
		fmt.Printf("警告：%s 不可用（%v）\n", gameEnvVar, err)
	}
	if saved := loadGameConfig(); saved != "" {
		if game, err := validateGameDir(saved, info); err == nil {
			return game, nil
		}
	}
	if !allowPrompt {
		return "", errors.New("未指定游戏目录：用 --game 或环境变量 " + gameEnvVar + " 指定")
	}
	for attempt := 0; attempt < 5; attempt++ {
		answer := prompt("游戏安装目录（Steam 库中的 FARLAND SAGA II Saturn Tribute）")
		if strings.TrimSpace(answer) == "" {
			return "", errors.New("未选择游戏目录")
		}
		game, err := validateGameDir(answer, info)
		if err == nil {
			saveGameConfig(game)
			return game, nil
		}
		fmt.Printf("  %v\n", err)
	}
	return "", errors.New("多次尝试后仍未找到有效的游戏目录")
}

// validateGameDir 确认目录存在、可写，并且在给定载荷下包含全部目标文件。
func validateGameDir(directory string, info *manifest) (string, error) {
	if strings.TrimSpace(directory) == "" {
		return "", errors.New("游戏目录为空")
	}
	absolute, err := filepath.Abs(directory)
	if err != nil {
		return "", err
	}
	stat, err := os.Stat(absolute)
	if err != nil {
		return "", fmt.Errorf("游戏目录不存在: %s", absolute)
	}
	if !stat.IsDir() {
		return "", fmt.Errorf("不是目录: %s", absolute)
	}
	probe, err := os.CreateTemp(absolute, ".FarlandSaga2Patcher-write-")
	if err != nil {
		return "", fmt.Errorf("游戏目录不可写: %s", absolute)
	}
	probe.Close()
	os.Remove(probe.Name())
	if info != nil {
		var missing []string
		for _, entry := range info.Files {
			if _, err := os.Stat(gameFilePath(absolute, entry.Path)); err != nil {
				missing = append(missing, entry.Path)
			}
		}
		if len(missing) > 0 {
			return "", fmt.Errorf("%s 不像游戏安装目录，缺少: %s", absolute, strings.Join(missing, "、"))
		}
	}
	return absolute, nil
}

type gameConfig struct {
	Game string `json:"game"`
}

func configPath() string {
	if directory, err := os.UserConfigDir(); err == nil {
		directory = filepath.Join(directory, "FarlandSaga2Patcher")
		if err := os.MkdirAll(directory, 0o755); err == nil {
			return filepath.Join(directory, "config.json")
		}
	}
	return filepath.Join(executableDir(), "FarlandSaga2Patcher-config.json")
}

func loadGameConfig() string {
	data, err := os.ReadFile(configPath())
	if err != nil {
		return ""
	}
	var config gameConfig
	if err := json.Unmarshal(data, &config); err != nil {
		return ""
	}
	return config.Game
}

func saveGameConfig(game string) {
	data, err := json.MarshalIndent(gameConfig{Game: game}, "", "  ")
	if err != nil {
		return
	}
	os.WriteFile(configPath(), append(data, '\n'), 0o644)
}

func interactive() bool {
	info, err := os.Stdin.Stat()
	if err != nil {
		return false
	}
	return info.Mode()&os.ModeCharDevice != 0
}

// terminalOutput 判断标准输出是否直接连到终端；重定向到文件或管道时不用回车刷新。
func terminalOutput() bool {
	info, err := os.Stdout.Stat()
	if err != nil {
		return false
	}
	return info.Mode()&os.ModeCharDevice != 0
}

func prompt(label string) string {
	fmt.Printf("%s: ", label)
	reader := bufio.NewReader(os.Stdin)
	line, err := reader.ReadString('\n')
	if err != nil && line == "" {
		return ""
	}
	return strings.TrimSpace(line)
}

func confirm(question string) bool {
	answer := prompt(question + " [y/N]")
	return strings.EqualFold(answer, "y") || strings.EqualFold(answer, "yes")
}

func printJSON(value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	fmt.Println(string(data))
	return nil
}

// summarizeStates 把文件状态汇总成一行结论，供界面状态行与命令行共用。
func summarizeStates(statuses []fileStatus) string {
	patched, original, other := 0, 0, 0
	for _, status := range statuses {
		switch status.State {
		case statePatched:
			patched++
		case stateOriginal:
			original++
		default:
			other++
		}
	}
	switch {
	case other > 0:
		return "游戏文件状态不一致，请查看文件清单"
	case patched > 0 && original > 0:
		return "部分文件已是补丁状态"
	case patched > 0:
		return "已是补丁状态，但安装记录缺失"
	default:
		return "原版，尚未安装补丁"
	}
}

// samePath 判断两个路径是否指向同一文件；Windows 路径大小写不敏感，
// 拖放与浏览得到的路径可能和程序自己解析出来的写法不同。
func samePath(left, right string) bool {
	if strings.EqualFold(left, right) {
		return true
	}
	leftAbsolute, err := filepath.Abs(left)
	if err != nil {
		return false
	}
	rightAbsolute, err := filepath.Abs(right)
	if err != nil {
		return false
	}
	return strings.EqualFold(filepath.Clean(leftAbsolute), filepath.Clean(rightAbsolute))
}

func printStatusTable(statuses []fileStatus) {
	fmt.Printf("  %-44s %-9s %-12s\n", "文件", "状态", "当前大小")
	for _, status := range statuses {
		size := "-"
		if status.CurrentSize > 0 {
			size = formatSize(status.CurrentSize)
		}
		fmt.Printf("  %-44s %-9s %-12s\n", status.Path, status.State, size)
	}
}

func formatSize(size int64) string {
	switch {
	case size < 1024:
		return fmt.Sprintf("%d B", size)
	case size < 1048576:
		return fmt.Sprintf("%.1f KiB", float64(size)/1024)
	case size < 1073741824:
		return fmt.Sprintf("%.2f MiB", float64(size)/1048576)
	default:
		return fmt.Sprintf("%.2f GiB", float64(size)/1073741824)
	}
}

// progressReporter 在终端里用回车刷新当前文件的写入百分比；输出被重定向时
// 改为每个文件完成后打印一行，--json 时完全静默。
func progressReporter(asJSON bool) func(fileEntry, int64) {
	if asJSON {
		return nil
	}
	if !terminalOutput() {
		done := map[string]bool{}
		return func(entry fileEntry, position int64) {
			if position >= entry.PatchedSize && !done[entry.Path] {
				done[entry.Path] = true
				fmt.Printf("  已写入 %s（%s）\n", entry.Path, formatSize(entry.PatchedSize))
			}
		}
	}
	var current string
	var open bool
	lastPercent := -1
	return func(entry fileEntry, position int64) {
		if entry.Path != current {
			if open {
				fmt.Print("\n")
			}
			current, open, lastPercent = entry.Path, false, -1
		}
		if entry.PatchedSize <= 0 {
			return
		}
		percent := int(position * 100 / entry.PatchedSize)
		if percent == lastPercent || (position < entry.PatchedSize && percent%5 != 0) {
			return
		}
		lastPercent = percent
		line := fmt.Sprintf("  正在写入 %s %3d%%", entry.Path, percent)
		padding := 78 - displayWidth(line)
		if padding < 0 {
			padding = 0
		}
		fmt.Printf("\r%s%*s", line, padding, "")
		open = true
		if position >= entry.PatchedSize {
			fmt.Print("\n")
			open = false
		}
	}
}

// displayWidth 估算字符串在等宽终端占用的列数，中日韩表意文字按两列计，
// 这样回车刷新时能把上一行残留的内容擦干净。
func displayWidth(value string) int {
	width := 0
	for _, char := range value {
		switch {
		case char >= 0x1100 && char <= 0x115F,
			char >= 0x2E80 && char <= 0xA4CF,
			char >= 0xAC00 && char <= 0xD7A3,
			char >= 0xF900 && char <= 0xFAFF,
			char >= 0xFE30 && char <= 0xFE6F,
			char >= 0xFF00 && char <= 0xFF60,
			char >= 0xFFE0 && char <= 0xFFE6,
			char >= 0x20000 && char <= 0x3FFFD:
			width += 2
		default:
			width++
		}
	}
	return width
}

func printUsage() {
	fmt.Println(`FARLAND SAGA II Saturn Tribute 三语本地化补丁器

用法：
  FarlandSaga2Patcher.exe                      图形界面：先选语言，再安装或还原
  FarlandSaga2Patcher.exe wizard               控制台向导：安装或恢复
  FarlandSaga2Patcher.exe install  [选项]     安装补丁
  FarlandSaga2Patcher.exe restore [选项]     恢复原版
  FarlandSaga2Patcher.exe status  [选项]     查看当前状态
  FarlandSaga2Patcher.exe verify  [选项]     核对并返回退出码

选项：
  --game <目录>     游戏安装目录（也可用环境变量 ` + gameEnvVar + `）
  --payload <文件>  补丁载荷 ZIP（默认取本程序所在目录的 farland-patch-*.zip）
  --yes             跳过交互确认
  --json            输出 JSON
  -h, --help        显示本帮助

图形界面把简体中文、한국어、English 三个补丁内置在程序里：把本程序放进游戏根目录
（含 FarlandSaga2.exe 的文件夹）双击运行，点语言按钮安装，点「还原」恢复。
界面语言跟随所选语言，第一屏的语言按钮各显示自己的名字。
命令行用法面向脚本与排查，可另配 --payload 指定外部补丁包。

安装前请先退出游戏。补丁只修改载荷 manifest 中列出的文件，原版会备份到
游戏目录的 FarlandSaga2Patcher-backup 目录，随时可以恢复；这个目录只有一个，
不随安装次数增加，请勿删除。`)
}
