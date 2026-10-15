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
	"os"
	"path/filepath"
	"runtime"
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
		Game: "FARLAND SAGA I Saturn Tribute", CreatedUTC: "2026-10-14T00:00:00Z", Files: entries}
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
		{name: "FarlandSaga.bin", original: original, patched: patched},
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

	// 重复安装是幂等的，且不再新建备份。
	again, err := installPatch(game, loaded, nil)
	if err != nil {
		t.Fatalf("重复安装失败: %v", err)
	}
	if !again.AlreadyInstalled {
		t.Fatal("重复安装应当识别为已安装")
	}
	entries, err := os.ReadDir(game)
	if err != nil {
		t.Fatal(err)
	}
	backups := 0
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), backupPrefix) {
			backups++
		}
	}
	if backups != 1 {
		t.Fatalf("重复安装新建了备份目录: %d", backups)
	}

	restored, err := restoreOriginal(game)
	if err != nil {
		t.Fatalf("恢复失败: %v", err)
	}
	if len(restored.Files) != len(files) {
		t.Fatalf("恢复文件数不符: %d", len(restored.Files))
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
	if err := os.Remove(filepath.Join(game, "FarlandSaga.bin")); err != nil {
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
	// 原版备份仍指向第一次安装时建立的目录。
	if result.BackupDirectory == "" {
		t.Fatal("切换后没有记录原版备份")
	}
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
