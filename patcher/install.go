// 安装、恢复与状态核对。
//
// 写入策略与 tools/install_alpha.py 一致：先核对每个目标文件的身份，再备份
// 当前状态，补丁文件先写临时文件并校验哈希后才原子改名，任何一步失败都从
// 备份回滚。差异对象始终相对「原版」回放，因此切换语言时以原版备份为基准，
// 而不是游戏目录里可能已被另一版补丁改过的文件。
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
	recordKind      = "farland-saga-patcher-install"
	recordName      = "FarlandSagaPatcher-install.json"
	backupPrefix    = "FarlandSagaPatcher-backup-"
	rollbackPrefix  = "FarlandSagaPatcher-restore-rollback-"
	tempSuffix      = ".FarlandSagaPatcher-tmp"
	timeLayout      = "20060102-150405"
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
	Format          int                   `json:"format"`
	Kind            string                `json:"kind"`
	Version         string                `json:"version"`
	Language        string                `json:"language"`
	InstalledUTC    string                `json:"installed_utc"`
	BackupDirectory string                `json:"backup_directory"`
	PreviousBackup  string                `json:"previous_release_backup,omitempty"`
	Files           map[string]fileRecord `json:"files"`
	Status          string                `json:"status"`
	RestoredUTC     string                `json:"restored_utc,omitempty"`
	RestoreRollback string                `json:"restore_rollback_directory,omitempty"`
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
	AlreadyInstalled bool           `json:"already_installed"`
	Version          string         `json:"version"`
	Language         string         `json:"language"`
	BackupDirectory  string         `json:"backup_directory"`
	PreviousBackup   string         `json:"previous_release_backup,omitempty"`
	Files            []fileStatus   `json:"files"`
	Record           *installRecord `json:"record"`
}

type restoreResult struct {
	Version         string       `json:"version"`
	Language        string       `json:"language"`
	BackupDirectory string       `json:"backup_directory"`
	Rollback        string       `json:"restore_rollback_directory"`
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

// uniqueDir 取一个不存在的目录名；同一秒内重复安装时追加序号。
func uniqueDir(parent, prefix string) (string, error) {
	base := filepath.Join(parent, prefix+time.Now().UTC().Format(timeLayout))
	candidate := base
	for index := 2; ; index++ {
		if _, err := os.Stat(candidate); err != nil {
			if os.IsNotExist(err) {
				return candidate, nil
			}
			return "", err
		}
		candidate = fmt.Sprintf("%s-%d", base, index)
	}
}

func gameFilePath(game, name string) string {
	return filepath.Join(game, filepath.FromSlash(name))
}

// relativeName 把目录名折算成游戏内的单段相对名，供安装记录安全地保存。
func relativeName(game, directory string) (string, error) {
	relative, err := filepath.Rel(game, directory)
	if err != nil {
		return "", err
	}
	name := filepath.ToSlash(relative)
	if name == "." || strings.ContainsAny(name, `\:`) || strings.HasPrefix(name, "/") {
		return "", fmt.Errorf("备份目录必须位于游戏目录内: %s", directory)
	}
	for _, part := range strings.Split(name, "/") {
		if part == "" || part == "." || part == ".." {
			return "", fmt.Errorf("备份目录必须位于游戏目录内: %s", directory)
		}
	}
	return name, nil
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
	for _, directory := range []string{record.BackupDirectory, record.PreviousBackup, record.RestoreRollback} {
		if directory == "" {
			continue
		}
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
	probe, err := os.CreateTemp(game, ".FarlandSagaPatcher-write-")
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
				BackupDirectory: prior.BackupDirectory, Files: statuses, Record: prior}, nil
		}
	}
	if err := checkWritable(game, info); err != nil {
		return nil, err
	}

	// 备份当前状态；原版备份优先沿用上一版安装记录的目录，缺失时从当前状态补齐。
	previousBackup, err := uniqueDir(game, backupPrefix)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(previousBackup, 0o755); err != nil {
		return nil, err
	}
	for _, status := range statuses {
		if status.State == stateMissing {
			continue
		}
		source := gameFilePath(game, status.Path)
		target := gameFilePath(previousBackup, status.Path)
		if err := copyFile(source, target); err != nil {
			return nil, fmt.Errorf("备份 %s 失败: %w", status.Path, err)
		}
		if hash, _, err := digestFile(target); err != nil || hash != status.CurrentSHA256 {
			return nil, fmt.Errorf("备份校验失败: %s", status.Path)
		}
	}
	originalBackup := previousBackup
	if prior != nil {
		originalBackup = gameFilePath(game, prior.BackupDirectory)
		if _, err := os.Stat(originalBackup); err != nil {
			return nil, fmt.Errorf("找不到上一版安装的原版备份 %s；请恢复该目录，或从干净安装重新打补丁", prior.BackupDirectory)
		}
	}
	for _, entry := range info.Files {
		target := gameFilePath(originalBackup, entry.Path)
		hash, _, err := digestFile(target)
		if err == nil && hash == entry.OriginalSHA256 {
			continue
		}
		if err != nil && !os.IsNotExist(err) {
			return nil, fmt.Errorf("原版备份读取失败: %s: %v", entry.Path, err)
		}
		// 首次安装时当前状态就是原版，用刚建立的备份补齐。
		if err := copyFile(gameFilePath(previousBackup, entry.Path), target); err != nil {
			return nil, fmt.Errorf("原版备份缺失且无法补齐: %s: %v", entry.Path, err)
		}
		if hash, _, err := digestFile(target); err != nil || hash != entry.OriginalSHA256 {
			return nil, fmt.Errorf("原版备份与补丁要求的原版不一致: %s", entry.Path)
		}
	}

	var replaced []string
	var temporaries []string
	rollback := func(cause error) error {
		for index := len(replaced) - 1; index >= 0; index-- {
			name := replaced[index]
			if err := copyFile(gameFilePath(previousBackup, name), gameFilePath(game, name)); err != nil {
				return fmt.Errorf("回滚 %s 失败: %w（原始错误: %v）", name, err, cause)
			}
		}
		for _, temporary := range temporaries {
			os.Remove(temporary)
		}
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
	backupName, err := relativeName(game, originalBackup)
	if err != nil {
		return nil, rollback(err)
	}
	record.BackupDirectory = backupName
	if originalBackup != previousBackup {
		name, err := relativeName(game, previousBackup)
		if err != nil {
			return nil, rollback(err)
		}
		record.PreviousBackup = name
	}
	if err := writeRecord(game, record); err != nil {
		return nil, rollback(fmt.Errorf("写入安装记录失败: %w", err))
	}
	return &installResult{Version: info.Version, Language: info.Language, BackupDirectory: record.BackupDirectory,
		PreviousBackup: record.PreviousBackup, Files: statuses, Record: record}, nil
}

// restoreOriginal 用原版备份覆盖已安装的补丁文件；当前文件先存入回滚目录，
// 全部成功后才把安装记录标记为已恢复。
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
	backup := gameFilePath(game, record.BackupDirectory)
	if _, err := os.Stat(backup); err != nil {
		return nil, fmt.Errorf("找不到原版备份 %s，无法恢复", record.BackupDirectory)
	}
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
	rollback, err := uniqueDir(game, rollbackPrefix)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(rollback, 0o755); err != nil {
		return nil, err
	}
	for _, entry := range items {
		if err := copyFile(entry.target, gameFilePath(rollback, entry.name)); err != nil {
			return nil, fmt.Errorf("保存回滚副本失败: %s: %v", entry.name, err)
		}
	}
	statuses := make([]fileStatus, 0, len(items))
	var replaced []string
	failure := func(cause error) error {
		for index := len(replaced) - 1; index >= 0; index-- {
			name := replaced[index]
			if err := copyFile(gameFilePath(rollback, name), gameFilePath(game, name)); err != nil {
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
	record.RestoreRollback, err = relativeName(game, rollback)
	if err != nil {
		return nil, err
	}
	if err := writeRecord(game, record); err != nil {
		return nil, fmt.Errorf("更新安装记录失败: %w", err)
	}
	return &restoreResult{Version: record.Version, Language: record.Language,
		BackupDirectory: record.BackupDirectory, Rollback: record.RestoreRollback, Files: statuses}, nil
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
