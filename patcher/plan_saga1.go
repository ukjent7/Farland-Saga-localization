//go:build !saga2

// 一代安装计划（plan）容器与安装期回放。
//
// 发布形态与二代对齐：编译期把各语言差异载荷打包成单个 plan.bin 由 go:embed
// 内嵌，安装时不再读取 ZIP。plan 以「FS1PLAN1\n + u32 头部长度 + JSON 头 +
// gzip 数据」存储；数据段是每语言一段差异记录流，记录格式与差异对象一致
// （u64 小端偏移 + u32 小端长度 + 块数据，按偏移升序）。回放时从原版基准
// 文件流式重建，空隙读原版，结果与计划声明的补丁后哈希逐项核对。
package main

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
)

const (
	fs1PlanMagic  = "FS1PLAN1\n"
	fs1PlanFormat = 1
	// 格式 2 在记录流之外引入声明式操作（fs1PlanOp）：安装期从用户原版
	// 现场生成目标文件，计划只携带本地化自己的字节（译文与 Unifont 字形）。
	fs1PlanFormatOps = 2
	// 头部与数据段的读取上限。三语言数据段约 15 MiB，上限留出余量并
	// 限定畸形输入的解析成本，与二代 plan 的限额策略一致。
	maxFS1PlanHeader = 16 << 20
	maxFS1PlanBlob   = 256 << 20
)

// fs1PlanFile 描述一个目标文件的原版身份。kind 区分流式重建（disc，约
// 300 MiB）与整体重建（binary，几十 MiB 以内）；两者的回放机制相同。
type fs1PlanFile struct {
	Kind           string `json:"kind"`
	Path           string `json:"path"`
	OriginalSHA256 string `json:"original_sha256"`
	Size           int64  `json:"size"`
}

// fs1PlanEntry 是一个语言对一个目标文件的补丁身份与数据段引用。
// 记录流（records_*）与声明式操作（ops）二选一：记录流回放预计算的差异
// 块，操作在安装期从原版现场生成。
type fs1PlanEntry struct {
	PatchedSHA256 string      `json:"patched_sha256"`
	PatchedSize   int64       `json:"patched_size"`
	RecordsOffset int64       `json:"records_offset"`
	RecordsLength int64       `json:"records_length"`
	Ops           []fs1PlanOp `json:"ops,omitempty"`
}

// fs1PlanOp 是一条声明式操作。patch 把计划携带的本地化字节写到原版的指定
// 偏移；font 把 Unifont 派生的字形槽位叠加到原版 exe 内压缩的 overlay
// 字体上（先解包、替换槽位、重打包，输出必须落回保留区）。
type fs1PlanOp struct {
	Type         string `json:"type"`
	Name         string `json:"name"`
	Offset       int64  `json:"offset"`
	DataOffset   int64  `json:"data_offset"`
	DataLength   int64  `json:"data_length"`
	Start        int64  `json:"start"`
	Limit        int64  `json:"limit"`
	Cells        int    `json:"cells"`
	Slots        []int  `json:"slots"`
	GlyphsOffset int64  `json:"glyphs_offset"`
}

// span 返回操作写出的字节区间，供重叠校验使用。
func (op *fs1PlanOp) span() (int64, int64) {
	if op.Type == "font" {
		return op.Start, op.Limit
	}
	return op.Offset, op.Offset + op.DataLength
}

// fs1PlanLanguage 携带一个语言的版本号与全部目标文件的条目。
type fs1PlanLanguage struct {
	Version string                   `json:"version"`
	Files   map[string]*fs1PlanEntry `json:"files"`
}

type fs1PlanHeader struct {
	Format       int                         `json:"format"`
	Game         string                      `json:"game"`
	Languages    []string                    `json:"languages"`
	Versions     map[string]string           `json:"versions"`
	Files        map[string]*fs1PlanFile     `json:"files"`
	LanguageData map[string]*fs1PlanLanguage `json:"languages_data"`
	BinarySHA256 string                      `json:"binary_sha256"`
}

// fs1Plan 是解析并校验后的安装计划。加载阶段走完全部差异记录流，任何
// 截断、越界或乱序都在触碰游戏目录之前拒绝。
type fs1Plan struct {
	header    fs1PlanHeader
	languages []string
	versions  map[string]string
	files     map[string]*fs1PlanFile
	entries   map[string]map[string]*fs1PlanEntry
	blob      []byte
}

// loadFS1Plan 解析 plan 容器并做完整校验。
func loadFS1Plan(data []byte) (*fs1Plan, error) {
	if len(data) < len(fs1PlanMagic)+4 || string(data[:len(fs1PlanMagic)]) != fs1PlanMagic {
		return nil, errors.New("安装计划缺少 FS1PLAN1 标记")
	}
	headerLength := int(binary.LittleEndian.Uint32(data[len(fs1PlanMagic) : len(fs1PlanMagic)+4]))
	start := len(fs1PlanMagic) + 4
	if headerLength <= 0 || headerLength > maxFS1PlanHeader || start+headerLength > len(data) {
		return nil, errors.New("安装计划头部长度不合法")
	}
	var header fs1PlanHeader
	decoder := json.NewDecoder(bytes.NewReader(data[start : start+headerLength]))
	if err := decoder.Decode(&header); err != nil {
		return nil, fmt.Errorf("安装计划头部解析失败: %w", err)
	}
	if header.Format != fs1PlanFormat && header.Format != fs1PlanFormatOps {
		return nil, fmt.Errorf("不支持的安装计划格式 %d", header.Format)
	}
	if header.Game != "saga1" {
		return nil, fmt.Errorf("安装计划属于 %q，不能用于一代补丁", header.Game)
	}
	if len(header.Languages) == 0 {
		return nil, errors.New("安装计划没有语言")
	}
	seen := make(map[string]bool, len(header.Languages))
	for _, language := range header.Languages {
		if language == "" || seen[language] {
			return nil, fmt.Errorf("安装计划语言列表不合法: %q", language)
		}
		seen[language] = true
	}
	if header.Versions == nil || header.LanguageData == nil {
		return nil, errors.New("安装计划缺少版本或语言数据")
	}
	for _, language := range header.Languages {
		if header.Versions[language] == "" {
			return nil, fmt.Errorf("安装计划缺少 %s 的版本号", language)
		}
		if header.LanguageData[language] == nil {
			return nil, fmt.Errorf("安装计划缺少 %s 的数据", language)
		}
	}

	compressed := bytes.NewReader(data[start+headerLength:])
	reader, err := gzip.NewReader(compressed)
	if err != nil {
		return nil, fmt.Errorf("安装计划数据段不是有效的 gzip: %w", err)
	}
	blob, err := io.ReadAll(io.LimitReader(reader, maxFS1PlanBlob+1))
	closeErr := reader.Close()
	if err != nil {
		return nil, fmt.Errorf("安装计划数据段读取失败: %w", err)
	}
	if closeErr != nil {
		return nil, fmt.Errorf("安装计划数据段损坏: %w", closeErr)
	}
	if len(blob) > maxFS1PlanBlob {
		return nil, errors.New("安装计划数据段过大")
	}
	sum := sha256.Sum256(blob)
	if hex.EncodeToString(sum[:]) != header.BinarySHA256 {
		return nil, errors.New("安装计划数据段哈希不符")
	}

	if len(header.Files) == 0 {
		return nil, errors.New("安装计划没有目标文件")
	}
	for name, file := range header.Files {
		if err := validateFS1PlanFile(file); err != nil {
			return nil, fmt.Errorf("%s: %w", name, err)
		}
		if name != file.Path {
			return nil, fmt.Errorf("安装计划文件 %s 的 path 与键不一致", name)
		}
	}
	entries := make(map[string]map[string]*fs1PlanEntry, len(header.Languages))
	for _, language := range header.Languages {
		section := header.LanguageData[language]
		if section.Version == "" {
			return nil, fmt.Errorf("安装计划 %s 的版本号为空", language)
		}
		if len(section.Files) != len(header.Files) {
			return nil, fmt.Errorf("安装计划 %s 的目标文件数量不符", language)
		}
		perLanguage := make(map[string]*fs1PlanEntry, len(section.Files))
		for name, entry := range section.Files {
			file, ok := header.Files[name]
			if !ok {
				return nil, fmt.Errorf("安装计划 %s 列出了未知文件 %s", language, name)
			}
			if err := validateFS1PlanEntry(header.Format, file, entry, blob); err != nil {
				return nil, fmt.Errorf("%s/%s: %w", language, name, err)
			}
			perLanguage[name] = entry
		}
		entries[language] = perLanguage
	}
	return &fs1Plan{
		header:    header,
		languages: header.Languages,
		versions:  header.Versions,
		files:     header.Files,
		entries:   entries,
		blob:      blob,
	}, nil
}

func validateFS1PlanFile(file *fs1PlanFile) error {
	if file == nil {
		return errors.New("文件条目为空")
	}
	safe, err := safeRelativePath(file.Path)
	if err != nil || safe != file.Path {
		return fmt.Errorf("文件路径不合法: %q", file.Path)
	}
	if !validSHA256(file.OriginalSHA256) || file.Size <= 0 {
		return errors.New("原版哈希或大小不合法")
	}
	switch file.Kind {
	case "disc", "binary":
		return nil
	default:
		return fmt.Errorf("不支持的文件类型 %q", file.Kind)
	}
}

func validateFS1PlanEntry(format int, file *fs1PlanFile, entry *fs1PlanEntry, blob []byte) error {
	if entry == nil {
		return errors.New("条目为空")
	}
	if !validSHA256(entry.PatchedSHA256) || entry.PatchedSize <= 0 {
		return errors.New("补丁后哈希或大小不合法")
	}
	if entry.RecordsLength < 0 || entry.RecordsOffset < 0 ||
		entry.RecordsOffset > int64(len(blob)) ||
		entry.RecordsLength > int64(len(blob))-entry.RecordsOffset {
		return errors.New("差异记录范围越过数据段")
	}
	if entry.RecordsLength > 0 {
		if len(entry.Ops) > 0 {
			return errors.New("记录流与声明式操作不能同时使用")
		}
		if err := validateRecordStream(entry, blob); err != nil {
			return err
		}
	} else if len(entry.Ops) == 0 {
		return errors.New("条目既没有记录流也没有操作")
	}
	if len(entry.Ops) > 0 && format != fs1PlanFormatOps {
		return errors.New("格式 1 不支持声明式操作")
	}
	for index := range entry.Ops {
		if err := validateFS1PlanOp(format, file, &entry.Ops[index], entry, blob); err != nil {
			return fmt.Errorf("操作 %d (%s): %w", index, entry.Ops[index].Name, err)
		}
	}
	// 操作区间两两不得重叠：安装期按顺序原地应用，重叠意味着两次写入
	// 争夺同一段字节，结果顺序相关。
	spans := make([][2]int64, 0, len(entry.Ops))
	for index := range entry.Ops {
		low, high := entry.Ops[index].span()
		spans = append(spans, [2]int64{low, high})
	}
	for i := 0; i < len(spans); i++ {
		for j := i + 1; j < len(spans); j++ {
			if spans[i][0] < spans[j][1] && spans[j][0] < spans[i][1] {
				return fmt.Errorf("操作区间重叠: [%d,%d) 与 [%d,%d)",
					spans[i][0], spans[i][1], spans[j][0], spans[j][1])
			}
		}
	}
	return nil
}

func validateRecordStream(entry *fs1PlanEntry, blob []byte) error {
	if entry.RecordsLength < recordSize ||
		entry.RecordsOffset > int64(len(blob)) ||
		entry.RecordsLength > int64(len(blob))-entry.RecordsOffset {
		return errors.New("差异记录范围越过数据段")
	}
	// 差异记录流必须完整走通：偏移严格升序且落在补丁后文件内，块长度
	// 不超过差异对象的 4 KiB 上限，流结束时不多不少。
	reader := bufio.NewReader(bytes.NewReader(blob[entry.RecordsOffset : entry.RecordsOffset+entry.RecordsLength]))
	position := int64(0)
	remaining := entry.RecordsLength
	record := make([]byte, recordSize)
	for remaining > 0 {
		if _, err := io.ReadFull(reader, record); err != nil {
			return fmt.Errorf("差异记录流被截断（位置 %d）", position)
		}
		remaining -= recordSize
		offset := int64(binary.LittleEndian.Uint64(record[0:8]))
		length := int64(binary.LittleEndian.Uint32(record[8:12]))
		if length == 0 || length > blockSize || offset < position || offset+length > entry.PatchedSize {
			return fmt.Errorf("差异记录范围非法（偏移 %d，长度 %d）", offset, length)
		}
		if remaining < length {
			return errors.New("差异记录数据被截断")
		}
		if _, err := io.CopyN(io.Discard, reader, length); err != nil {
			return errors.New("差异记录数据被截断")
		}
		remaining -= length
		position = offset + length
	}
	return nil
}

func validateFS1PlanOp(format int, file *fs1PlanFile, op *fs1PlanOp, entry *fs1PlanEntry, blob []byte) error {
	if op.Name == "" {
		return errors.New("缺少名字")
	}
	switch op.Type {
	case "patch":
		if op.Offset < 0 || op.DataLength <= 0 ||
			op.Offset > entry.PatchedSize || op.DataLength > entry.PatchedSize-op.Offset {
			return errors.New("写出范围越过文件")
		}
		if op.DataOffset < 0 || op.DataOffset > int64(len(blob)) ||
			op.DataLength > int64(len(blob))-op.DataOffset {
			return errors.New("数据范围越过数据段")
		}
		if op.Start != 0 || op.Limit != 0 || op.Cells != 0 || op.Slots != nil || op.GlyphsOffset != 0 {
			return errors.New("patch 操作携带了多余字段")
		}
	case "font":
		if op.Start < 0 || op.Limit <= op.Start || op.Limit > file.Size {
			return errors.New("字体保留区越过文件")
		}
		if op.Cells <= 0 {
			return errors.New("字体槽位数不合法")
		}
		if len(op.Slots) == 0 {
			return errors.New("字体操作没有槽位")
		}
		previous := -1
		for _, slot := range op.Slots {
			if slot <= previous || slot >= op.Cells {
				return fmt.Errorf("槽位 %d 越界或乱序", slot)
			}
			previous = slot
		}
		glyphs := int64(len(op.Slots)) * 32
		if op.GlyphsOffset < 0 || op.GlyphsOffset > int64(len(blob)) || glyphs > int64(len(blob))-op.GlyphsOffset {
			return errors.New("字形数据范围越过数据段")
		}
		if op.Offset != 0 || op.DataOffset != 0 || op.DataLength != 0 {
			return errors.New("font 操作携带了多余字段")
		}
	default:
		return fmt.Errorf("不支持的操作类型 %q", op.Type)
	}
	return nil
}

// version 返回指定语言的候选版本号。
func (p *fs1Plan) version(language string) string { return p.versions[language] }

// newFS1PlanPayload 把计划的一个语言包装成安装器使用的 *payload。
// 文件列表与 ZIP 载荷的 manifest 同构，GUI 与命令行的既有流程直接可用。
func newFS1PlanPayload(plan *fs1Plan, language string) (*payload, error) {
	supported := false
	for _, candidate := range plan.languages {
		if candidate == language {
			supported = true
			break
		}
	}
	if !supported {
		return nil, fmt.Errorf("安装计划没有 %s（可用语言：%v）", language, plan.languages)
	}
	section := plan.entries[language]
	paths := make([]string, 0, len(section))
	for path := range section {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	files := make([]fileEntry, 0, len(paths))
	for _, path := range paths {
		file := plan.files[path]
		entry := section[path]
		files = append(files, fileEntry{
			Path:           path,
			OriginalSHA256: file.OriginalSHA256,
			OriginalSize:   file.Size,
			PatchedSHA256:  entry.PatchedSHA256,
			PatchedSize:    entry.PatchedSize,
		})
	}
	info := &manifest{
		Format:   fs1PlanFormat,
		Kind:     payloadKind,
		Version:  plan.version(language),
		Language: language,
		Game:     "saga1",
		Files:    files,
	}
	source := &fs1PlanSource{plan: plan, language: language, info: info}
	return &payload{path: "plan/saga1/plan.bin", info: info, recipe: source}, nil
}

// fs1PlanSource 是一个语言的计划安装源：差异记录已在加载阶段核对过范围，
// 回放只做流式重建与最终哈希核对。
type fs1PlanSource struct {
	plan     *fs1Plan
	language string
	info     *manifest
}

func (s *fs1PlanSource) sourceInfo() *manifest { return s.info }

func (s *fs1PlanSource) engineName() string { return "plan " + s.plan.version(s.language) }

func (s *fs1PlanSource) close() error { return nil }

func (s *fs1PlanSource) patchedHash(path string) (string, bool) {
	entry, ok := s.plan.entries[s.language][path]
	if !ok {
		return "", false
	}
	return entry.PatchedSHA256, true
}

func (s *fs1PlanSource) patchedSize(path string) (int64, bool) {
	entry, ok := s.plan.entries[s.language][path]
	if !ok {
		return 0, false
	}
	return entry.PatchedSize, true
}

func (s *fs1PlanSource) replayEntry(entry fileEntry, basePath string, _ string, output io.Writer, progress func(int64)) error {
	planEntry, ok := s.plan.entries[s.language][entry.Path]
	if !ok {
		return fmt.Errorf("安装计划没有 %s 的 %s 数据", s.language, entry.Path)
	}
	if len(planEntry.Ops) > 0 {
		data, err := s.generateFromOps(planEntry, entry, basePath)
		if err != nil {
			return err
		}
		if output != nil {
			if _, err := output.Write(data); err != nil {
				return fmt.Errorf("写入 %s 失败: %w", entry.Path, err)
			}
		}
		if progress != nil {
			progress(int64(len(data)))
		}
		return nil
	}
	return replayRecordStream(
		bytes.NewReader(s.plan.blob[planEntry.RecordsOffset:planEntry.RecordsOffset+planEntry.RecordsLength]),
		planEntry.RecordsLength, blockSize, true, entry.PatchedSHA256, entry.PatchedSize, basePath, output, progress)
}

// generateFromOps 从用户原版现场生成目标文件：核对原版身份，按序应用
// 声明式操作，最终长度与哈希必须与计划声明一致。
func (s *fs1PlanSource) generateFromOps(planEntry *fs1PlanEntry, entry fileEntry, basePath string) ([]byte, error) {
	original, err := os.ReadFile(basePath)
	if err != nil {
		return nil, fmt.Errorf("无法读取原版文件 %s: %w", basePath, err)
	}
	if int64(len(original)) != entry.OriginalSize {
		return nil, fmt.Errorf("原版 %s 大小不符：期望 %d，实际 %d", entry.Path, entry.OriginalSize, len(original))
	}
	if digestBytes(original) != entry.OriginalSHA256 {
		return nil, fmt.Errorf("原版 %s 哈希不符：期望 %s，实际 %s",
			entry.Path, entry.OriginalSHA256, digestBytes(original))
	}
	buffer := append([]byte(nil), original...)
	for index := range planEntry.Ops {
		op := &planEntry.Ops[index]
		switch op.Type {
		case "patch":
			copy(buffer[op.Offset:op.Offset+op.DataLength],
				s.plan.blob[op.DataOffset:op.DataOffset+op.DataLength])
		case "font":
			if err := applyFontOp(buffer, op, s.plan.blob); err != nil {
				return nil, err
			}
		default:
			return nil, fmt.Errorf("%s: 不支持的操作类型 %q", entry.Path, op.Type)
		}
	}
	if int64(len(buffer)) != planEntry.PatchedSize {
		return nil, fmt.Errorf("%s 生成大小不符：期望 %d，实际 %d", entry.Path, planEntry.PatchedSize, len(buffer))
	}
	if digestBytes(buffer) != planEntry.PatchedSHA256 {
		return nil, fmt.Errorf("%s 生成结果与计划声明的哈希不符", entry.Path)
	}
	return buffer, nil
}

// applyFontOp 把 Unifont 派生的字形槽位叠加到原版 exe 的 overlay 字体上：
// 解包 LZ 得到严格 0/255 的 RGBA 字体，收缩回 1bpp mono，替换声明的槽位，
// 重新展开、打包并写回保留区，尾部保持零填充。
func applyFontOp(buffer []byte, op *fs1PlanOp, blob []byte) error {
	rgba, err := UnpackLZ(buffer[op.Start:op.Limit])
	if err != nil {
		return fmt.Errorf("原版字体解包失败: %w", err)
	}
	if int64(len(rgba)) != int64(op.Cells)*1024 {
		return fmt.Errorf("原版字体展开大小不符: 期望 %d, 实际 %d", int64(op.Cells)*1024, len(rgba))
	}
	mono, err := monoFromRGBA(rgba)
	if err != nil {
		return err
	}
	for index, slot := range op.Slots {
		start := slot * 32
		copy(mono[start:start+32], blob[op.GlyphsOffset+int64(index)*32:])
	}
	packed, err := PackLZ(rgbaFromMono(mono))
	if err != nil {
		return err
	}
	if int64(len(packed)) > op.Limit-op.Start {
		return fmt.Errorf("重打包字体超过保留区: %d > %d", len(packed), op.Limit-op.Start)
	}
	copy(buffer[op.Start:op.Start+int64(len(packed))], packed)
	for i := op.Start + int64(len(packed)); i < op.Limit; i++ {
		buffer[i] = 0
	}
	return nil
}

// monoFromRGBA 把 expand_font 的输出收缩回 1bpp mono；字节组必须严格
// 0/255（原版字体即如此），任何中间值都视为数据损坏。
func monoFromRGBA(rgba []byte) ([]byte, error) {
	if len(rgba)%32 != 0 {
		return nil, errors.New("RGBA 字体长度不是 32 的倍数")
	}
	mono := make([]byte, len(rgba)/32)
	for off := range mono {
		var value byte
		for bit := 0; bit < 8; bit++ {
			base := off*32 + bit*4
			if rgba[base] != 0 || rgba[base+1] != 0 || rgba[base+2] != 0 {
				return nil, fmt.Errorf("RGBA 字体字节组 %d/%d 的 RGB 通道非零", off, bit)
			}
			switch rgba[base+3] {
			case 255:
				value |= 128 >> bit
			case 0:
			default:
				return nil, fmt.Errorf("RGBA 字体字节组 %d/%d 的 alpha 非严格 0/255", off, bit)
			}
		}
		mono[off] = value
	}
	return mono, nil
}

// rgbaFromMono 是 expand_font 的等价实现（Python 侧验证过字节一致）。
func rgbaFromMono(mono []byte) []byte {
	rgba := make([]byte, len(mono)*32)
	for off, value := range mono {
		for bit := 0; bit < 8; bit++ {
			if value&(128>>bit) != 0 {
				rgba[off*32+bit*4+3] = 255
			}
		}
	}
	return rgba
}

func digestBytes(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
