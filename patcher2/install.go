// 安装、恢复与状态核对。
//
// 写入策略与 tools/install_alpha.py 一致：先核对每个目标文件的身份，再备份
// 当前状态，补丁文件先写临时文件并校验哈希后才原子改名，任何一步失败都从
// 备份回滚。差异对象始终相对「原版」回放，因此切换语言时以原版备份为基准，
// 而不是游戏目录里可能已被另一版补丁改过的文件。
//
// 备份策略：整个安装生命周期只有一个原版备份目录 backupDirName，靠 manifest
// 的原版哈希判定是否可用，从不按时间戳复制。三个语言的补丁从同一份原版派生，
// original_sha256 一致，所以固定目录对任何语言都有效，切语言不需要重新备份。
// 旧版本每次安装都新建一个带时间戳的目录并全量拷贝——这个游戏一份是 379 MB，
// 反复切换语言能把游戏目录撑大好几倍，而那些目录除了第一次那份之外没有任何
// 代码会读。安装成功后统一清扫这些残留。
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const (
	recordFormat    = 1
	recordKind      = "farland-saga-2-patcher-install"
	recordName      = "FarlandSaga2Patcher-install.json"
	backupDirName   = "FarlandSaga2Patcher-backup"
	rollbackDirName = "FarlandSaga2Patcher-rollback"
	// stalePrefix 与 rollbackPrefix 是旧版本留下的带时间戳目录，安装成功后
	// 统一清扫。固定名 backupDirName 没有尾横线，匹配不到这两个前缀。
	stalePrefix     = "FarlandSaga2Patcher-backup-"
	rollbackPrefix  = "FarlandSaga2Patcher-restore-rollback-"
	tempSuffix      = ".FarlandSaga2Patcher-tmp"
	stampLayout     = time.RFC3339
	stateOriginal   = "original"
	statePatched    = "patched"
	statePrevious   = "previous"
	stateMissing    = "missing"
	stateMismatch   = "mismatch"
	statusInstalled = "installed"
	statusRestored  = "restored"
)

type fileRecord struct {
	OriginalSHA256 string `json:"original_sha256"`
	PatchedSHA256  string `json:"patched_sha256"`
	Size           int64  `json:"size"`
}

type installRecord struct {
	Format       int    `json:"format"`
	Kind         string `json:"kind"`
	Version      string `json:"version"`
	Language     string `json:"language"`
	InstalledUTC string `json:"installed_utc"`
	// BackupDirectory 恒为 backupDirName。旧版本写入的是带时间戳的目录名，
	// loadRecord 照旧接受，安装时会迁移成固定名。
	BackupDirectory string                `json:"backup_directory"`
	Files           map[string]fileRecord `json:"files"`
	Status          string                `json:"status"`
	RestoredUTC     string                `json:"restored_utc,omitempty"`
}

type fileStatus struct {
	Path          string `json:"path"`
	State         string `json:"state"`
	OriginalSize  int64  `json:"original_size"`
	PatchedSize   int64  `json:"patched_size"`
	CurrentSize   int64  `json:"current_size,omitempty"`
	CurrentSHA256 string `json:"current_sha256,omitempty"`
	Error         string `json:"error,omitempty"`
}

type installResult struct {
	AlreadyInstalled bool   `json:"already_installed"`
	Version          string `json:"version"`
	Language         string `json:"language"`
	BackupDirectory  string `json:"backup_directory"`
	// CleanedUp 与 ReclaimedBytes 记录这次安装清扫掉的旧式目录，供命令行告知
	// 用户腾出了多少空间。GUI 不展示，JSON 里留着便于排查。
	CleanedUp      []string       `json:"cleaned_up,omitempty"`
	ReclaimedBytes int64          `json:"reclaimed_bytes,omitempty"`
	Files          []fileStatus   `json:"files"`
	Record         *installRecord `json:"record"`
}

type restoreResult struct {
	Version         string       `json:"version"`
	Language        string       `json:"language"`
	BackupDirectory string       `json:"backup_directory"`
	Files           []fileStatus `json:"files"`
}

func digestFile(path string) (string, int64, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer file.Close()
	hash := sha256.New()
	size, err := io.Copy(hash, file)
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(hash.Sum(nil)), size, nil
}

func copyFile(source, target string) error {
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	in, err := os.Open(source)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.Create(target)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// backupTarget 是一份原版文件的期望身份：相对路径加原版哈希。
type backupTarget struct {
	path   string
	sha256 string
}

func backupTargetsFromManifest(info *manifest) []backupTarget {
	targets := make([]backupTarget, 0, len(info.Files))
	for _, entry := range info.Files {
		targets = append(targets, backupTarget{path: entry.Path, sha256: entry.OriginalSHA256})
	}
	return targets
}

func backupTargetsFromRecord(record *installRecord) []backupTarget {
	targets := make([]backupTarget, 0, len(record.Files))
	for name, item := range record.Files {
		targets = append(targets, backupTarget{path: name, sha256: item.OriginalSHA256})
	}
	sort.Slice(targets, func(i, j int) bool { return targets[i].path < targets[j].path })
	return targets
}

// verifyBackup 核对备份目录是否完整持有期望的原版文件。返回空切片表示完整，
// 否则逐条说明缺哪个文件、哪个内容不对，调用方直接拼进错误信息或状态输出。
// 哈希一致即证明这份备份对任何语言都可用——三个语言的补丁从同一份原版派生，
// original_sha256 相同，所以固定目录不需要按语言各存一份。
func verifyBackup(game, directory string, targets []backupTarget) []string {
	var problems []string
	for _, target := range targets {
		hash, _, err := digestFile(gameFilePath(gameFilePath(game, directory), target.path))
		switch {
		case os.IsNotExist(err):
			problems = append(problems, fmt.Sprintf("  %s  缺失", target.path))
		case err != nil:
			problems = append(problems, fmt.Sprintf("  %s  无法读取（%v）", target.path, err))
		case hash != target.sha256:
			problems = append(problems, fmt.Sprintf("  %s  内容不是原版", target.path))
		}
	}
	return problems
}

// effectiveBackupName 返回当前实际可用的备份目录名：固定目录完整就用固定名，
// 否则沿用记录里的名字——老安装的记录可能指向旧版的时间戳目录。
func effectiveBackupName(game string, targets []backupTarget, recorded string) string {
	if len(verifyBackup(game, backupDirName, targets)) == 0 {
		return backupDirName
	}
	return recorded
}

// allOriginal 判断是否所有目标文件都还是原版。只有这时候才能用游戏里的当前
// 文件重建备份——补丁态文件反推不出原版。
func allOriginal(statuses []fileStatus) bool {
	for _, status := range statuses {
		if status.State != stateOriginal {
			return false
		}
	}
	return true
}

// backupUnusableError 拼出「拿不到原版」的完整说明。这种情况下无法从补丁态
// 反推原版，唯一可靠的取回途径是让 Steam 校验一遍安装文件，所以直接把这条
// 路写给用户，而不是只报一个目录名让他自己去猜。
func backupUnusableError(game string, targets []backupTarget, prior *installRecord, fixedProblems []string) error {
	var parts []string
	if _, err := os.Stat(filepath.Join(game, backupDirName)); err == nil {
		parts = append(parts, backupDirName+" 里的原版文件不完整:\n"+strings.Join(fixedProblems, "\n"))
	} else {
		parts = append(parts, "找不到原版备份目录 "+backupDirName)
	}
	if prior != nil && prior.BackupDirectory != "" && prior.BackupDirectory != backupDirName {
		// 能走到这里，说明 resolveBackup 已经试过这个目录并且没通过核对。
		parts = append(parts, "安装记录里的 "+prior.BackupDirectory+" 同样不可用")
	}
	parts = append(parts, "当前游戏文件不是原版，无法从中还原出原版文件。")
	parts = append(parts, "请在 Steam 中「属性 → 已安装文件 → 验证游戏文件的完整性」恢复原版文件后重试；"+
		"验证会还原本补丁涉及的全部文件，之后重新安装即可重建 "+backupDirName+"。")
	return errors.New(strings.Join(parts, "\n"))
}

// resolveBackup 确定这次安装使用的原版备份目录，返回相对游戏目录的单段名称。
// 依次尝试：固定目录完整就直接沿用（一个字节都不拷）；记录里的旧式时间戳目录
// 完整就原地改名成固定名，同卷 rename 瞬间完成；游戏文件恰好全是原版就从当前
// 文件重建。三条路都走不通才报错。
func resolveBackup(game string, info *manifest, statuses []fileStatus, prior *installRecord) (string, error) {
	targets := backupTargetsFromManifest(info)
	fixedProblems := verifyBackup(game, backupDirName, targets)
	if len(fixedProblems) == 0 {
		return backupDirName, nil
	}
	if prior != nil && prior.BackupDirectory != "" && prior.BackupDirectory != backupDirName {
		if len(verifyBackup(game, prior.BackupDirectory, targets)) == 0 {
			old := gameFilePath(game, prior.BackupDirectory)
			if err := os.Rename(old, filepath.Join(game, backupDirName)); err != nil {
				return "", fmt.Errorf("迁移原版备份 %s 到 %s 失败: %w", prior.BackupDirectory, backupDirName, err)
			}
			return backupDirName, nil
		}
	}
	if allOriginal(statuses) {
		fixed := filepath.Join(game, backupDirName)
		if err := os.MkdirAll(fixed, 0o755); err != nil {
			return "", err
		}
		for _, status := range statuses {
			if err := copyFile(gameFilePath(game, status.Path), gameFilePath(fixed, status.Path)); err != nil {
				return "", fmt.Errorf("重建原版备份 %s 失败: %w", status.Path, err)
			}
		}
		if problems := verifyBackup(game, backupDirName, targets); len(problems) > 0 {
			return "", fmt.Errorf("重建的原版备份仍不完整:\n%s", strings.Join(problems, "\n"))
		}
		return backupDirName, nil
	}
	return "", backupUnusableError(game, targets, prior, fixedProblems)
}

// backupHealth 描述固定备份目录的现状，供 status 在用户点还原之前就把问题
// 摆出来，而不是等还原失败才看到一个目录名。
type backupHealth struct {
	Directory string   `json:"directory"`
	Present   bool     `json:"present"`
	Complete  bool     `json:"complete"`
	Problems  []string `json:"problems,omitempty"`
}

// inspectBackup 核对固定备份目录。targets 由调用方从安装记录或 manifest 取，
// 两者都保证非空，因此 Present 为真时 Complete 一定经过真实的内容核对。
func inspectBackup(game string, targets []backupTarget) backupHealth {
	health := backupHealth{Directory: backupDirName}
	if _, err := os.Stat(filepath.Join(game, backupDirName)); err != nil {
		return health
	}
	health.Present = true
	health.Problems = verifyBackup(game, backupDirName, targets)
	health.Complete = len(health.Problems) == 0
	return health
}

// dirSize 统计目录下所有文件的字节数，用于报告清扫腾出了多少空间。
func dirSize(root string) (int64, error) {
	var total int64
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if !info.IsDir() {
			total += info.Size()
		}
		return nil
	})
	return total, err
}

// sweepStaleBackups 删除安装成功后残留的旧式目录：带时间戳的 backup、还原
// rollback，以及上次崩溃留下的临时回滚目录。只在安装成功、固定备份已确认完整
// 之后调用，所以它们必然可再生——固定目录持有原版，补丁态文件随时能从载荷重建。
// keep 是固定备份目录名，它匹配不到任何一个前缀，不会被误删。
func sweepStaleBackups(game, keep string) (removed []string, reclaimed int64, err error) {
	entries, err := os.ReadDir(game)
	if err != nil {
		return nil, 0, err
	}
	for _, entry := range entries {
		name := entry.Name()
		if !entry.IsDir() || name == keep {
			continue
		}
		if !strings.HasPrefix(name, stalePrefix) && !strings.HasPrefix(name, rollbackPrefix) && name != rollbackDirName {
			continue
		}
		path := filepath.Join(game, name)
		size, err := dirSize(path)
		if err != nil {
			return removed, reclaimed, err
		}
		if err := os.RemoveAll(path); err != nil {
			return removed, reclaimed, fmt.Errorf("清理残留目录 %s 失败: %w", name, err)
		}
		removed = append(removed, name)
		reclaimed += size
	}
	return removed, reclaimed, nil
}

func gameFilePath(game, name string) string {
	return filepath.Join(game, filepath.FromSlash(name))
}

func recordPath(game string) string {
	return filepath.Join(game, recordName)
}

func loadRecord(game string) (*installRecord, error) {
	data, err := os.ReadFile(recordPath(game))
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var record installRecord
	if err := json.Unmarshal(data, &record); err != nil {
		return nil, fmt.Errorf("安装记录损坏: %w", err)
	}
	if record.Format != recordFormat || record.Kind != recordKind {
		return nil, errors.New("安装记录不是本程序写入的")
	}
	if len(record.Files) == 0 {
		return nil, errors.New("安装记录没有文件清单")
	}
	for name, item := range record.Files {
		if _, err := safeRelativePath(name); err != nil {
			return nil, fmt.Errorf("安装记录路径不合法: %s", name)
		}
		if !validSHA256(item.OriginalSHA256) || !validSHA256(item.PatchedSHA256) {
			return nil, fmt.Errorf("安装记录哈希不合法: %s", name)
		}
	}
	if directory := record.BackupDirectory; directory != "" {
		if _, err := safeRelativePath(directory); err != nil {
			return nil, fmt.Errorf("安装记录的备份目录不合法: %s", directory)
		}
	}
	return &record, nil
}

func writeRecord(game string, record *installRecord) error {
	data, err := json.MarshalIndent(record, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	temp := recordPath(game) + tempSuffix
	if err := os.WriteFile(temp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(temp, recordPath(game))
}

// inspectFile 把游戏里的当前文件与 manifest 身份对照；priorHashes 提供上一版
// 补丁的哈希，用于识别「已打补丁但属于另一版本」的状态。
func inspectFile(game string, entry fileEntry, priorHashes map[string]string) fileStatus {
	status := fileStatus{Path: entry.Path, OriginalSize: entry.OriginalSize, PatchedSize: entry.PatchedSize}
	target := gameFilePath(game, entry.Path)
	info, err := os.Stat(target)
	if err != nil {
		status.State = stateMissing
		if !os.IsNotExist(err) {
			status.Error = err.Error()
		}
		return status
	}
	if info.IsDir() {
		status.State = stateMismatch
		status.Error = "目标是目录"
		return status
	}
	status.CurrentSize = info.Size()
	hash, _, err := digestFile(target)
	if err != nil {
		status.State = stateMismatch
		status.Error = err.Error()
		return status
	}
	status.CurrentSHA256 = hash
	switch {
	case hash == entry.OriginalSHA256:
		status.State = stateOriginal
	case hash == entry.PatchedSHA256:
		status.State = statePatched
	default:
		if prior, ok := priorHashes[entry.Path]; ok && hash == prior {
			status.State = statePrevious
			break
		}
		status.State = stateMismatch
	}
	return status
}

func inspectAll(game string, info *manifest, prior *installRecord) []fileStatus {
	priorHashes := map[string]string{}
	if prior != nil {
		for name, item := range prior.Files {
			priorHashes[name] = item.PatchedSHA256
		}
	}
	statuses := make([]fileStatus, 0, len(info.Files))
	for _, entry := range info.Files {
		statuses = append(statuses, inspectFile(game, entry, priorHashes))
	}
	return statuses
}

func stateOf(statuses []fileStatus, path string) string {
	for _, status := range statuses {
		if status.Path == path {
			return status.State
		}
	}
	return stateMissing
}

func describeState(status fileStatus) string {
	switch status.State {
	case stateOriginal:
		return "原版"
	case statePatched:
		return "已打补丁"
	case statePrevious:
		return "其他版本补丁"
	case stateMissing:
		if status.Error != "" {
			return "无法读取（" + status.Error + "）"
		}
		return "缺失"
	default:
		return "已被修改"
	}
}

// blockedFiles 列出既不是原版、也不是本程序安装过的补丁的文件。这些文件
// 身份不明，安装前必须先由用户自行处理，否则回放出来的结果不可信。
func blockedFiles(statuses []fileStatus) []string {
	var blocked []string
	for _, status := range statuses {
		switch status.State {
		case stateOriginal, statePatched, statePrevious:
		default:
			blocked = append(blocked, "  "+status.Path+"  "+describeState(status))
		}
	}
	return blocked
}

// checkWritable 在写入前确认目标文件没有被占用（Windows 上运行中的游戏会锁住
// 镜像文件），并确认游戏目录可写。
func checkWritable(game string, info *manifest) error {
	probe, err := os.CreateTemp(game, ".FarlandSaga2Patcher-write-")
	if err != nil {
		return fmt.Errorf("游戏目录不可写: %w", err)
	}
	probe.Close()
	os.Remove(probe.Name())
	for _, entry := range info.Files {
		target := gameFilePath(game, entry.Path)
		file, err := os.OpenFile(target, os.O_RDWR, 0)
		if err != nil {
			if os.IsNotExist(err) {
				continue
			}
			return fmt.Errorf("%s 被其他程序占用（%v）；请先退出游戏", entry.Path, err)
		}
		file.Close()
	}
	return nil
}

// installPatch 执行一次安装：核对身份、备份、回放差异、原子改名、写安装记录，
// 任何失败都从刚建立的备份回滚已替换的文件。
func installPatch(game string, loaded *payload, progress func(fileEntry, int64)) (*installResult, error) {
	info := loaded.info
	prior, err := loadRecord(game)
	if err != nil {
		return nil, err
	}
	statuses := inspectAll(game, info, prior)
	if blocked := blockedFiles(statuses); len(blocked) > 0 {
		return nil, errors.New("以下文件既不是原版也不是本程序安装的补丁，已中止:\n" + strings.Join(blocked, "\n"))
	}
	if prior != nil && prior.Version == info.Version && prior.Language == info.Language {
		// 已安装的判定：每个文件都达到补丁后状态；补丁不改变的文件（原版与
		// 补丁后内容相同）保持原版也算达成，避免重复安装反复建备份。
		installed := true
		for index, status := range statuses {
			entry := info.Files[index]
			if status.State == statePatched {
				continue
			}
			if status.State == stateOriginal && entry.OriginalSHA256 == entry.PatchedSHA256 {
				continue
			}
			installed = false
			break
		}
		if installed {
			return &installResult{AlreadyInstalled: true, Version: info.Version, Language: info.Language,
				BackupDirectory: effectiveBackupName(game, backupTargetsFromManifest(info), prior.BackupDirectory),
				Files:           statuses, Record: prior}, nil
		}
	}
	if err := checkWritable(game, info); err != nil {
		return nil, err
	}

	// 先把原版备份定下来再改写任何文件。备份拿不到就必须在这里退出，否则一次
	// 失败的安装会在游戏目录里留下几百 MB 的孤儿目录——旧版本正是如此。
	backupName, err := resolveBackup(game, info, statuses, prior)
	if err != nil {
		return nil, err
	}
	originalBackup := gameFilePath(game, backupName)

	// 回滚副本只保存「不是原版」的文件：原版文件回滚时直接从固定备份取回，
	// 不必再拷一份。固定名字 + 收尾清扫，所以不会像旧的时间戳目录那样每次
	// 安装都堆一个几百 MB 的残留；只有切换语言这种「当前不是原版」的场景才
	// 会真的写盘。
	rollbackDir := filepath.Join(game, rollbackDirName)
	preserve := func() error {
		var kept []string
		for _, status := range statuses {
			if status.State == stateOriginal || status.State == stateMissing {
				continue
			}
			kept = append(kept, status.Path)
		}
		if len(kept) == 0 {
			return nil
		}
		if err := os.MkdirAll(rollbackDir, 0o755); err != nil {
			return err
		}
		for _, name := range kept {
			if err := copyFile(gameFilePath(game, name), gameFilePath(rollbackDir, name)); err != nil {
				return fmt.Errorf("保存回滚副本 %s 失败: %w", name, err)
			}
		}
		return nil
	}
	if err := preserve(); err != nil {
		os.RemoveAll(rollbackDir)
		return nil, err
	}

	var replaced []string
	var temporaries []string
	rollback := func(cause error) error {
		for index := len(replaced) - 1; index >= 0; index-- {
			name := replaced[index]
			// 安装前是原版的，从固定备份取回；原本就是别的语言补丁的，取回滚
			// 副本，把游戏完整还原到这次安装之前的状态。
			source := gameFilePath(originalBackup, name)
			if stateOf(statuses, name) != stateOriginal {
				source = gameFilePath(rollbackDir, name)
			}
			if err := copyFile(source, gameFilePath(game, name)); err != nil {
				return fmt.Errorf("回滚 %s 失败: %w（原始错误: %v）", name, err, cause)
			}
		}
		for _, temporary := range temporaries {
			os.Remove(temporary)
		}
		// 回滚副本是这次安装的私有暂存，用完必须消失；留着就会像旧版那样每次
		// 失败都堆一个几百 MB 的目录。删不掉也没关系，下次安装成功会清掉。
		os.RemoveAll(rollbackDir)
		return cause
	}
	for _, entry := range info.Files {
		if stateOf(statuses, entry.Path) == statePatched {
			continue
		}
		target := gameFilePath(game, entry.Path)
		temporary := target + tempSuffix
		temporaries = append(temporaries, temporary)
		out, err := os.Create(temporary)
		if err != nil {
			return nil, rollback(fmt.Errorf("无法写入 %s: %w", entry.Path, err))
		}
		// 差异对象相对原版回放，基准取原版备份而不是游戏目录里的当前文件。
		replayErr := loaded.replay(entry, gameFilePath(originalBackup, entry.Path), out, func(position int64) {
			if progress != nil {
				progress(entry, position)
			}
		})
		closeErr := out.Close()
		if replayErr != nil {
			return nil, rollback(fmt.Errorf("重建 %s 失败: %w", entry.Path, replayErr))
		}
		if closeErr != nil {
			return nil, rollback(fmt.Errorf("写入 %s 失败: %w", entry.Path, closeErr))
		}
		if hash, size, err := digestFile(temporary); err != nil || hash != entry.PatchedSHA256 || size != entry.PatchedSize {
			return nil, rollback(fmt.Errorf("补丁文件校验失败: %s", entry.Path))
		}
		if err := os.Rename(temporary, target); err != nil {
			return nil, rollback(fmt.Errorf("替换 %s 失败: %w", entry.Path, err))
		}
		replaced = append(replaced, entry.Path)
		if hash, _, err := digestFile(target); err != nil || hash != entry.PatchedSHA256 {
			return nil, rollback(fmt.Errorf("安装后校验失败: %s", entry.Path))
		}
	}

	record := &installRecord{
		Format: recordFormat, Kind: recordKind,
		Version: info.Version, Language: info.Language,
		InstalledUTC: time.Now().UTC().Format(stampLayout),
		Files:        map[string]fileRecord{},
		Status:       statusInstalled,
	}
	for _, entry := range info.Files {
		record.Files[entry.Path] = fileRecord{OriginalSHA256: entry.OriginalSHA256,
			PatchedSHA256: entry.PatchedSHA256, Size: entry.PatchedSize}
	}
	// backupName 已经是校验过的单段相对名，直接记进记录。
	record.BackupDirectory = backupName
	if err := writeRecord(game, record); err != nil {
		return nil, rollback(fmt.Errorf("写入安装记录失败: %w", err))
	}
	// 记录落盘、固定备份已确认完整，此刻旧式目录都是可再生垃圾，一并清掉。
	// 回滚副本也在这里收尾，正常情况下上面已经用不上了。
	cleaned, reclaimed, err := sweepStaleBackups(game, backupDirName)
	if err != nil {
		return nil, err
	}
	return &installResult{Version: info.Version, Language: info.Language, BackupDirectory: record.BackupDirectory,
		CleanedUp: cleaned, ReclaimedBytes: reclaimed, Files: statuses, Record: record}, nil
}

// restoreOriginal 用原版备份覆盖已安装的补丁文件；全部成功后才把安装记录标记
// 为已恢复。写入前逐文件核对哈希，只接受记录中的补丁后哈希或原版哈希，否则
// 拒绝；写入走临时文件加哈希校验再原子改名，失败时在内存里按已替换顺序回滚。
// 不落回滚目录：撤销一次还原等于重装一次，载荷内嵌在程序里，随时可用。
func restoreOriginal(game string) (*restoreResult, error) {
	record, err := loadRecord(game)
	if err != nil {
		return nil, err
	}
	if record == nil {
		return nil, fmt.Errorf("没有找到安装记录 %s，无法恢复", recordName)
	}
	if record.Status != statusInstalled {
		return nil, fmt.Errorf("安装记录状态为 %s，无需恢复", record.Status)
	}
	// 备份身份以安装记录为准，还原不需要载荷。优先用固定备份目录；老安装
	// 记录里可能是带时间戳的名字，照旧能用。
	targets := backupTargetsFromRecord(record)
	backupName := effectiveBackupName(game, targets, record.BackupDirectory)
	if problems := verifyBackup(game, backupName, targets); len(problems) > 0 {
		named := backupDirName
		if record.BackupDirectory != backupDirName {
			named += " 与 " + record.BackupDirectory
		}
		return nil, fmt.Errorf("原版备份不可用：%s 不完整:\n%s", named, strings.Join(problems, "\n"))
	}
	backup := gameFilePath(game, backupName)
	names := make([]string, 0, len(record.Files))
	for name := range record.Files {
		names = append(names, name)
	}
	sort.Strings(names)
	type item struct {
		name         string
		target       string
		original     string
		currentHash  string
		originalHash string
		originalSize int64
	}
	items := make([]item, 0, len(names))
	for _, name := range names {
		entry := record.Files[name]
		target := gameFilePath(game, name)
		original := gameFilePath(backup, name)
		originalHash, originalSize, err := digestFile(original)
		if err != nil || originalHash != entry.OriginalSHA256 {
			return nil, fmt.Errorf("原版备份校验失败: %s", name)
		}
		currentHash, _, err := digestFile(target)
		if err != nil {
			return nil, fmt.Errorf("无法读取当前文件 %s: %v", name, err)
		}
		if currentHash != entry.PatchedSHA256 && currentHash != entry.OriginalSHA256 {
			return nil, fmt.Errorf("%s 在安装后已被修改，请先备份再恢复", name)
		}
		items = append(items, item{name: name, target: target, original: original,
			currentHash: currentHash, originalHash: entry.OriginalSHA256, originalSize: originalSize})
	}
	statuses := make([]fileStatus, 0, len(items))
	var replaced []string
	failure := func(cause error) error {
		for index := len(replaced) - 1; index >= 0; index-- {
			name := replaced[index]
			// 只回滚「安装前就是补丁态」的文件；原本已是原版的，还原对它是
			// 无操作，谈不上回滚。补丁态取不回来，但记录尚未改写，重装一次
			// 即可完全恢复，用户不会卡在中间态。
			if err := copyFile(gameFilePath(backup, name), gameFilePath(game, name)); err != nil {
				return fmt.Errorf("回滚 %s 失败: %w（原始错误: %v）", name, err, cause)
			}
		}
		return cause
	}
	for _, entry := range items {
		temporary := entry.target + tempSuffix
		if err := copyFile(entry.original, temporary); err != nil {
			return nil, failure(fmt.Errorf("恢复 %s 失败: %v", entry.name, err))
		}
		if hash, _, err := digestFile(temporary); err != nil || hash != entry.originalHash {
			os.Remove(temporary)
			return nil, failure(fmt.Errorf("恢复 %s 的副本校验失败", entry.name))
		}
		if err := os.Rename(temporary, entry.target); err != nil {
			os.Remove(temporary)
			return nil, failure(fmt.Errorf("恢复 %s 失败: %v", entry.name, err))
		}
		replaced = append(replaced, entry.name)
		if hash, _, err := digestFile(entry.target); err != nil || hash != entry.originalHash {
			return nil, failure(fmt.Errorf("恢复后校验失败: %s", entry.name))
		}
		statuses = append(statuses, fileStatus{Path: entry.name, State: stateOriginal,
			OriginalSize: entry.originalSize, PatchedSize: record.Files[entry.name].Size})
	}
	record.Status = statusRestored
	record.RestoredUTC = time.Now().UTC().Format(stampLayout)
	if err := writeRecord(game, record); err != nil {
		return nil, fmt.Errorf("更新安装记录失败: %w", err)
	}
	return &restoreResult{Version: record.Version, Language: record.Language,
		BackupDirectory: backupName, Files: statuses}, nil
}

// verifyInstall 核对游戏现状：有安装记录时对照补丁后哈希，否则对照载荷的原版哈希。
func verifyInstall(game string, loaded *payload) ([]fileStatus, error) {
	record, err := loadRecord(game)
	if err != nil {
		return nil, err
	}
	if loaded == nil {
		if record == nil {
			return nil, fmt.Errorf("没有找到安装记录 %s", recordName)
		}
		names := make([]string, 0, len(record.Files))
		for name := range record.Files {
			names = append(names, name)
		}
		sort.Strings(names)
		statuses := make([]fileStatus, 0, len(names))
		for _, name := range names {
			entry := record.Files[name]
			status := fileStatus{Path: name, OriginalSize: -1, PatchedSize: entry.Size}
			hash, size, err := digestFile(gameFilePath(game, name))
			if err != nil {
				status.State = stateMissing
				status.Error = err.Error()
				statuses = append(statuses, status)
				continue
			}
			status.CurrentSize = size
			status.CurrentSHA256 = hash
			if hash == entry.PatchedSHA256 {
				status.State = statePatched
			} else if hash == entry.OriginalSHA256 {
				status.State = stateOriginal
			} else {
				status.State = stateMismatch
			}
			statuses = append(statuses, status)
		}
		return statuses, nil
	}
	return inspectAll(game, loaded.info, record), nil
}
