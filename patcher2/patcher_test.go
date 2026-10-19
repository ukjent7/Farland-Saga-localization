package main

import (
	"archive/zip"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strings"
	"testing"
)

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// pattern 生成确定性的测试数据，避免随机数让失败不可复现。
func pattern(seed byte, size int) []byte {
	data := make([]byte, size)
	for index := range data {
		data[index] = byte(index*31 + int(seed)*17 + index/blockSize)
	}
	return data
}

func sliceAt(data []byte, start, end int) []byte {
	if start > len(data) {
		start = len(data)
	}
	if end > len(data) {
		end = len(data)
	}
	return data[start:end]
}

// writeObject 按 version_storage.encode 的格式写一个差异对象：gzip 内一行
// JSON 头，随后是 <偏移, 长度> 记录与对应的 4 KiB 块。
func writeObject(t *testing.T, writer *bytes.Buffer, original, patched []byte, key string) {
	t.Helper()
	header := map[string]any{"format": 1, "sha256": key, "size": len(patched)}
	if original != nil {
		header["base"] = map[string]any{"path": "backups/original/example.bin",
			"sha256": sha256Hex(original), "size": len(original)}
	}
	line, err := json.Marshal(header)
	if err != nil {
		t.Fatal(err)
	}
	compressed := gzip.NewWriter(writer)
	if _, err := compressed.Write(line); err != nil {
		t.Fatal(err)
	}
	if _, err := compressed.Write([]byte("\n")); err != nil {
		t.Fatal(err)
	}
	for start := 0; start < len(patched); start += blockSize {
		chunk := sliceAt(patched, start, start+blockSize)
		if !bytes.Equal(chunk, sliceAt(original, start, start+blockSize)) {
			var record [recordSize]byte
			binary.LittleEndian.PutUint64(record[0:8], uint64(start))
			binary.LittleEndian.PutUint32(record[8:12], uint32(len(chunk)))
			if _, err := compressed.Write(record[:]); err != nil {
				t.Fatal(err)
			}
			if _, err := compressed.Write(chunk); err != nil {
				t.Fatal(err)
			}
		}
	}
	if err := compressed.Close(); err != nil {
		t.Fatal(err)
	}
}

func gzipParts(t *testing.T, parts ...[]byte) []byte {
	t.Helper()
	var buffer bytes.Buffer
	writer := gzip.NewWriter(&buffer)
	for _, part := range parts {
		if _, err := writer.Write(part); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return buffer.Bytes()
}

func recordBytes(offset int64, size uint32, data []byte) []byte {
	var record [recordSize]byte
	binary.LittleEndian.PutUint64(record[0:8], uint64(offset))
	binary.LittleEndian.PutUint32(record[8:12], size)
	return append(record[:], data...)
}

type testFile struct {
	name     string
	original []byte
	patched  []byte
}

func writePayloadZip(t *testing.T, path string, info manifest, objects map[string][]byte) string {
	t.Helper()
	archive, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	writer := zip.NewWriter(archive)
	for name, data := range objects {
		header := &zip.FileHeader{Name: name, Method: zip.Deflate}
		entry, err := writer.CreateHeader(header)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := entry.Write(data); err != nil {
			t.Fatal(err)
		}
	}
	data, err := json.MarshalIndent(info, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	manifestEntry, err := writer.Create(manifestName)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := manifestEntry.Write(data); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	return path
}

func buildPayload(t *testing.T, directory, version, language string, files []testFile) (string, []fileEntry) {
	t.Helper()
	entries := make([]fileEntry, 0, len(files))
	objects := map[string][]byte{}
	for _, file := range files {
		key := sha256Hex(file.patched)
		var buffer bytes.Buffer
		writeObject(t, &buffer, file.original, file.patched, key)
		objects[objectPrefix+key+".gz"] = buffer.Bytes()
		entries = append(entries, fileEntry{
			Path: file.name, OriginalSHA256: sha256Hex(file.original), OriginalSize: int64(len(file.original)),
			PatchedSHA256: key, PatchedSize: int64(len(file.patched)), Object: objectPrefix + key + ".gz",
		})
	}
	info := manifest{Format: payloadFormat, Kind: payloadKind, Version: version, Language: language,
		Game: "FARLAND SAGA II Saturn Tribute", CreatedUTC: "2026-10-14T00:00:00Z", Files: entries}
	path := filepath.Join(directory, fmt.Sprintf("farland-patch-%s.zip", version))
	return writePayloadZip(t, path, info, objects), entries
}

func buildGame(t *testing.T, files []testFile) string {
	t.Helper()
	game := t.TempDir()
	for _, file := range files {
		target := filepath.Join(game, filepath.FromSlash(file.name))
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(target, file.original, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return game
}

func openTestPayload(t *testing.T, path string) *payload {
	t.Helper()
	loaded, err := openPayload(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { loaded.close() })
	return loaded
}

// sampleFiles 造一组跨目录、跨尺寸变化的文件。
func sampleFiles() []testFile {
	original := pattern(1, 12*blockSize+123)
	patched := append([]byte(nil), original...)
	copy(patched[blockSize:2*blockSize], pattern(9, blockSize))
	copy(patched[5*blockSize:5*blockSize+17], pattern(3, 17))
	patched = append(patched, pattern(7, blockSize+50)...)
	return []testFile{
		{name: "FarlandSaga2.bin", original: original, patched: patched},
		{name: "S-Tribute_Data/level0", original: pattern(2, 5000), patched: pattern(4, 5000)},
		{name: "Texture.tex", original: pattern(5, 3000), patched: pattern(5, 3000)},
	}
}

func TestReplayRoundTrip(t *testing.T) {
	directory := t.TempDir()
	files := sampleFiles()
	path, entries := buildPayload(t, directory, "round", "zh-CN", files)
	loaded := openTestPayload(t, path)
	for index, entry := range entries {
		base := filepath.Join(directory, fmt.Sprintf("base-%d.bin", index))
		if err := os.WriteFile(base, files[index].original, 0o644); err != nil {
			t.Fatal(err)
		}
		var output bytes.Buffer
		if err := loaded.replay(entry, base, &output, nil); err != nil {
			t.Fatalf("%s 回放失败: %v", entry.Path, err)
		}
		if !bytes.Equal(output.Bytes(), files[index].patched) {
			t.Fatalf("%s 回放结果与补丁文件不一致", entry.Path)
		}
	}
}

func TestReplayShrinkAndFullObject(t *testing.T) {
	directory := t.TempDir()
	original := pattern(6, 8*blockSize)
	shrunk := original[:3*blockSize+77]
	full := pattern(8, 2*blockSize+9)
	path, entries := buildPayload(t, directory, "shrink", "zh-CN", []testFile{
		{name: "shrunk.bin", original: original, patched: shrunk},
		{name: "full.bin", original: nil, patched: full},
	})
	loaded := openTestPayload(t, path)
	base := filepath.Join(directory, "base.bin")
	if err := os.WriteFile(base, original, 0o644); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	if err := loaded.replay(entries[0], base, &output, nil); err != nil {
		t.Fatalf("缩小的文件回放失败: %v", err)
	}
	if !bytes.Equal(output.Bytes(), shrunk) {
		t.Fatal("缩小的文件回放结果不一致")
	}
	output.Reset()
	// 完整对象没有基准，基准路径不存在也不能报错。
	if err := loaded.replay(entries[1], filepath.Join(directory, "missing.bin"), &output, nil); err != nil {
		t.Fatalf("完整对象回放失败: %v", err)
	}
	if !bytes.Equal(output.Bytes(), full) {
		t.Fatal("完整对象回放结果不一致")
	}
}

func TestReplayRejectsHeaderWithoutNewline(t *testing.T) {
	directory := t.TempDir()
	original := pattern(16, 2*blockSize)
	patched := pattern(17, 2*blockSize)
	key := sha256Hex(patched)
	header := fmt.Sprintf(`{"format":1,"sha256":%q,"size":%d,"base":{"path":"b","sha256":%q,"size":%d}}`,
		key, len(patched), sha256Hex(original), len(original))
	info := manifest{Format: payloadFormat, Kind: payloadKind, Version: "nonewline", Language: "zh-CN",
		Files: []fileEntry{{Path: "x.bin", OriginalSHA256: sha256Hex(original), OriginalSize: int64(len(original)),
			PatchedSHA256: key, PatchedSize: int64(len(patched)), Object: objectPrefix + key + ".gz"}}}
	// 头部行没有换行结尾，也没有记录：readLine 到 EOF 也等不到换行。
	path := writePayloadZip(t, filepath.Join(directory, "nonewline.zip"), info,
		map[string][]byte{objectPrefix + key + ".gz": gzipParts(t, []byte(header))})
	loaded := openTestPayload(t, path)
	base := filepath.Join(directory, "base.bin")
	if err := os.WriteFile(base, original, 0o644); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	err := loaded.replay(info.Files[0], base, &output, nil)
	if err == nil {
		t.Fatal("头部缺少换行时应当拒绝")
	}
	if !strings.Contains(err.Error(), "缺少头部行") {
		t.Fatalf("错误信息不符: %v", err)
	}
}

func TestReplayRejectsCorruption(t *testing.T) {
	directory := t.TempDir()
	original := pattern(11, 4*blockSize)
	patched := append([]byte(nil), original...)
	copy(patched[blockSize:2*blockSize], pattern(12, blockSize))
	key := sha256Hex(patched)
	baseHeader := fmt.Sprintf(`{"format":1,"sha256":%q,"size":%d,"base":{"path":"backups/original/x.bin","sha256":%q,"size":%d}}`,
		key, len(patched), sha256Hex(original), len(original))
	block := patched[blockSize : 2*blockSize]
	validRecord := recordBytes(blockSize, uint32(len(block)), block)

	cases := []struct {
		name    string
		header  string
		body    []byte
		wantErr string
	}{
		{name: "记录乱序", header: baseHeader, wantErr: "记录范围非法",
			body: append(recordBytes(2*blockSize, blockSize, patched[2*blockSize:3*blockSize]), validRecord...)},
		{name: "块长超过 4 KiB", header: baseHeader, wantErr: "记录范围非法",
			body: recordBytes(blockSize, blockSize+1, block)},
		{name: "记录超出文件大小", header: baseHeader, wantErr: "记录范围非法",
			body: recordBytes(int64(len(patched))-10, blockSize, block)},
		{name: "记录被截断", header: baseHeader, wantErr: "记录被截断", body: validRecord[:recordSize-3]},
		{name: "数据块被截断", header: baseHeader, wantErr: "数据块被截断",
			body: recordBytes(blockSize, blockSize, block[:100])},
		{name: "零长度记录", header: baseHeader, wantErr: "记录范围非法", body: recordBytes(blockSize, 0, nil)},
		{name: "头部哈希不符", wantErr: "身份与 manifest 不一致",
			header: strings.Replace(baseHeader, key, strings.Repeat("0", 64), 1), body: validRecord},
		{name: "头部基准不符", wantErr: "原版基准与 manifest 不一致",
			header: strings.Replace(baseHeader, sha256Hex(original), strings.Repeat("a", 64), 1), body: validRecord},
		{name: "头部不是 JSON", wantErr: "头部解析失败", header: "not json at all", body: validRecord},
		{name: "重建哈希不符", header: baseHeader, wantErr: "重建哈希不符",
			body: recordBytes(blockSize, blockSize, pattern(13, blockSize))},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			info := manifest{Format: payloadFormat, Kind: payloadKind, Version: "corrupt", Language: "zh-CN",
				Files: []fileEntry{{Path: "x.bin", OriginalSHA256: sha256Hex(original), OriginalSize: int64(len(original)),
					PatchedSHA256: key, PatchedSize: int64(len(patched)), Object: objectPrefix + key + ".gz"}}}
			path := writePayloadZip(t, filepath.Join(directory, testCase.name+".zip"), info,
				map[string][]byte{objectPrefix + key + ".gz": gzipParts(t, []byte(testCase.header+"\n"), testCase.body)})
			loaded := openTestPayload(t, path)
			base := filepath.Join(directory, "base.bin")
			if err := os.WriteFile(base, original, 0o644); err != nil {
				t.Fatal(err)
			}
			var output bytes.Buffer
			err := loaded.replay(info.Files[0], base, &output, nil)
			if err == nil {
				t.Fatalf("期望拒绝，但回放成功了")
			}
			if !strings.Contains(err.Error(), testCase.wantErr) && testCase.wantErr != "" {
				t.Fatalf("错误信息不含 %q: %v", testCase.wantErr, err)
			}
		})
	}
}

func TestReplayRejectsShortBase(t *testing.T) {
	directory := t.TempDir()
	original := pattern(14, 2*blockSize)
	patched := pattern(15, 4*blockSize) // 变长且尾部没有记录覆盖
	key := sha256Hex(patched)
	header := fmt.Sprintf(`{"format":1,"sha256":%q,"size":%d,"base":{"path":"b","sha256":%q,"size":%d}}`,
		key, len(patched), sha256Hex(original), len(original))
	info := manifest{Format: payloadFormat, Kind: payloadKind, Version: "short", Language: "zh-CN",
		Files: []fileEntry{{Path: "x.bin", OriginalSHA256: sha256Hex(original), OriginalSize: int64(len(original)),
			PatchedSHA256: key, PatchedSize: int64(len(patched)), Object: objectPrefix + key + ".gz"}}}
	path := writePayloadZip(t, filepath.Join(directory, "short.zip"), info,
		map[string][]byte{objectPrefix + key + ".gz": gzipParts(t, []byte(header+"\n"))})
	loaded := openTestPayload(t, path)
	base := filepath.Join(directory, "base.bin")
	if err := os.WriteFile(base, original, 0o644); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	if err := loaded.replay(info.Files[0], base, &output, nil); err == nil {
		t.Fatal("原版比基准短时应当拒绝")
	}
}

func TestManifestValidation(t *testing.T) {
	valid := fileEntry{Path: "a.bin", OriginalSHA256: strings.Repeat("a", 64), OriginalSize: 1,
		PatchedSHA256: strings.Repeat("b", 64), PatchedSize: 2, Object: objectPrefix + strings.Repeat("b", 64) + ".gz"}
	base := func() manifest {
		return manifest{Format: payloadFormat, Kind: payloadKind, Version: "v", Language: "zh-CN",
			Files: []fileEntry{valid}}
	}
	cases := []struct {
		name   string
		mutate func(*manifest)
	}{
		{"格式版本", func(m *manifest) { m.Format = 2 }},
		{"类型", func(m *manifest) { m.Kind = "other" }},
		{"缺版本", func(m *manifest) { m.Version = "" }},
		{"缺语言", func(m *manifest) { m.Language = "" }},
		{"没有文件", func(m *manifest) { m.Files = nil }},
		{"绝对路径", func(m *manifest) { m.Files[0].Path = "/etc/passwd" }},
		{"上级跳转", func(m *manifest) { m.Files[0].Path = "../escape.bin" }},
		{"反斜杠", func(m *manifest) { m.Files[0].Path = `S-Tribute_Data\level0` }},
		{"盘符", func(m *manifest) { m.Files[0].Path = "C:/level0" }},
		{"重复条目", func(m *manifest) { m.Files = append(m.Files, valid) }},
		{"哈希过短", func(m *manifest) { m.Files[0].PatchedSHA256 = "abc" }},
		{"哈希含大写", func(m *manifest) { m.Files[0].PatchedSHA256 = strings.Repeat("B", 64) }},
		{"负大小", func(m *manifest) { m.Files[0].PatchedSize = -1 }},
		{"对象在目录外", func(m *manifest) { m.Files[0].Object = "elsewhere/x.gz" }},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			info := base()
			testCase.mutate(&info)
			data, err := json.Marshal(info)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := parseManifest(data); err == nil {
				t.Fatal("期望拒绝非法 manifest")
			}
		})
	}
}

func TestPayloadMissingObjectAndManifest(t *testing.T) {
	directory := t.TempDir()
	info := manifest{Format: payloadFormat, Kind: payloadKind, Version: "v", Language: "zh-CN",
		Files: []fileEntry{{Path: "a.bin", OriginalSHA256: strings.Repeat("a", 64), OriginalSize: 1,
			PatchedSHA256: strings.Repeat("b", 64), PatchedSize: 2, Object: objectPrefix + strings.Repeat("b", 64) + ".gz"}}}
	path := writePayloadZip(t, filepath.Join(directory, "missing.zip"), info, nil)
	if _, err := openPayload(path); err == nil {
		t.Fatal("缺少差异对象时应当拒绝")
	}
	path = writePayloadZip(t, filepath.Join(directory, "nomanifest.zip"), info, nil)
	// 重新写一个只有对象没有 manifest 的包
	archive, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	writer := zip.NewWriter(archive)
	entry, err := writer.CreateHeader(&zip.FileHeader{Name: objectPrefix + "x.gz", Method: zip.Deflate})
	if err != nil {
		t.Fatal(err)
	}
	entry.Write([]byte("data"))
	writer.Close()
	archive.Close()
	if _, err := openPayload(path); err == nil {
		t.Fatal("缺少 manifest 时应当拒绝")
	}
}

func TestInstallRestoreRoundTrip(t *testing.T) {
	directory := t.TempDir()
	files := sampleFiles()
	path, _ := buildPayload(t, directory, "zh-CN-test01", "zh-CN", files)
	game := buildGame(t, files)
	loaded := openTestPayload(t, path)

	result, err := installPatch(game, loaded, nil)
	if err != nil {
		t.Fatalf("安装失败: %v", err)
	}
	if result.AlreadyInstalled {
		t.Fatal("首次安装不应报告已安装")
	}
	for _, file := range files {
		target := filepath.Join(game, filepath.FromSlash(file.name))
		data, err := os.ReadFile(target)
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(data, file.patched) {
			t.Fatalf("%s 安装后内容不符", file.name)
		}
		backup := filepath.Join(game, filepath.FromSlash(result.BackupDirectory), filepath.FromSlash(file.name))
		data, err = os.ReadFile(backup)
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(data, file.original) {
			t.Fatalf("%s 的原版备份内容不符", file.name)
		}
	}
	record, err := loadRecord(game)
	if err != nil || record == nil {
		t.Fatalf("安装记录缺失: %v", err)
	}
	if record.Status != statusInstalled || record.Version != "zh-CN-test01" {
		t.Fatalf("安装记录内容不符: %+v", record)
	}

	// 重复安装是幂等的，且不新建任何目录。
	again, err := installPatch(game, loaded, nil)
	if err != nil {
		t.Fatalf("重复安装失败: %v", err)
	}
	if !again.AlreadyInstalled {
		t.Fatal("重复安装应当识别为已安装")
	}
	assertOnlyBackupDir(t, game)
	if again.BackupDirectory != backupDirName {
		t.Fatalf("备份目录应为固定名 %s，实际 %s", backupDirName, again.BackupDirectory)
	}

	restored, err := restoreOriginal(game)
	if err != nil {
		t.Fatalf("恢复失败: %v", err)
	}
	if len(restored.Files) != len(files) {
		t.Fatalf("恢复文件数不符: %d", len(restored.Files))
	}
	if restored.BackupDirectory != backupDirName {
		t.Fatalf("恢复后备份目录应为固定名 %s，实际 %s", backupDirName, restored.BackupDirectory)
	}
	for _, file := range files {
		data, err := os.ReadFile(filepath.Join(game, filepath.FromSlash(file.name)))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(data, file.original) {
			t.Fatalf("%s 恢复后不是原版", file.name)
		}
	}
	// 还原不落回滚目录：撤销一次还原等于重装一次，载荷随时可用。
	assertOnlyBackupDir(t, game)
	record, err = loadRecord(game)
	if err != nil {
		t.Fatal(err)
	}
	if record.Status != statusRestored {
		t.Fatalf("恢复后记录状态不符: %s", record.Status)
	}
	if _, err := restoreOriginal(game); err == nil {
		t.Fatal("重复恢复应当被拒绝")
	}
}

// assertOnlyBackupDir 断言游戏目录里只有固定备份目录与安装记录，没有任何
// 带时间戳的残留——这是「备份不随安装次数膨胀」这条性质的直接体现。
func assertOnlyBackupDir(t *testing.T, game string) {
	t.Helper()
	entries, err := os.ReadDir(game)
	if err != nil {
		t.Fatal(err)
	}
	var patcherEntries []string
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), "FarlandSaga2Patcher") {
			patcherEntries = append(patcherEntries, entry.Name())
		}
	}
	want := []string{backupDirName, recordName}
	sort.Strings(want)
	sort.Strings(patcherEntries)
	if strings.Join(patcherEntries, ",") != strings.Join(want, ",") {
		t.Fatalf("游戏目录里的补丁器文件应为 %v，实际 %v", want, patcherEntries)
	}
	if _, err := os.Stat(filepath.Join(game, backupDirName)); err != nil {
		t.Fatalf("固定备份目录不存在: %v", err)
	}
}

func TestInstallRefusesUnexpectedState(t *testing.T) {
	directory := t.TempDir()
	files := sampleFiles()
	path, _ := buildPayload(t, directory, "zh-CN-test02", "zh-CN", files)
	game := buildGame(t, files)
	loaded := openTestPayload(t, path)

	if err := os.WriteFile(filepath.Join(game, "Texture.tex"), []byte("被人改过"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := installPatch(game, loaded, nil)
	if err == nil {
		t.Fatal("文件被修改时应当拒绝安装")
	}
	if !strings.Contains(err.Error(), "Texture.tex") {
		t.Fatalf("错误信息应指出问题文件: %v", err)
	}
	// 拒绝后游戏文件必须保持原样。
	data, err := os.ReadFile(filepath.Join(game, "Texture.tex"))
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "被人改过" {
		t.Fatal("拒绝安装后游戏文件被改动了")
	}
	if record, err := loadRecord(game); err != nil || record != nil {
		t.Fatalf("拒绝安装后不应留下安装记录: %v", err)
	}

	// 删除一个文件同样要拒绝。
	if err := os.Remove(filepath.Join(game, "Texture.tex")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(game, "Texture.tex"), files[2].original, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(game, "FarlandSaga2.bin")); err != nil {
		t.Fatal(err)
	}
	if _, err := installPatch(game, loaded, nil); err == nil {
		t.Fatal("文件缺失时应当拒绝安装")
	}
}

func TestInstallRollsBackOnCorruptObject(t *testing.T) {
	directory := t.TempDir()
	files := sampleFiles()
	path, entries := buildPayload(t, directory, "zh-CN-test03", "zh-CN", files)
	// 把第一个文件的差异对象换成哈希不符的版本（ZIP CRC 保持一致）。
	original := files[0].original
	patched := append([]byte(nil), files[0].patched...)
	copy(patched[blockSize:2*blockSize], pattern(21, blockSize))
	key := sha256Hex(patched)
	header := fmt.Sprintf(`{"format":1,"sha256":%q,"size":%d,"base":{"path":"b","sha256":%q,"size":%d}}`,
		key, len(patched), sha256Hex(original), len(original))
	corrupt := gzipParts(t, []byte(header+"\n"), recordBytes(blockSize, blockSize, patched[blockSize:2*blockSize]))
	info := manifest{Format: payloadFormat, Kind: payloadKind, Version: "zh-CN-test03", Language: "zh-CN",
		Files: entries}
	objects := map[string][]byte{}
	for index, file := range files {
		var buffer bytes.Buffer
		writeObject(t, &buffer, file.original, file.patched, sha256Hex(file.patched))
		objects[entries[index].Object] = buffer.Bytes()
	}
	objects[entries[0].Object] = corrupt
	path = writePayloadZip(t, path, info, objects)
	game := buildGame(t, files)
	loaded := openTestPayload(t, path)

	if _, err := installPatch(game, loaded, nil); err == nil {
		t.Fatal("差异对象损坏时应当中止安装")
	}
	for _, file := range files {
		data, err := os.ReadFile(filepath.Join(game, filepath.FromSlash(file.name)))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(data, file.original) {
			t.Fatalf("%s 在失败安装后被改动了", file.name)
		}
	}
	if record, err := loadRecord(game); err != nil || record != nil {
		t.Fatalf("失败安装不应留下安装记录: %v", err)
	}
	// 临时文件必须清理干净。
	err := filepath.Walk(game, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if strings.Contains(info.Name(), tempSuffix) {
			t.Fatalf("残留临时文件: %s", path)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestSwitchBetweenPayloadsAndRestore(t *testing.T) {
	directory := t.TempDir()
	files := sampleFiles()
	korean := make([]testFile, len(files))
	for index, file := range files {
		patched := append([]byte(nil), file.original...)
		copy(patched[:min(blockSize, len(patched))], pattern(31, min(blockSize, len(patched))))
		korean[index] = testFile{name: file.name, original: file.original, patched: patched}
	}
	chinesePath, _ := buildPayload(t, directory, "zh-CN-test04", "zh-CN", files)
	koreanPath, _ := buildPayload(t, directory, "ko-KR-test04", "ko-KR", korean)
	game := buildGame(t, files)

	chinese := openTestPayload(t, chinesePath)
	if _, err := installPatch(game, chinese, nil); err != nil {
		t.Fatalf("安装中文补丁失败: %v", err)
	}
	koreanLoaded := openTestPayload(t, koreanPath)
	result, err := installPatch(game, koreanLoaded, nil)
	if err != nil {
		t.Fatalf("切换韩语补丁失败: %v", err)
	}
	if result.Language != "ko-KR" {
		t.Fatalf("切换后语言不符: %s", result.Language)
	}
	for index, file := range korean {
		data, err := os.ReadFile(filepath.Join(game, filepath.FromSlash(file.name)))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(data, file.patched) {
			t.Fatalf("%s 切换后不是韩语补丁内容", file.name)
		}
		_ = index
	}
	// 原版备份始终是同一个固定目录，切语言不新建、不清空、不改写。
	if result.BackupDirectory != backupDirName {
		t.Fatalf("切换后备份目录应为固定名 %s，实际 %s", backupDirName, result.BackupDirectory)
	}
	firstBackup := filepath.Join(game, backupDirName)
	for _, file := range files {
		data, err := os.ReadFile(filepath.Join(firstBackup, filepath.FromSlash(file.name)))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(data, file.original) {
			t.Fatalf("%s 的原版备份被切换语言改动了", file.name)
		}
	}
	assertOnlyBackupDir(t, game)
	if _, err := restoreOriginal(game); err != nil {
		t.Fatalf("切换后恢复失败: %v", err)
	}
	for _, file := range files {
		data, err := os.ReadFile(filepath.Join(game, filepath.FromSlash(file.name)))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(data, file.original) {
			t.Fatalf("%s 恢复后不是原版", file.name)
		}
	}
}

// koreanFiles 造一组与 files 同原版、补丁后内容不同的文件，用来模拟切换语言。
func koreanFiles(files []testFile) []testFile {
	out := make([]testFile, len(files))
	for index, file := range files {
		patched := append([]byte(nil), file.original...)
		copy(patched[:min(blockSize, len(patched))], pattern(31, min(blockSize, len(patched))))
		out[index] = testFile{name: file.name, original: file.original, patched: patched}
	}
	return out
}

// restoreOriginals 把游戏文件写回原版，模拟「Steam 校验完安装文件」之后的状态。
func restoreOriginals(t *testing.T, game string, files []testFile) {
	t.Helper()
	for _, file := range files {
		target := filepath.Join(game, filepath.FromSlash(file.name))
		if err := os.WriteFile(target, file.original, 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// TestInstallSweepsStaleBackupDirs 覆盖用户实际遇到的现场：游戏目录里堆着旧版
// 留下的带时间戳备份与还原回滚目录。安装成功后必须一次性清掉，并报告腾出了
// 多少空间。
func TestInstallSweepsStaleBackupDirs(t *testing.T) {
	directory := t.TempDir()
	files := sampleFiles()
	chinesePath, _ := buildPayload(t, directory, "zh-CN-sweep", "zh-CN", files)
	koreanPath, _ := buildPayload(t, directory, "ko-KR-sweep", "ko-KR", koreanFiles(files))
	game := buildGame(t, files)

	if _, err := installPatch(game, openTestPayload(t, chinesePath), nil); err != nil {
		t.Fatalf("安装中文补丁失败: %v", err)
	}
	stale := []string{"FarlandSaga2Patcher-backup-20260101-000000", "FarlandSaga2Patcher-restore-rollback-20260101-000000"}
	for _, name := range stale {
		if err := os.MkdirAll(filepath.Join(game, name), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(game, name, "junk.bin"), pattern(77, 4096), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	result, err := installPatch(game, openTestPayload(t, koreanPath), nil)
	if err != nil {
		t.Fatalf("切换韩语补丁失败: %v", err)
	}
	assertOnlyBackupDir(t, game)
	for _, name := range stale {
		if !slicesContains(result.CleanedUp, name) {
			t.Fatalf("应清理 %s，实际 %v", name, result.CleanedUp)
		}
	}
	// 切换语言时暂存回滚副本也会被收掉，所以清理数只多不少。
	if len(result.CleanedUp) < len(stale) {
		t.Fatalf("至少应清理 %d 个残留目录，实际 %v", len(stale), result.CleanedUp)
	}
	if result.ReclaimedBytes < int64(len(pattern(77, 4096))) {
		t.Fatalf("腾出空间统计不合理: %d", result.ReclaimedBytes)
	}
	if record, err := loadRecord(game); err != nil || record.BackupDirectory != backupDirName {
		t.Fatalf("记录的备份目录应为固定名: %+v (%v)", record, err)
	}
}

func slicesContains(list []string, want string) bool {
	for _, item := range list {
		if item == want {
			return true
		}
	}
	return false
}

// TestInstallMigratesRecordedTimestampedBackup 覆盖老安装：记录指向带时间戳的
// 备份目录，而固定目录不存在。此时必须把旧目录原地改名成固定名，而不是再从
// 游戏文件拷一份——游戏文件已经是补丁态，根本拷不出原版。
func TestInstallMigratesRecordedTimestampedBackup(t *testing.T) {
	directory := t.TempDir()
	files := sampleFiles()
	chinesePath, _ := buildPayload(t, directory, "zh-CN-migrate", "zh-CN", files)
	koreanPath, _ := buildPayload(t, directory, "ko-KR-migrate", "ko-KR", koreanFiles(files))
	game := buildGame(t, files)

	if _, err := installPatch(game, openTestPayload(t, chinesePath), nil); err != nil {
		t.Fatalf("安装中文补丁失败: %v", err)
	}
	// 把固定备份改名成旧式时间戳名，并让记录指向它，模拟上一版补丁器的现场。
	legacy := "FarlandSaga2Patcher-backup-20260101-010203"
	if err := os.Rename(filepath.Join(game, backupDirName), filepath.Join(game, legacy)); err != nil {
		t.Fatal(err)
	}
	record, err := loadRecord(game)
	if err != nil {
		t.Fatal(err)
	}
	record.BackupDirectory = legacy
	if err := writeRecord(game, record); err != nil {
		t.Fatal(err)
	}

	result, err := installPatch(game, openTestPayload(t, koreanPath), nil)
	if err != nil {
		t.Fatalf("迁移后切换韩语补丁失败: %v", err)
	}
	if result.BackupDirectory != backupDirName {
		t.Fatalf("迁移后备份目录应为固定名 %s，实际 %s", backupDirName, result.BackupDirectory)
	}
	assertOnlyBackupDir(t, game)
	for _, file := range files {
		data, err := os.ReadFile(filepath.Join(game, backupDirName, filepath.FromSlash(file.name)))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(data, file.original) {
			t.Fatalf("%s 的原版备份内容不符", file.name)
		}
	}
}

// TestInstallRebuildsBackupFromOriginalFiles 覆盖备份目录被用户删掉、但游戏文件
// 恰好还是原版的情况：应当静默重建，不拿一句「找不到备份」去打扰用户。
func TestInstallRebuildsBackupFromOriginalFiles(t *testing.T) {
	directory := t.TempDir()
	files := sampleFiles()
	chinesePath, _ := buildPayload(t, directory, "zh-CN-rebuild", "zh-CN", files)
	koreanPath, _ := buildPayload(t, directory, "ko-KR-rebuild", "ko-KR", koreanFiles(files))
	game := buildGame(t, files)

	if _, err := installPatch(game, openTestPayload(t, chinesePath), nil); err != nil {
		t.Fatalf("安装中文补丁失败: %v", err)
	}
	if err := os.RemoveAll(filepath.Join(game, backupDirName)); err != nil {
		t.Fatal(err)
	}
	restoreOriginals(t, game, files)

	result, err := installPatch(game, openTestPayload(t, koreanPath), nil)
	if err != nil {
		t.Fatalf("备份缺失但游戏是原版时应能重建: %v", err)
	}
	if result.BackupDirectory != backupDirName {
		t.Fatalf("备份目录应为固定名 %s，实际 %s", backupDirName, result.BackupDirectory)
	}
	assertOnlyBackupDir(t, game)
	for _, file := range files {
		data, err := os.ReadFile(filepath.Join(game, filepath.FromSlash(file.name)))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(data, koreanFiles(files)[0].patched) && file.name == files[0].name {
			t.Fatalf("%s 安装后不是韩语补丁内容", file.name)
		}
	}
}

// TestInstallFailsCleanlyWhenBackupLostAndPatched 是本次修复的核心回归：备份被
// 删、游戏已是补丁态时，安装必须原样失败——既不改写文件，也不在游戏目录里留下
// 任何新目录。旧版本会先建一个几百 MB 的备份再报错，那个孤儿目录一直留在盘上。
func TestInstallFailsCleanlyWhenBackupLostAndPatched(t *testing.T) {
	directory := t.TempDir()
	files := sampleFiles()
	chinesePath, _ := buildPayload(t, directory, "zh-CN-lost", "zh-CN", files)
	koreanPath, _ := buildPayload(t, directory, "ko-KR-lost", "ko-KR", koreanFiles(files))
	game := buildGame(t, files)

	if _, err := installPatch(game, openTestPayload(t, chinesePath), nil); err != nil {
		t.Fatalf("安装中文补丁失败: %v", err)
	}
	patched := map[string][]byte{}
	for _, file := range files {
		data, err := os.ReadFile(filepath.Join(game, filepath.FromSlash(file.name)))
		if err != nil {
			t.Fatal(err)
		}
		patched[file.name] = data
	}
	if err := os.RemoveAll(filepath.Join(game, backupDirName)); err != nil {
		t.Fatal(err)
	}

	_, err := installPatch(game, openTestPayload(t, koreanPath), nil)
	if err == nil {
		t.Fatal("备份丢失且游戏已打补丁时应当拒绝安装")
	}
	if !strings.Contains(err.Error(), "验证游戏文件的完整性") {
		t.Fatalf("错误信息应指出取回原版的途径: %v", err)
	}
	// 一个字节都不能改，一个目录都不能多。
	for name, want := range patched {
		data, err := os.ReadFile(filepath.Join(game, filepath.FromSlash(name)))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(data, want) {
			t.Fatalf("%s 在失败的安装中被改动了", name)
		}
	}
	entries, err := os.ReadDir(game)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), "FarlandSaga2Patcher") && entry.Name() != recordName {
			t.Fatalf("失败的安装留下了 %s", entry.Name())
		}
	}
}

// TestInstallRepairsCorruptBackup 备份里有个文件被改过，但游戏文件还是原版：
// 应当用当前文件把备份修好，而不是拒绝安装。
func TestInstallRepairsCorruptBackup(t *testing.T) {
	directory := t.TempDir()
	files := sampleFiles()
	chinesePath, _ := buildPayload(t, directory, "zh-CN-repair", "zh-CN", files)
	koreanPath, _ := buildPayload(t, directory, "ko-KR-repair", "ko-KR", koreanFiles(files))
	game := buildGame(t, files)

	if _, err := installPatch(game, openTestPayload(t, chinesePath), nil); err != nil {
		t.Fatalf("安装中文补丁失败: %v", err)
	}
	victim := filepath.Join(game, backupDirName, filepath.FromSlash(files[0].name))
	if err := os.WriteFile(victim, pattern(88, len(files[0].original)), 0o644); err != nil {
		t.Fatal(err)
	}
	restoreOriginals(t, game, files)

	if _, err := installPatch(game, openTestPayload(t, koreanPath), nil); err != nil {
		t.Fatalf("备份损坏但游戏是原版时应能修复: %v", err)
	}
	assertOnlyBackupDir(t, game)
	data, err := os.ReadFile(victim)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(data, files[0].original) {
		t.Fatal("修复后的原版备份内容不符")
	}
}

// TestInstallRefusesCorruptBackupWhenPatched 备份损坏且游戏已是补丁态：无法
// 修复，必须明确指出是哪个文件不对。
func TestInstallRefusesCorruptBackupWhenPatched(t *testing.T) {
	directory := t.TempDir()
	files := sampleFiles()
	chinesePath, _ := buildPayload(t, directory, "zh-CN-corrupt", "zh-CN", files)
	koreanPath, _ := buildPayload(t, directory, "ko-KR-corrupt", "ko-KR", koreanFiles(files))
	game := buildGame(t, files)

	if _, err := installPatch(game, openTestPayload(t, chinesePath), nil); err != nil {
		t.Fatalf("安装中文补丁失败: %v", err)
	}
	victim := filepath.Join(game, backupDirName, filepath.FromSlash(files[0].name))
	if err := os.WriteFile(victim, pattern(88, len(files[0].original)), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := installPatch(game, openTestPayload(t, koreanPath), nil)
	if err == nil {
		t.Fatal("备份损坏且游戏已打补丁时应当拒绝安装")
	}
	if !strings.Contains(err.Error(), files[0].name) {
		t.Fatalf("错误信息应指出损坏的文件: %v", err)
	}
	assertOnlyBackupDir(t, game)
}

// TestRestoreUsesRecordedTimestampedBackup 还原要认记录里的旧式时间戳目录，
// 老安装在迁移之前也能还原。
func TestRestoreUsesRecordedTimestampedBackup(t *testing.T) {
	directory := t.TempDir()
	files := sampleFiles()
	path, _ := buildPayload(t, directory, "zh-CN-legacy", "zh-CN", files)
	game := buildGame(t, files)

	if _, err := installPatch(game, openTestPayload(t, path), nil); err != nil {
		t.Fatalf("安装失败: %v", err)
	}
	legacy := "FarlandSaga2Patcher-backup-20260101-040506"
	if err := os.Rename(filepath.Join(game, backupDirName), filepath.Join(game, legacy)); err != nil {
		t.Fatal(err)
	}
	record, err := loadRecord(game)
	if err != nil {
		t.Fatal(err)
	}
	record.BackupDirectory = legacy
	if err := writeRecord(game, record); err != nil {
		t.Fatal(err)
	}

	result, err := restoreOriginal(game)
	if err != nil {
		t.Fatalf("从旧式备份还原失败: %v", err)
	}
	if result.BackupDirectory != legacy {
		t.Fatalf("还原应使用记录里的 %s，实际 %s", legacy, result.BackupDirectory)
	}
	for _, file := range files {
		data, err := os.ReadFile(filepath.Join(game, filepath.FromSlash(file.name)))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(data, file.original) {
			t.Fatalf("%s 还原后不是原版", file.name)
		}
	}
}

// TestRestoreReportsUnusableBackup 备份彻底没了的时候，还原要把固定目录和记录
// 里的目录都点出来，而不是只说一句找不到备份。
func TestRestoreReportsUnusableBackup(t *testing.T) {
	directory := t.TempDir()
	files := sampleFiles()
	path, _ := buildPayload(t, directory, "zh-CN-nobackup", "zh-CN", files)
	game := buildGame(t, files)

	if _, err := installPatch(game, openTestPayload(t, path), nil); err != nil {
		t.Fatalf("安装失败: %v", err)
	}
	legacy := "FarlandSaga2Patcher-backup-20260101-070809"
	if err := os.Rename(filepath.Join(game, backupDirName), filepath.Join(game, legacy)); err != nil {
		t.Fatal(err)
	}
	record, err := loadRecord(game)
	if err != nil {
		t.Fatal(err)
	}
	record.BackupDirectory = legacy
	if err := writeRecord(game, record); err != nil {
		t.Fatal(err)
	}
	if err := os.RemoveAll(filepath.Join(game, legacy)); err != nil {
		t.Fatal(err)
	}

	_, err = restoreOriginal(game)
	if err == nil {
		t.Fatal("备份缺失时应当拒绝还原")
	}
	for _, want := range []string{backupDirName, legacy} {
		if !strings.Contains(err.Error(), want) {
			t.Fatalf("错误信息应提到 %s: %v", want, err)
		}
	}
}

func TestInspectBackup(t *testing.T) {
	directory := t.TempDir()
	files := sampleFiles()
	path, entries := buildPayload(t, directory, "zh-CN-health", "zh-CN", files)
	game := buildGame(t, files)
	loaded := openTestPayload(t, path)
	targets := backupTargetsFromManifest(loaded.info)

	health := inspectBackup(game, targets)
	if health.Present || health.Complete {
		t.Fatalf("目录不存在时应报告缺失: %+v", health)
	}
	if health.Directory != backupDirName {
		t.Fatalf("目录名应为 %s，实际 %s", backupDirName, health.Directory)
	}

	if _, err := installPatch(game, loaded, nil); err != nil {
		t.Fatalf("安装失败: %v", err)
	}
	health = inspectBackup(game, targets)
	if !health.Present || !health.Complete || len(health.Problems) != 0 {
		t.Fatalf("完整备份应报告健康: %+v", health)
	}
	if len(entries) != len(files) {
		t.Fatalf("清单文件数不符: %d", len(entries))
	}

	victim := filepath.Join(game, backupDirName, filepath.FromSlash(files[1].name))
	if err := os.WriteFile(victim, pattern(99, len(files[1].original)), 0o644); err != nil {
		t.Fatal(err)
	}
	health = inspectBackup(game, targets)
	if !health.Present || health.Complete {
		t.Fatalf("损坏备份不应报告健康: %+v", health)
	}
	if len(health.Problems) != 1 || !strings.Contains(health.Problems[0], files[1].name) {
		t.Fatalf("应指出损坏的文件: %v", health.Problems)
	}
}

func TestSweepStaleBackupsKeepsFixedName(t *testing.T) {
	game := t.TempDir()
	keep := filepath.Join(game, backupDirName)
	if err := os.MkdirAll(keep, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(keep, "original.bin"), pattern(1, 128), 0o644); err != nil {
		t.Fatal(err)
	}
	// 与补丁无关的目录不能被扫掉。
	unrelated := filepath.Join(game, "S-Tribute_Data")
	if err := os.MkdirAll(unrelated, 0o755); err != nil {
		t.Fatal(err)
	}
	stale := map[string]int64{
		"FarlandSaga2Patcher-backup-20260101-000000":      4096,
		"FarlandSaga2Patcher-backup-20260101-000000-2":    2048,
		"FarlandSaga2Patcher-restore-rollback-20260101-1": 1024,
		"FarlandSaga2Patcher-rollback":                    512,
	}
	wantBytes := int64(0)
	for name, size := range stale {
		if err := os.MkdirAll(filepath.Join(game, name), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(game, name, "junk.bin"), pattern(3, int(size)), 0o644); err != nil {
			t.Fatal(err)
		}
		wantBytes += size
	}

	removed, reclaimed, err := sweepStaleBackups(game, backupDirName)
	if err != nil {
		t.Fatal(err)
	}
	if len(removed) != len(stale) {
		t.Fatalf("应清理 %d 个目录，实际 %v", len(stale), removed)
	}
	if reclaimed != wantBytes {
		t.Fatalf("腾出空间应为 %d，实际 %d", wantBytes, reclaimed)
	}
	// 固定备份目录与补丁无关的目录必须原样留着，旧式目录一个不剩。
	for _, name := range keysOf(stale) {
		if _, err := os.Stat(filepath.Join(game, name)); !os.IsNotExist(err) {
			t.Fatalf("%s 应被清理: %v", name, err)
		}
	}
	for _, name := range []string{backupDirName, "S-Tribute_Data"} {
		if _, err := os.Stat(filepath.Join(game, name)); err != nil {
			t.Fatalf("%s 不应当被清理: %v", name, err)
		}
	}
}

func keysOf(m map[string]int64) []string {
	out := make([]string, 0, len(m))
	for key := range m {
		out = append(out, key)
	}
	return out
}

func TestHumanSize(t *testing.T) {
	cases := []struct {
		size int64
		want string
	}{
		{0, "0 B"},
		{999, "999 B"},
		{1024, "1.0 KiB"},
		{301359116, "287.4 MiB"},
		{3 * (1 << 30), "3.0 GiB"},
	}
	for _, item := range cases {
		if got := humanSize(item.size); got != item.want {
			t.Fatalf("humanSize(%d) = %s，期望 %s", item.size, got, item.want)
		}
	}
}

func TestRecordRejectsUnsafePaths(t *testing.T) {
	game := t.TempDir()
	for _, directory := range []string{"../escape", "/etc", `C:\backup`, "a/../../b"} {
		record := installRecord{Format: recordFormat, Kind: recordKind, Version: "v", Language: "zh-CN",
			BackupDirectory: directory, Status: statusInstalled,
			Files: map[string]fileRecord{"a.bin": {OriginalSHA256: strings.Repeat("a", 64),
				PatchedSHA256: strings.Repeat("b", 64), Size: 1}}}
		data, err := json.Marshal(record)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(recordPath(game), data, 0o644); err != nil {
			t.Fatal(err)
		}
		if _, err := loadRecord(game); err == nil {
			t.Fatalf("备份目录 %q 应当被拒绝", directory)
		}
	}
	record := installRecord{Format: recordFormat, Kind: recordKind, Version: "v", Language: "zh-CN",
		BackupDirectory: "backup", Status: statusInstalled,
		Files: map[string]fileRecord{"../a.bin": {OriginalSHA256: strings.Repeat("a", 64),
			PatchedSHA256: strings.Repeat("b", 64), Size: 1}}}
	data, err := json.Marshal(record)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(recordPath(game), data, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := loadRecord(game); err == nil {
		t.Fatal("含上级跳转的文件名应当被拒绝")
	}
}

func TestRestoreRefusesModifiedInstalledFile(t *testing.T) {
	directory := t.TempDir()
	files := sampleFiles()
	path, _ := buildPayload(t, directory, "zh-CN-test05", "zh-CN", files)
	game := buildGame(t, files)
	loaded := openTestPayload(t, path)
	if _, err := installPatch(game, loaded, nil); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(game, "Texture.tex"), []byte("安装后又被改了"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := restoreOriginal(game); err == nil {
		t.Fatal("安装后文件被修改时应当拒绝恢复")
	}
	// 拒绝后文件保持用户的修改，不被覆盖。
	data, err := os.ReadFile(filepath.Join(game, "Texture.tex"))
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "安装后又被改了" {
		t.Fatal("拒绝恢复时覆盖了用户文件")
	}
}

func TestVerifyStates(t *testing.T) {
	directory := t.TempDir()
	files := sampleFiles()
	path, _ := buildPayload(t, directory, "zh-CN-test06", "zh-CN", files)
	game := buildGame(t, files)
	loaded := openTestPayload(t, path)

	statuses, err := verifyInstall(game, loaded)
	if err != nil {
		t.Fatal(err)
	}
	for _, status := range statuses {
		if status.State != stateOriginal {
			t.Fatalf("%s 初始状态应为原版: %s", status.Path, status.State)
		}
	}
	if _, err := installPatch(game, loaded, nil); err != nil {
		t.Fatal(err)
	}
	statuses, err = verifyInstall(game, nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, status := range statuses {
		if status.State != statePatched {
			t.Fatalf("%s 安装后状态应为已打补丁: %s", status.Path, status.State)
		}
	}
	if err := os.WriteFile(filepath.Join(game, "Texture.tex"), []byte("漂移"), 0o644); err != nil {
		t.Fatal(err)
	}
	statuses, err = verifyInstall(game, nil)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, status := range statuses {
		if status.Path == "Texture.tex" && status.State == stateMismatch {
			found = true
		}
	}
	if !found {
		t.Fatal("文件漂移应被识别为不匹配")
	}
}

func TestSafeRelativePathAcceptsNestedPosix(t *testing.T) {
	for _, name := range []string{"a.bin", "S-Tribute_Data/level0", "a/b/c/d.bin"} {
		got, err := safeRelativePath(name)
		if err != nil || got != name {
			t.Fatalf("%q 应被接受: %q %v", name, got, err)
		}
	}
	for _, name := range []string{"", ".", "a//b", "a/./b", "a/", "/a"} {
		if _, err := safeRelativePath(name); err == nil {
			t.Fatalf("%q 应被拒绝", name)
		}
	}
}

// blockedFiles 与 summarizeStates 是图形界面和安装前预检共用的判断，
// samePath 用于把拖放和浏览得到的路径与已发现的载荷对上。
func TestBlockedFiles(t *testing.T) {
	statuses := []fileStatus{
		{Path: "a.bin", State: stateOriginal},
		{Path: "b.bin", State: statePatched},
		{Path: "c.bin", State: statePrevious},
	}
	if blocked := blockedFiles(statuses); len(blocked) != 0 {
		t.Fatalf("原版、本程序装的补丁、其他版本补丁都应放行，得到 %v", blocked)
	}
	statuses = append(statuses,
		fileStatus{Path: "d.bin", State: stateMismatch},
		fileStatus{Path: "e.bin", State: stateMissing})
	blocked := blockedFiles(statuses)
	if len(blocked) != 2 {
		t.Fatalf("应拦下 2 个文件，得到 %d 个: %v", len(blocked), blocked)
	}
	for _, line := range blocked {
		if !strings.Contains(line, "d.bin") && !strings.Contains(line, "e.bin") {
			t.Fatalf("拦下信息里应含文件名: %q", line)
		}
	}
}

func TestSummarizeStates(t *testing.T) {
	cases := []struct {
		name   string
		states []string
		want   string
	}{
		{"全部原版", []string{stateOriginal, stateOriginal}, "原版，尚未安装补丁"},
		{"全部已打补丁", []string{statePatched, statePatched}, "已是补丁状态，但安装记录缺失"},
		{"混合", []string{statePatched, stateOriginal}, "部分文件已是补丁状态"},
		{"有异常文件", []string{statePatched, stateMismatch}, "游戏文件状态不一致，请查看文件清单"},
		{"有缺失文件", []string{stateOriginal, stateMissing}, "游戏文件状态不一致，请查看文件清单"},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			statuses := make([]fileStatus, 0, len(testCase.states))
			for index, state := range testCase.states {
				statuses = append(statuses, fileStatus{Path: fmt.Sprintf("f%d.bin", index), State: state})
			}
			if got := summarizeStates(statuses); got != testCase.want {
				t.Fatalf("summarizeStates = %q，期望 %q", got, testCase.want)
			}
		})
	}
}

func TestSamePath(t *testing.T) {
	if !samePath(`C:\Games\Farland`, `c:\games\farland`) {
		t.Fatal("Windows 路径大小写不敏感，同一路径应判定为相同")
	}
	if !samePath("patch.zip", filepath.Join("sub", "..", "patch.zip")) {
		t.Fatal("清理后相同的相对路径应判定为相同")
	}
	if samePath(`C:\Games\Farland`, `C:\Games\Other`) {
		t.Fatal("不同目录不应判定为相同")
	}
	if samePath("a.zip", "b.zip") {
		t.Fatal("不同文件名不应判定为相同")
	}
}

// 入口分发：gui 子命令在非 Windows 平台应给出明确错误，未知命令要报错。
func TestRunDispatch(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows 上 gui 子命令会真的开窗口")
	}
	err := run([]string{"gui"})
	if err == nil || !strings.Contains(err.Error(), "图形界面只在 Windows 版提供") {
		t.Fatalf("run(gui) = %v，期望提示图形界面只在 Windows 版提供", err)
	}
	if err := run([]string{"no-such-command"}); err == nil {
		t.Fatal("未知命令应报错")
	}
	if err := run([]string{"help"}); err != nil {
		t.Fatalf("run(help) = %v，期望正常返回", err)
	}
}

// 内嵌补丁：三个语言都要能解析，顺序固定为中、韩、英，且每个包都声明全部
// 目标文件。payload/ 为空时（比如刚克隆仓库还没打包）跳过，不算失败。
func TestLoadEmbedded(t *testing.T) {
	loaded, problems := loadEmbedded()
	for _, problem := range problems {
		t.Errorf("内嵌补丁解析失败：%s", problem)
	}
	if len(loaded) == 0 {
		t.Skip("payload/ 下没有内嵌补丁，跳过（打包后此测试会实际校验）")
	}
	want := []string{"zh-CN", "ko-KR", "en-US"}
	if len(loaded) != len(want) {
		t.Fatalf("内嵌补丁数量 = %d，期望 %d（%v）", len(loaded), len(want), want)
	}
	for index, code := range want {
		info := loaded[index].info
		if info.Language != code {
			t.Fatalf("第 %d 个补丁语言 = %s，期望 %s（顺序必须固定）", index, info.Language, code)
		}
		if (info.Format != payloadFormat && info.Format != 3) || info.Kind != payloadKind {
			t.Fatalf("%s 的 manifest 格式不符：format=%d kind=%s", code, info.Format, info.Kind)
		}
		if len(info.Files) == 0 {
			t.Fatalf("%s 的 manifest 没有目标文件", code)
		}
		for _, entry := range info.Files {
			if entry.Path == "" || (entry.Object == "" && entry.SectorsObject == "") {
				t.Fatalf("%s 的 manifest 有条目缺少路径或对象名", code)
			}
			if !validSHA256(entry.OriginalSHA256) || !validSHA256(entry.PatchedSHA256) {
				t.Fatalf("%s 的 %s 哈希不合法", code, entry.Path)
			}
		}
	}
}

// 语言代码到按钮文字与固定顺序的映射。第一屏按钮用的是语言「自己」的名字，
// 所以韩文按钮必须是 한국어，不能是中文的「韩文」——韩国人看不懂那个。
func TestNativeLanguageNameAndRank(t *testing.T) {
	names := map[string]string{
		"zh-CN": "简体中文",
		"ko-KR": "한국어",
		"en-US": "English",
		"ja-JP": "ja-JP",
	}
	for code, want := range names {
		if got := nativeLanguageName(code); got != want {
			t.Errorf("nativeLanguageName(%s) = %s，期望 %s", code, got, want)
		}
	}
	if languageRank("zh-CN") >= languageRank("ko-KR") ||
		languageRank("ko-KR") >= languageRank("en-US") ||
		languageRank("en-US") >= languageRank("fr-FR") {
		t.Fatal("语言顺序必须为中、韩、英，未知语言排最后")
	}
}

// 三张界面文字表必须逐字段补齐。用反射枚举字段，新增字段忘了翻译会在这里
// 失败，而不是等到用户在韩文 Windows 上看到一串英文。
func TestUIStringsComplete(t *testing.T) {
	tables := map[string]uiStrings{"zh-CN": uiChinese, "ko-KR": uiKorean, "en-US": uiEnglish}
	if len(tables) != 3 {
		t.Fatalf("应该有三张表，实际 %d 张", len(tables))
	}
	value := reflect.ValueOf(uiChinese)
	typ := value.Type()
	for code, table := range tables {
		v := reflect.ValueOf(table)
		for i := 0; i < typ.NumField(); i++ {
			field := typ.Field(i)
			got := v.Field(i).String()
			if got == "" {
				t.Errorf("%s 表的 %s 是空的", code, field.Name)
			}
		}
	}
	// 三张表必须真的不同：同文字说明复制漏改。appTitle 不参与这个检查——
	// 它是三语共用的常量，故意不在表里。
	if uiChinese.pageTitleFormat == uiKorean.pageTitleFormat {
		t.Error("第二屏标题的中韩文不能相同")
	}
	if uiKorean.pageTitleFormat != "%s 패치" {
		t.Errorf("韩文第二屏标题 = %s，期望 %%s 패치", uiKorean.pageTitleFormat)
	}
	if uiChinese.buttonInstall == uiKorean.buttonInstall {
		t.Error("安装按钮的中韩文字不能相同")
	}
	if uiKorean.buttonInstall != "설치" {
		t.Errorf("韩文安装按钮 = %s，期望 설치", uiKorean.buttonInstall)
	}
}

// uiStrings.name 用「当前界面语言」称呼各语言，和第一屏按钮用的
// nativeLanguageName 是两回事，不能混。
func TestUIStringsNameUsesUILanguage(t *testing.T) {
	if got := uiKorean.name("ko-KR"); got != "한국어" {
		t.Errorf("韩文界面称呼韩语 = %s，期望 한국어", got)
	}
	if got := uiKorean.name("zh-CN"); got != "중국어(간체)" {
		t.Errorf("韩文界面称呼简中 = %s，期望 중국어(간체)", got)
	}
	if got := uiEnglish.name("ko-KR"); got != "Korean" {
		t.Errorf("英文界面称呼韩语 = %s，期望 Korean", got)
	}
	if got := uiChinese.name("ko-KR"); got != "韩文" {
		t.Errorf("中文界面称呼韩语 = %s，期望 韩文", got)
	}
	// 未知语言原样返回代码，不猜。
	if got := uiKorean.name("fr-FR"); got != "fr-FR" {
		t.Errorf("未知语言 = %s，期望 fr-FR", got)
	}
}

// 取不到的语言必须回落英文表，不能返回空表让界面变成一片空白。
func TestUIStringsForFallsBackToEnglish(t *testing.T) {
	for _, code := range []string{"ja-JP", "fr-FR", "", "KO-kr"} {
		got := uiStringsFor(code)
		if got != uiEnglish {
			t.Errorf("uiStringsFor(%q) 应回落英文表", code)
		}
	}
	for _, code := range []string{"zh-CN", "ko-KR", "en-US"} {
		if uiStringsFor(code) == uiEnglish && code != "en-US" {
			t.Errorf("uiStringsFor(%q) 不应回落英文表", code)
		}
	}
	// 韩文表必须指定带韩文字形的字体，否则中文 Windows 上会是方框。
	if uiKorean.fontFace != "Malgun Gothic" {
		t.Errorf("韩文字体 = %s，期望 Malgun Gothic", uiKorean.fontFace)
	}
}

// Format 2 支持任意合法的变长记录，不受 4096 字节固定块限制。
func TestReplayFormat2VariableLength(t *testing.T) {
	temp := t.TempDir()
	originalData := bytes.Repeat([]byte("ORIGINAL"), 2000) // 16000 字节
	origPath := filepath.Join(temp, "original.bin")
	if err := os.WriteFile(origPath, originalData, 0o644); err != nil {
		t.Fatal(err)
	}

	patchedData := make([]byte, len(originalData))
	copy(patchedData, originalData)
	// 在 offset 1000 处替换一段 8000 字节的连续内容（远超 4096 字节）
	replacement := bytes.Repeat([]byte("VARIABLE"), 1000) // 8000 字节
	copy(patchedData[1000:], replacement)

	origHash := sha256Hex(originalData)
	patchedHash := sha256Hex(patchedData)

	header := objectHeader{
		Format: 2,
		SHA256: patchedHash,
		Size:   int64(len(patchedData)),
		Base: &struct {
			Path   string `json:"path"`
			SHA256 string `json:"sha256"`
			Size   int64  `json:"size"`
		}{
			Path:   "test.bin",
			SHA256: origHash,
			Size:   int64(len(originalData)),
		},
	}
	headerBytes, _ := json.Marshal(header)
	headerLine := append(headerBytes, '\n')

	// 记录: offset 1000, size 8000
	rec := recordBytes(1000, uint32(len(replacement)), replacement)
	gzData := gzipParts(t, headerLine, rec)

	objName := "objects/" + patchedHash + ".gz"
	info := manifest{
		Format:     payloadFormat,
		Kind:       payloadKind,
		Version:    "v-format2",
		Language:   "zh-CN",
		CreatedUTC: "2026-10-18T00:00:00Z",
		Files: []fileEntry{
			{
				Path:           "test.bin",
				OriginalSHA256: origHash,
				OriginalSize:   int64(len(originalData)),
				PatchedSHA256:  patchedHash,
				PatchedSize:    int64(len(patchedData)),
				Object:         objName,
			},
		},
	}

	zipPath := filepath.Join(temp, "format2-patch.zip")
	writePayloadZip(t, zipPath, info, map[string][]byte{objName: gzData})

	loaded, err := openPayload(zipPath)
	if err != nil {
		t.Fatalf("openPayload 失败: %v", err)
	}
	defer loaded.close()

	var output bytes.Buffer
	if err := loaded.replay(loaded.info.Files[0], origPath, &output, nil); err != nil {
		t.Fatalf("Format 2 replay 失败: %v", err)
	}

	if !bytes.Equal(output.Bytes(), patchedData) {
		t.Fatal("Format 2 重建数据与预期不符")
	}
}

// TestEmbeddedRealPayloadReplay 在本地备份存在时，验证所有内嵌 payload 能针对原版正确回放。
func TestEmbeddedRealPayloadReplay(t *testing.T) {
	backupDir := filepath.Join("..", "..", "work", "backups", "alpha01-20260930-104848")
	if _, err := os.Stat(backupDir); err != nil {
		t.Skip("本地无原版备份，跳过真实载荷端到端测试")
	}

	payloads, problems := loadEmbedded()
	if len(problems) > 0 {
		t.Fatalf("内嵌载荷问题: %v", problems)
	}

	for _, p := range payloads {
		t.Run(p.info.Language, func(t *testing.T) {
			for _, f := range p.info.Files {
				origPath := filepath.Join(backupDir, filepath.FromSlash(f.Path))
				if _, err := os.Stat(origPath); err != nil {
					altPath := filepath.Join("/mnt/e/Steam/steamapps/common/FARLAND SAGA II Saturn Tribute", filepath.FromSlash(f.Path))
					if _, err := os.Stat(altPath); err == nil {
						origPath = altPath
					} else {
						t.Skipf("原版文件缺失: %s", origPath)
					}
				}
				h := sha256.New()
				var totalWritten int64
				countingWriter := io.MultiWriter(h, &byteCounter{written: &totalWritten})
				var replayErr error
				if f.SectorsObject != "" {
					exePath := filepath.Join("/mnt/e/Steam/steamapps/common/FARLAND SAGA II Saturn Tribute", "FarlandSaga2.exe")
					replayErr = p.replaySectors(f, origPath, exePath, countingWriter, nil)
				} else {
					replayErr = p.replay(f, origPath, countingWriter, nil)
				}
				if replayErr != nil {
					t.Fatalf("文件 %s 回放失败: %v", f.Path, replayErr)
				}
				gotHash := hex.EncodeToString(h.Sum(nil))
				if gotHash != f.PatchedSHA256 {
					t.Errorf("文件 %s 回放哈希不符: 期望 %s, 实际 %s", f.Path, f.PatchedSHA256, gotHash)
				}
				if totalWritten != f.PatchedSize {
					t.Errorf("文件 %s 回放大小不符: 期望 %d, 实际 %d", f.Path, f.PatchedSize, totalWritten)
				}
			}
		})
	}
}

type byteCounter struct {
	written *int64
}

func (b *byteCounter) Write(p []byte) (n int, err error) {
	*b.written += int64(len(p))
	return len(p), nil
}

