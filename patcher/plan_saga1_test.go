//go:build !saga2

// 一代安装计划的解析、校验与安装往返测试。全部使用合成数据，不需要游戏文件；
// 记录流格式与差异对象一致，回放语义由 patcher_test.go 的共用用例覆盖。
package main

import (
	"bytes"
	"compress/gzip"
	"encoding/binary"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// planFileSpec 是合成计划里的一个目标文件。
type planFileSpec struct {
	name     string
	kind     string
	original []byte
	patched  []byte
}

// diffRecordStream 按 4 KiB 块比较原版与补丁后内容，生成升序差异记录流。
func diffRecordStream(original, patched []byte) []byte {
	var stream []byte
	for base := 0; base < len(patched); base += blockSize {
		end := min(base+blockSize, len(patched))
		origEnd := min(end, len(original))
		if base >= len(original) || !bytes.Equal(original[base:origEnd], patched[base:end]) {
			stream = append(stream, recordBytes(int64(base), uint32(end-base), patched[base:end])...)
		}
	}
	return stream
}

// fs1PlanFixture 汇集一份合成计划的所有部件，方便单个字段变异做拒绝测试。
type fs1PlanFixture struct {
	Header fs1PlanHeader
	Blob   []byte
}

func buildFS1PlanFixture(t *testing.T, languages []string, versions map[string]string,
	files []planFileSpec) fs1PlanFixture {
	t.Helper()
	fixture := fs1PlanFixture{Blob: nil}
	fixture.Header = fs1PlanHeader{
		Format:       fs1PlanFormat,
		Game:         "saga1",
		Languages:    languages,
		Versions:     map[string]string{},
		Files:        map[string]*fs1PlanFile{},
		LanguageData: map[string]*fs1PlanLanguage{},
	}
	var parts []byte
	offset := int64(0)
	for _, spec := range files {
		fixture.Header.Files[spec.name] = &fs1PlanFile{
			Kind: spec.kind, Path: spec.name,
			OriginalSHA256: sha256Hex(spec.original), Size: int64(len(spec.original)),
		}
	}
	for _, language := range languages {
		version, ok := versions[language]
		if !ok {
			version = "v-" + language
		}
		fixture.Header.Versions[language] = version
		section := &fs1PlanLanguage{Version: version, Files: map[string]*fs1PlanEntry{}}
		for _, spec := range files {
			records := diffRecordStream(spec.original, spec.patched)
			section.Files[spec.name] = &fs1PlanEntry{
				PatchedSHA256: sha256Hex(spec.patched), PatchedSize: int64(len(spec.patched)),
				RecordsOffset: offset, RecordsLength: int64(len(records)),
			}
			parts = append(parts, records...)
			offset += int64(len(records))
		}
		fixture.Header.LanguageData[language] = section
	}
	fixture.Blob = parts
	fixture.Header.BinarySHA256 = sha256Hex(parts)
	return fixture
}

// marshalFS1Plan 把 fixture 组装成 plan.bin 字节，可选地替换头部或数据段。
func marshalFS1Plan(t *testing.T, header fs1PlanHeader, blob []byte) []byte {
	t.Helper()
	headerBytes, err := json.Marshal(header)
	if err != nil {
		t.Fatal(err)
	}
	plan := make([]byte, 0, len(fs1PlanMagic)+4+len(headerBytes)+len(blob))
	plan = append(plan, fs1PlanMagic...)
	plan = binary.LittleEndian.AppendUint32(plan, uint32(len(headerBytes)))
	plan = append(plan, headerBytes...)
	var compressed bytes.Buffer
	writer := gzip.NewWriter(&compressed)
	if _, err := writer.Write(blob); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return append(plan, compressed.Bytes()...)
}

func planTestFiles() []planFileSpec {
	// 一个带空隙的流式文件、一个整体重写（尺寸变化）的文件、一个无差异文件。
	original := pattern(1, 6*blockSize+123)
	patched := append([]byte(nil), original...)
	copy(patched[blockSize:2*blockSize], pattern(9, blockSize))
	shrunkOriginal := pattern(5, 4*blockSize)
	shrunk := pattern(6, 2*blockSize+70)
	return []planFileSpec{
		{name: gameDiscImage, kind: "disc", original: original, patched: patched},
		{name: "Texture.tex", kind: "binary", original: shrunkOriginal, patched: shrunk},
	}
}

func loadTestFS1Plan(t *testing.T, data []byte) *fs1Plan {
	t.Helper()
	plan, err := loadFS1Plan(data)
	if err != nil {
		t.Fatalf("loadFS1Plan 失败: %v", err)
	}
	return plan
}

func TestFS1PlanLoadAndReplay(t *testing.T) {
	files := planTestFiles()
	fixture := buildFS1PlanFixture(t, []string{"zh-CN", "en-US"},
		map[string]string{"zh-CN": "zh-CN-plan01", "en-US": "en-US-plan01"}, files)
	plan := loadTestFS1Plan(t, marshalFS1Plan(t, fixture.Header, fixture.Blob))

	if got := plan.version("zh-CN"); got != "zh-CN-plan01" {
		t.Fatalf("版本号不符: %q", got)
	}
	for _, language := range []string{"zh-CN", "en-US"} {
		loaded, err := newFS1PlanPayload(plan, language)
		if err != nil {
			t.Fatalf("%s 包装失败: %v", language, err)
		}
		if want := "plan " + plan.version(language); loaded.engineName() != want {
			t.Fatalf("引擎名不符: %q != %q", loaded.engineName(), want)
		}
		if len(loaded.info.Files) != len(files) {
			t.Fatalf("%s 的文件数量不符: %d", language, len(loaded.info.Files))
		}
		for index, spec := range files {
			entry := loaded.info.Files[index]
			if entry.Path != spec.name {
				t.Fatalf("文件顺序不符: %s != %s", entry.Path, spec.name)
			}
			if entry.PatchedSHA256 != sha256Hex(spec.patched) || entry.PatchedSize != int64(len(spec.patched)) {
				t.Fatalf("%s 的补丁后身份不符", spec.name)
			}
			base := filepath.Join(t.TempDir(), "base.bin")
			if err := os.WriteFile(base, spec.original, 0o644); err != nil {
				t.Fatal(err)
			}
			var output bytes.Buffer
			if err := loaded.replayEntry(entry, base, "", &output, nil); err != nil {
				t.Fatalf("%s/%s 回放失败: %v", language, spec.name, err)
			}
			if !bytes.Equal(output.Bytes(), spec.patched) {
				t.Fatalf("%s/%s 回放结果与补丁内容不一致", language, spec.name)
			}
		}
	}
}

func TestFS1PlanInstallRestoreRoundTrip(t *testing.T) {
	files := planTestFiles()
	fixture := buildFS1PlanFixture(t, []string{"zh-CN", "ko-KR"},
		map[string]string{"zh-CN": "zh-CN-plan02", "ko-KR": "ko-KR-plan02"}, files)
	plan := loadTestFS1Plan(t, marshalFS1Plan(t, fixture.Header, fixture.Blob))
	game := buildGame(t, []testFile{
		{name: files[0].name, original: files[0].original, patched: files[0].patched},
		{name: files[1].name, original: files[1].original, patched: files[1].patched},
	})

	zhLoaded, err := newFS1PlanPayload(plan, "zh-CN")
	if err != nil {
		t.Fatal(err)
	}
	result, err := installPatch(game, zhLoaded, nil)
	if err != nil {
		t.Fatalf("安装失败: %v", err)
	}
	if result.Engine != "plan zh-CN-plan02" {
		t.Fatalf("安装记录引擎名不符: %q", result.Engine)
	}
	for _, spec := range files {
		data, err := os.ReadFile(filepath.Join(game, filepath.FromSlash(spec.name)))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(data, spec.patched) {
			t.Fatalf("%s 安装后内容不符", spec.name)
		}
	}
	again, err := installPatch(game, zhLoaded, nil)
	if err != nil {
		t.Fatalf("重复安装失败: %v", err)
	}
	if !again.AlreadyInstalled {
		t.Fatal("重复安装应当识别为已安装")
	}

	// 切换语言按重装处理：回放到原版基准上，不留第二份备份。
	koLoaded, err := newFS1PlanPayload(plan, "ko-KR")
	if err != nil {
		t.Fatal(err)
	}
	switched, err := installPatch(game, koLoaded, nil)
	if err != nil {
		t.Fatalf("切换语言失败: %v", err)
	}
	if switched.AlreadyInstalled || switched.Version != "ko-KR-plan02" {
		t.Fatalf("切换语言结果不符: %+v", switched)
	}
	if _, err := restoreOriginal(game); err != nil {
		t.Fatalf("恢复失败: %v", err)
	}
	for _, spec := range files {
		data, err := os.ReadFile(filepath.Join(game, filepath.FromSlash(spec.name)))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(data, spec.original) {
			t.Fatalf("%s 恢复后不是原版", spec.name)
		}
	}
}

func TestFS1PlanLanguageSelection(t *testing.T) {
	files := planTestFiles()
	fixture := buildFS1PlanFixture(t, []string{"zh-CN", "ko-KR", "en-US"}, nil, files)
	planBytes := marshalFS1Plan(t, fixture.Header, fixture.Blob)

	if _, err := wrapRecipeSource(nil, planBytes, ""); err == nil ||
		!strings.Contains(err.Error(), "--lang") {
		t.Fatalf("多语言计划缺 --lang 应当拒绝: %v", err)
	}
	// 外部载荷指明语言时沿用它的语言。
	if _, err := wrapRecipeSource(nil, planBytes, "fr-FR"); err == nil {
		t.Fatal("计划外语言应当拒绝")
	} else if !strings.Contains(err.Error(), "fr-FR") {
		t.Fatalf("错误信息应包含语言代码: %v", err)
	}

	// 单语言计划无需 --lang。
	single := buildFS1PlanFixture(t, []string{"zh-CN"},
		map[string]string{"zh-CN": "zh-CN-single"}, files)
	loaded, err := wrapRecipeSource(nil, marshalFS1Plan(t, single.Header, single.Blob), "")
	if err != nil {
		t.Fatalf("单语言计划应当自动选择语言: %v", err)
	}
	if loaded.sourceInfo().Language != "zh-CN" {
		t.Fatalf("自动选择的语言不符: %q", loaded.sourceInfo().Language)
	}
	// 没有计划时退回载荷。
	if _, err := wrapRecipeSource(nil, nil, ""); err == nil {
		t.Fatal("没有计划与载荷应当拒绝")
	}
}

func TestFS1PlanRejectsCorruption(t *testing.T) {
	files := planTestFiles()

	cases := []struct {
		name    string
		wantErr string
		mutate  func(*fs1PlanFixture)
	}{
		{name: "游戏不符", wantErr: "不能用于一代补丁", mutate: func(f *fs1PlanFixture) {
			f.Header.Game = "saga2"
		}},
		{name: "格式不符", wantErr: "不支持的安装计划格式", mutate: func(f *fs1PlanFixture) {
			f.Header.Format = 99
		}},
		{name: "没有语言", wantErr: "没有语言", mutate: func(f *fs1PlanFixture) {
			f.Header.Languages = nil
		}},
		{name: "语言重复", wantErr: "语言列表不合法", mutate: func(f *fs1PlanFixture) {
			f.Header.Languages = []string{"zh-CN", "zh-CN"}
		}},
		{name: "缺少版本", wantErr: "版本号", mutate: func(f *fs1PlanFixture) {
			f.Header.Versions["zh-CN"] = ""
		}},
		{name: "缺少语言数据", wantErr: "缺少 zh-CN 的数据", mutate: func(f *fs1PlanFixture) {
			delete(f.Header.LanguageData, "zh-CN")
		}},
		{name: "数据段哈希不符", wantErr: "数据段哈希不符", mutate: func(f *fs1PlanFixture) {
			f.Header.BinarySHA256 = strings.Repeat("0", 64)
		}},
		{name: "文件数量不符", wantErr: "目标文件数量不符", mutate: func(f *fs1PlanFixture) {
			delete(f.Header.LanguageData["zh-CN"].Files, "Texture.tex")
		}},
		{name: "未知文件", wantErr: "未知文件", mutate: func(f *fs1PlanFixture) {
			// 改名既有条目，保持数量一致，让拒绝发生在未知文件这一步。
			section := f.Header.LanguageData["zh-CN"].Files
			entry := section["Texture.tex"]
			delete(section, "Texture.tex")
			section["unknown.bin"] = entry
		}},
		{name: "路径不安全", wantErr: "路径不合法", mutate: func(f *fs1PlanFixture) {
			f.Header.Files[gameDiscImage].Path = "../FarlandSaga.bin"
		}},
		{name: "原版哈希不合法", wantErr: "原版哈希或大小不合法", mutate: func(f *fs1PlanFixture) {
			f.Header.Files[gameDiscImage].OriginalSHA256 = "xyz"
		}},
		{name: "文件类型不合法", wantErr: "不支持的文件类型", mutate: func(f *fs1PlanFixture) {
			f.Header.Files[gameDiscImage].Kind = "story"
		}},
		{name: "记录越界", wantErr: "越过数据段", mutate: func(f *fs1PlanFixture) {
			f.Header.LanguageData["zh-CN"].Files["Texture.tex"].RecordsLength = int64(len(f.Blob)) + 8
		}},
		{name: "补丁后哈希不合法", wantErr: "补丁后哈希或大小不合法", mutate: func(f *fs1PlanFixture) {
			f.Header.LanguageData["zh-CN"].Files["Texture.tex"].PatchedSHA256 = strings.Repeat("g", 64)
		}},
		{name: "记录乱序", wantErr: "记录范围非法", mutate: func(f *fs1PlanFixture) {
			// 把数据段里的第一条记录替换成回退偏移，并同步数据段哈希，
			// 让拒绝确实发生在记录校验这一步。
			offset := f.Header.LanguageData["zh-CN"].Files[gameDiscImage].RecordsOffset
			binary.LittleEndian.PutUint64(f.Blob[offset:offset+8], ^uint64(0)-8)
			f.Header.BinarySHA256 = sha256Hex(f.Blob)
		}},
		{name: "记录越过补丁后大小", wantErr: "记录范围非法", mutate: func(f *fs1PlanFixture) {
			f.Header.LanguageData["zh-CN"].Files["Texture.tex"].PatchedSize = 4
		}},
		{name: "数据段截断", wantErr: "数据段哈希不符", mutate: func(f *fs1PlanFixture) {
			f.Blob = f.Blob[:len(f.Blob)-3]
		}},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			fixture := buildFS1PlanFixture(t, []string{"zh-CN"}, nil, files)
			testCase.mutate(&fixture)
			planBytes := marshalFS1Plan(t, fixture.Header, fixture.Blob)
			_, err := loadFS1Plan(planBytes)
			if err == nil {
				t.Fatalf("期望拒绝，但解析成功")
			}
			if !strings.Contains(err.Error(), testCase.wantErr) {
				t.Fatalf("错误信息不含 %q: %v", testCase.wantErr, err)
			}
		})
	}

	t.Run("容器外壳损坏", func(t *testing.T) {
		fixture := buildFS1PlanFixture(t, []string{"zh-CN"}, nil, files)
		planBytes := marshalFS1Plan(t, fixture.Header, fixture.Blob)
		if _, err := loadFS1Plan([]byte("FS2PLAN2\n\x01\x00\x00\x00{}")); err == nil {
			t.Fatal("缺少 FS1PLAN1 标记应当拒绝")
		}
		broken := append([]byte(nil), planBytes...)
		binary.LittleEndian.PutUint32(broken[len(fs1PlanMagic):len(fs1PlanMagic)+4], 1<<30)
		if _, err := loadFS1Plan(broken); err == nil {
			t.Fatal("头部长度越界应当拒绝")
		}
		if _, err := loadFS1Plan(planBytes[:len(fs1PlanMagic)+2]); err == nil {
			t.Fatal("容器截断应当拒绝")
		}
	})
}

func TestFS1PlanReplayRejectsShortBase(t *testing.T) {
	files := planTestFiles()
	fixture := buildFS1PlanFixture(t, []string{"zh-CN"}, nil, files)
	plan := loadTestFS1Plan(t, marshalFS1Plan(t, fixture.Header, fixture.Blob))
	loaded, err := newFS1PlanPayload(plan, "zh-CN")
	if err != nil {
		t.Fatal(err)
	}
	spec := files[0]
	entry := loaded.info.Files[0]
	base := filepath.Join(t.TempDir(), "base.bin")
	// 原版比记录声称的基准短：回放必须在补空档时拒绝。
	if err := os.WriteFile(base, spec.original[:len(spec.original)/2], 0o644); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	if err := loaded.replayEntry(entry, base, "", &output, nil); err == nil {
		t.Fatal("原版基准过短时应当拒绝")
	}
}

// buildOpsPlan 构造一份带声明式操作的合成计划：一个 binary 目标文件，
// 其保留区内是 PackLZ 出的原版字体，另有一处普通补丁。
func buildOpsPlan(t *testing.T) (planBytes []byte, original []byte, patched []byte) {
	t.Helper()
	const (
		fontStart = 0x100
		fontLimit = 0x900
		cells     = 4
		patchOff  = 0x10
	)
	origMono := pattern(21, cells*32)
	newGlyph := pattern(23, 32)
	fontRegion := func(mono []byte) []byte {
		region := make([]byte, fontLimit-fontStart)
		packed, err := PackLZ(rgbaFromMono(mono))
		if err != nil {
			t.Fatal(err)
		}
		if len(packed) > len(region) {
			t.Fatal("合成字体压缩后超过保留区")
		}
		copy(region, packed)
		return region
	}
	original = make([]byte, 0x1000)
	copy(original[fontStart:], fontRegion(origMono))
	patched = append([]byte(nil), original...)
	copy(patched[patchOff:], pattern(22, 16))
	patchedMono := append([]byte(nil), origMono...)
	copy(patchedMono[32:64], newGlyph)
	copy(patched[fontStart:], fontRegion(patchedMono))

	fontOp := fs1PlanOp{Type: "font", Name: "overlay_font", Start: fontStart,
		Limit: fontLimit, Cells: cells, Slots: []int{1}, GlyphsOffset: 0}
	patchOp := fs1PlanOp{Type: "patch", Name: "test-patch", Offset: patchOff,
		DataOffset: int64(len(newGlyph)), DataLength: 16}
	opData := append(append([]byte(nil), newGlyph...), pattern(22, 16)...)
	header := fs1PlanHeader{
		Format:    fs1PlanFormatOps,
		Game:      "saga1",
		Languages: []string{"zh-CN"},
		Versions:  map[string]string{"zh-CN": "zh-CN-ops01"},
		Files: map[string]*fs1PlanFile{
			gameExecutable: {Kind: "binary", Path: gameExecutable,
				OriginalSHA256: sha256Hex(original), Size: int64(len(original))},
		},
		LanguageData: map[string]*fs1PlanLanguage{
			"zh-CN": {Version: "zh-CN-ops01", Files: map[string]*fs1PlanEntry{
				gameExecutable: {PatchedSHA256: sha256Hex(patched),
					PatchedSize: int64(len(patched)),
					Ops:         []fs1PlanOp{fontOp, patchOp}},
			}},
		},
		BinarySHA256: sha256Hex(opData),
	}
	return marshalFS1Plan(t, header, opData), original, patched
}

func TestFS1PlanOpsInstall(t *testing.T) {
	planBytes, original, patched := buildOpsPlan(t)
	plan := loadTestFS1Plan(t, planBytes)
	loaded, err := newFS1PlanPayload(plan, "zh-CN")
	if err != nil {
		t.Fatal(err)
	}
	base := filepath.Join(t.TempDir(), "FarlandSaga.exe")
	if err := os.WriteFile(base, original, 0o644); err != nil {
		t.Fatal(err)
	}
	entry := loaded.info.Files[0]
	var output bytes.Buffer
	if err := loaded.replayEntry(entry, base, "", &output, nil); err != nil {
		t.Fatalf("ops 回放失败: %v", err)
	}
	if !bytes.Equal(output.Bytes(), patched) {
		t.Fatal("ops 生成结果与预期补丁内容不一致")
	}

	// 完整安装往返：ops 文件参与备份、替换与恢复。
	files := []testFile{{name: gameExecutable, original: original, patched: patched}}
	game := buildGame(t, files)
	result, err := installPatch(game, loaded, nil)
	if err != nil {
		t.Fatalf("ops 安装失败: %v", err)
	}
	data, err := os.ReadFile(filepath.Join(game, gameExecutable))
	if err != nil || !bytes.Equal(data, patched) {
		t.Fatal("ops 安装后内容不符")
	}
	if _, err := restoreOriginal(game); err != nil {
		t.Fatalf("ops 恢复失败: %v", err)
	}
	_ = result
}

func TestFS1PlanOpsRejectsCorruption(t *testing.T) {
	planBytes, original, _ := buildOpsPlan(t)
	base := filepath.Join(t.TempDir(), "FarlandSaga.exe")
	if err := os.WriteFile(base, original, 0o644); err != nil {
		t.Fatal(err)
	}

	plan := loadTestFS1Plan(t, planBytes)
	loaded, err := newFS1PlanPayload(plan, "zh-CN")
	if err != nil {
		t.Fatal(err)
	}
	entry := loaded.info.Files[0]

	// 原版被改写：生成必须在身份核对处拒绝。
	tampered := append([]byte(nil), original...)
	tampered[0x200] ^= 0xFF
	tamperedPath := filepath.Join(t.TempDir(), "tampered.exe")
	if err := os.WriteFile(tamperedPath, tampered, 0o644); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	if err := loaded.replayEntry(entry, tamperedPath, "", &output, nil); err == nil {
		t.Fatal("原版身份不符时应当拒绝")
	}

	// 格式 1 携带 ops：解析阶段拒绝。先摘掉该条目的记录流，
	// 避免先触发记录与操作互斥的检查。
	files := planTestFiles()
	fixture := buildFS1PlanFixture(t, []string{"zh-CN"}, nil, files)
	fixture.Header.Format = fs1PlanFormat
	discEntry := fixture.Header.LanguageData["zh-CN"].Files[gameDiscImage]
	discEntry.RecordsOffset = 0
	discEntry.RecordsLength = 0
	discEntry.Ops = []fs1PlanOp{
		{Type: "patch", Name: "x", Offset: 0, DataOffset: 0, DataLength: 4}}
	badBytes := marshalFS1Plan(t, fixture.Header, fixture.Blob)
	if _, err := loadFS1Plan(badBytes); err == nil {
		t.Fatal("格式 1 携带 ops 应当拒绝")
	} else if !strings.Contains(err.Error(), "格式 1") {
		t.Fatalf("错误信息不符: %v", err)
	}

	// ops 区间重叠：解析阶段拒绝。
	opsPlan := loadTestFS1Plan(t, planBytes)
	opsEntry := opsPlan.entries["zh-CN"][gameExecutable]
	// buildOpsPlan 的保留区为 [0x100,0x900)：把补丁挪进保留区制造重叠。
	opsEntry.Ops[1].Offset = 0x900 - 8
	overlapHeader := opsPlan.header
	overlapHeader.BinarySHA256 = sha256Hex(opsPlan.blob)
	overlapBytes := marshalFS1Plan(t, overlapHeader, opsPlan.blob)
	if _, err := loadFS1Plan(overlapBytes); err == nil {
		t.Fatal("重叠 ops 应当拒绝")
	} else if !strings.Contains(err.Error(), "重叠") {
		t.Fatalf("错误信息不符: %v", err)
	}

	// 槽位乱序：解析阶段拒绝。
	opsPlan2 := loadTestFS1Plan(t, planBytes)
	opsEntry2 := opsPlan2.entries["zh-CN"][gameExecutable]
	opsEntry2.Ops[0].Slots = []int{3, 1}
	reordered := opsPlan2.header
	reordered.BinarySHA256 = sha256Hex(opsPlan2.blob)
	if _, err := loadFS1Plan(marshalFS1Plan(t, reordered, opsPlan2.blob)); err == nil {
		t.Fatal("槽位乱序应当拒绝")
	}

	_ = entry
}
