// 补丁载荷的读取与差异回放。
//
// ZIP 载荷由 fls.storage.package_release 写出，与 version_storage 的差异对象
// 格式一致：ZIP 内一个 manifest.json 描述每个目标文件的原版/补丁身份，
// objects/<sha256>.gz 是相对原版的差异块（gzip：一行 JSON 头 + 连续的
// <偏移 u64, 长度 u32> 记录与块数据）。回放时空隙从原版文件读取，重建结果的
// SHA-256 必须等于对象名，任何不符都视为损坏。发布构建不再内嵌 ZIP：两个
// 作品都安装编译期生成的 plan——一代 plan 由 plan_saga1.go 回放，二代 recipe
// plan 由 recipe_source.go 从用户原版现场生成，Format 3 历史载荷仍由
// payload_saga2.go 支持。ZIP 载荷保留给 --payload 外部排障包。
package main

import (
	"archive/zip"
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
	"strings"
)

const (
	payloadFormat       = 1
	payloadKind         = "farland-saga-patch"
	manifestName        = "manifest.json"
	objectPrefix        = "objects/"
	blockSize           = 4096
	sectorPayload       = 2048 // MODE1 数据载荷与 SSF 加密块的大小
	sectorSize          = 2352 // 原始 MODE1 扇区大小
	objectFormatChunked = 2
	recordSize          = 12 // struct.pack('<QI')：偏移 u64 小端 + 长度 u32 小端
	maxHeaderLine       = 65536
	readChunk           = 1024 * 1024
	maxManifest         = 4 * 1024 * 1024
)

// supportedPayloadFormat 报告当前作品是否接受某个 manifest 格式。格式清单
// 按作品定义在 game_saga1.go / game_saga2.go。
func supportedPayloadFormat(format int) bool {
	for _, accepted := range payloadFormats {
		if format == accepted {
			return true
		}
	}
	return false
}

// fileEntry 是 manifest 中一个目标文件的身份与差异对象引用。
type fileEntry struct {
	Path           string `json:"path"`
	OriginalSHA256 string `json:"original_sha256"`
	OriginalSize   int64  `json:"original_size"`
	PatchedSHA256  string `json:"patched_sha256"`
	PatchedSize    int64  `json:"patched_size"`
	Object         string `json:"object,omitempty"`
	SectorsObject  string `json:"sectors_object,omitempty"`
	SectorCount    int    `json:"sector_count,omitempty"`
}

type manifest struct {
	Format     int         `json:"format"`
	Kind       string      `json:"kind"`
	Version    string      `json:"version"`
	Language   string      `json:"language"`
	Game       string      `json:"game"`
	CreatedUTC string      `json:"created_utc"`
	Files      []fileEntry `json:"files"`
}

// objectHeader 对应 version_storage 差异对象的第一行 JSON。
type objectHeader struct {
	Format int    `json:"format"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
	Base   *struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
		Size   int64  `json:"size"`
	} `json:"base"`
}

type payload struct {
	path string
	info *manifest
	// recipe is non-nil when this payload is one language of an embedded
	// install-time recipe plan rather than a ZIP.
	recipe recipeRunner
	// closer 在从磁盘路径打开时关闭底层文件句柄；从内存（go:embed）打开时
	// 为 nil，没有需要释放的资源。
	closer  io.Closer
	objects map[string]*zip.File
}

// patchSource 是安装器需要的补丁来源。*payload 直接回放预生成对象；
// *recipeSource 用用户的原版资源现场生成 recipe 覆盖的扇区。
type patchSource interface {
	sourceInfo() *manifest
	engineName() string
	replayEntry(entry fileEntry, basePath, exePath string, output io.Writer, progress func(int64)) error
	close() error
}

// recipeRunner is implemented by the saga2-only install-time generator and
// lets a *payload carry it without making the shared payload code depend on
// the saga2 build tag.
type recipeRunner interface {
	engineName() string
	patchedHash(path string) (string, bool)
	patchedSize(path string) (int64, bool)
	replayEntry(entry fileEntry, basePath, exePath string, output io.Writer, progress func(int64)) error
}

func (p *payload) sourceInfo() *manifest { return p.info }

func (p *payload) engineName() string {
	if p.recipe != nil {
		return p.recipe.engineName()
	}
	return "payload"
}

// patchedHash / patchedSize expose the identity of generated files whose
// bytes depend on the user's pristine resources.
func (p *payload) patchedHash(path string) (string, bool) {
	if p.recipe != nil {
		return p.recipe.patchedHash(path)
	}
	return "", false
}

func (p *payload) patchedSize(path string) (int64, bool) {
	if p.recipe != nil {
		return p.recipe.patchedSize(path)
	}
	return 0, false
}

// safeRelativePath 只接受 POSIX 相对路径：拒绝绝对路径、盘符、反斜杠、
// 空段与上级跳转，和安装器对游戏内路径的约束保持一致。
func safeRelativePath(name string) (string, error) {
	if name == "" || strings.ContainsAny(name, `\:`) || strings.HasPrefix(name, "/") {
		return "", fmt.Errorf("非法路径 %q", name)
	}
	for _, part := range strings.Split(name, "/") {
		if part == "" || part == "." || part == ".." {
			return "", fmt.Errorf("非法路径 %q", name)
		}
	}
	return name, nil
}

func validSHA256(value string) bool {
	if len(value) != 64 {
		return false
	}
	for _, char := range value {
		if (char < '0' || char > '9') && (char < 'a' || char > 'f') {
			return false
		}
	}
	return true
}

func parseManifest(data []byte) (*manifest, error) {
	var info manifest
	if err := json.Unmarshal(data, &info); err != nil {
		return nil, fmt.Errorf("%s 解析失败: %w", manifestName, err)
	}
	if !supportedPayloadFormat(info.Format) {
		return nil, fmt.Errorf("不支持的载荷格式 %d", info.Format)
	}
	if info.Kind != payloadKind {
		return nil, fmt.Errorf("不是本项目的补丁载荷: %q", info.Kind)
	}
	if info.Version == "" || info.Language == "" {
		return nil, errors.New("载荷缺少版本或语言")
	}
	if len(info.Files) == 0 {
		return nil, errors.New("载荷没有列出任何文件")
	}
	seen := make(map[string]bool, len(info.Files))
	for index := range info.Files {
		entry := &info.Files[index]
		name, err := safeRelativePath(entry.Path)
		if err != nil {
			return nil, err
		}
		entry.Path = name
		if seen[name] {
			return nil, fmt.Errorf("载荷重复列出文件 %s", name)
		}
		seen[name] = true
		if !validSHA256(entry.OriginalSHA256) || !validSHA256(entry.PatchedSHA256) {
			return nil, fmt.Errorf("文件 %s 的哈希不合法", name)
		}
		if entry.OriginalSize < 0 || entry.PatchedSize < 0 {
			return nil, fmt.Errorf("文件 %s 的大小不合法", name)
		}
		if entry.SectorsObject != "" {
			if !supportsSectors() {
				return nil, fmt.Errorf("本作品的补丁不支持扇区对象（文件 %s）", name)
			}
			sectorsObj, err := safeRelativePath(entry.SectorsObject)
			if err != nil {
				return nil, fmt.Errorf("文件 %s 的扇区对象名不合法", name)
			}
			entry.SectorsObject = sectorsObj
		} else {
			object, err := safeRelativePath(entry.Object)
			if err != nil {
				return nil, fmt.Errorf("文件 %s 的差异对象名不合法", name)
			}
			if !strings.HasPrefix(object, objectPrefix) {
				return nil, fmt.Errorf("差异对象必须位于 %s 下: %s", objectPrefix, object)
			}
			entry.Object = object
		}
	}
	return &info, nil
}

// openPayload 从磁盘路径打开载荷 ZIP，解析并校验 manifest，同时确认
// manifest 引用的差异对象都在包内。ZIP 成员的 CRC32 由 archive/zip 在读取
// 时逐块核对。
func openPayload(path string) (*payload, error) {
	reader, err := zip.OpenReader(path)
	if err != nil {
		return nil, fmt.Errorf("无法打开补丁载荷 %s: %w", path, err)
	}
	loaded, err := parsePayload(&reader.Reader)
	if err != nil {
		reader.Close()
		return nil, err
	}
	loaded.path = path
	loaded.closer = reader
	return loaded, nil
}

// openPayloadData 从内存中的 ZIP 数据打开载荷，供 go:embed 内嵌的补丁使用。
// 整包读进内存，省得把几十兆的临时文件写到用户磁盘上。
func openPayloadData(name string, data []byte) (*payload, error) {
	reader, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return nil, fmt.Errorf("无法读取内嵌补丁 %s: %w", name, err)
	}
	loaded, err := parsePayload(reader)
	if err != nil {
		return nil, err
	}
	loaded.path = name
	return loaded, nil
}

// parsePayload 解析并校验 manifest，并确认 manifest 引用的差异对象都在包内。
func parsePayload(reader *zip.Reader) (*payload, error) {
	result := &payload{objects: map[string]*zip.File{}}
	var manifestFile *zip.File
	for _, file := range reader.File {
		if file.FileInfo().IsDir() {
			continue
		}
		switch {
		case file.Name == manifestName:
			manifestFile = file
		case strings.HasPrefix(file.Name, objectPrefix) || strings.HasSuffix(file.Name, ".bin.gz"):
			result.objects[file.Name] = file
		}
	}
	if manifestFile == nil {
		return nil, fmt.Errorf("补丁载荷缺少 %s", manifestName)
	}
	stream, err := manifestFile.Open()
	if err != nil {
		return nil, fmt.Errorf("无法读取 %s: %w", manifestName, err)
	}
	defer stream.Close()
	data, err := io.ReadAll(io.LimitReader(stream, maxManifest+1))
	if err != nil {
		return nil, fmt.Errorf("无法读取 %s: %w", manifestName, err)
	}
	if len(data) > maxManifest {
		return nil, errors.New("manifest.json 过大")
	}
	info, err := parseManifest(data)
	if err != nil {
		return nil, err
	}
	result.info = info
	for _, entry := range info.Files {
		if entry.Object != "" && result.objects[entry.Object] == nil {
			return nil, fmt.Errorf("补丁载荷缺少差异对象 %s（文件 %s）", entry.Object, entry.Path)
		}
		if entry.SectorsObject != "" && result.objects[entry.SectorsObject] == nil {
			return nil, fmt.Errorf("补丁载荷缺少扇区对象 %s（文件 %s）", entry.SectorsObject, entry.Path)
		}
	}
	return result, nil
}

func (p *payload) close() error {
	if p.closer == nil {
		return nil
	}
	return p.closer.Close()
}

// readLine 读取一行；调用方负责检查换行结尾，与 version_storage 的头部校验一致。
func readLine(reader *bufio.Reader) (string, error) {
	var builder strings.Builder
	for {
		chunk, err := reader.ReadString('\n')
		builder.WriteString(chunk)
		if err != nil {
			if errors.Is(err, io.EOF) {
				return builder.String(), nil
			}
			return builder.String(), err
		}
		if builder.Len() > maxHeaderLine {
			return builder.String(), errors.New("差异对象头部行过长")
		}
		return builder.String(), nil
	}
}

// replay 把一个差异对象回放成完整文件：记录按顺序供给变更块，空隙从
// basePath（原版文件）读取，边写边哈希，最终与对象名逐项核对。
func (p *payload) replay(entry fileEntry, basePath string, output io.Writer, progress func(int64)) error {
	stream, err := p.objects[entry.Object].Open()
	if err != nil {
		return fmt.Errorf("无法读取差异对象 %s: %w", entry.Object, err)
	}
	defer stream.Close()
	compressed, err := gzip.NewReader(stream)
	if err != nil {
		return fmt.Errorf("差异对象 %s 不是有效的 gzip: %w", entry.Object, err)
	}
	reader := bufio.NewReaderSize(compressed, readChunk)
	line, err := readLine(reader)
	if err != nil {
		return fmt.Errorf("差异对象 %s 头部读取失败: %w", entry.Object, err)
	}
	if !strings.HasSuffix(line, "\n") {
		return fmt.Errorf("差异对象 %s 缺少头部行", entry.Object)
	}
	var header objectHeader
	if err := json.Unmarshal([]byte(line), &header); err != nil {
		return fmt.Errorf("差异对象 %s 头部解析失败: %w", entry.Object, err)
	}
	if (header.Format != payloadFormat && header.Format != objectFormatChunked) || header.SHA256 != entry.PatchedSHA256 || header.Size != entry.PatchedSize || header.Size < 0 {
		return fmt.Errorf("差异对象 %s 的身份与 manifest 不一致", entry.Object)
	}
	if header.Base != nil && (header.Base.SHA256 != entry.OriginalSHA256 || header.Base.Size != entry.OriginalSize) {
		return fmt.Errorf("差异对象 %s 的原版基准与 manifest 不一致", entry.Object)
	}
	maxRecord := int64(blockSize)
	if header.Format == objectFormatChunked {
		maxRecord = 64 * 1024 * 1024
	}
	// 差异对象声明了原版基准时空隙从基准补齐；未声明基准的对象必须覆盖
	// 整个文件。记录流的回放、长度与哈希核对由共用的 replayRecordStream
	// 完成，安装计划（plan.bin）走同一实现。
	if err := replayRecordStream(reader, -1, maxRecord, header.Base != nil,
		header.SHA256, header.Size, basePath, output, progress); err != nil {
		return fmt.Errorf("差异对象 %s: %w", entry.Object, err)
	}
	return nil
}

// replayRecordStream 把一段差异记录流回放到原版基准文件上：记录按顺序供给
// 变更块，空隙从 basePath 读取，边写边哈希，最终长度与哈希逐一核对。
// streamLength 小于零表示读到流结束；maxRecord 限定单块长度上限；allowBase
// 为假时空隙一律拒绝（声明无基准的差异对象必须覆盖整个文件）。
func replayRecordStream(records io.Reader, streamLength int64, maxRecord int64, allowBase bool,
	patchedSHA256 string, patchedSize int64, basePath string, output io.Writer, progress func(int64)) error {
	reader := bufio.NewReaderSize(records, readChunk)
	hash := sha256.New()
	var position int64
	write := func(data []byte) error {
		hash.Write(data)
		if output != nil {
			if _, err := output.Write(data); err != nil {
				return err
			}
		}
		position += int64(len(data))
		if progress != nil {
			progress(position)
		}
		return nil
	}
	// 基准文件惰性打开：记录流覆盖整个文件的条目（如 Texture.tex 整体重写）
	// 不需要原版文件在场。
	var base *os.File
	openBase := func() error {
		if base != nil {
			return nil
		}
		opened, err := os.Open(basePath)
		if err != nil {
			return fmt.Errorf("无法读取原版文件 %s: %w", basePath, err)
		}
		base = opened
		return nil
	}
	defer func() {
		if base != nil {
			base.Close()
		}
	}()
	gap := func(end int64) error {
		if end <= position {
			return nil
		}
		if !allowBase {
			return errors.New("完整差异对象缺少原版基准块")
		}
		if err := openBase(); err != nil {
			return err
		}
		// 写入差异记录时输出位置会越过基准文件的读取位置，每次补空档前
		// 都必须先把基准文件定位到输出位置，和 version_storage.replay 一致。
		if _, err := base.Seek(position, io.SeekStart); err != nil {
			return err
		}
		for position < end {
			buffer := make([]byte, min(readChunk, end-position))
			read, err := base.Read(buffer)
			if read > 0 {
				if err := write(buffer[:read]); err != nil {
					return err
				}
			}
			if read == 0 {
				if errors.Is(err, io.EOF) {
					return errors.New("原版文件比差异记录的基准短")
				}
				if err == nil {
					return errors.New("原版文件读取没有进展")
				}
				return err
			}
			if err != nil && !errors.Is(err, io.EOF) {
				return err
			}
		}
		return nil
	}
	record := make([]byte, recordSize)
	bounded := streamLength >= 0
	remaining := streamLength
	for {
		if bounded && remaining == 0 {
			break
		}
		if _, err := io.ReadFull(reader, record); err != nil {
			if errors.Is(err, io.EOF) && !bounded {
				break // 未定长流（差异对象）在记录流结尾正常结束
			}
			if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
				return errors.New("记录被截断")
			}
			return fmt.Errorf("差异记录读取失败: %w", err)
		}
		if bounded {
			remaining -= recordSize
		}
		offset := int64(binary.LittleEndian.Uint64(record[0:8]))
		size := int64(binary.LittleEndian.Uint32(record[8:12]))
		if offset < position || size == 0 || size > maxRecord || offset+size > patchedSize {
			return errors.New("记录范围非法")
		}
		if err := gap(offset); err != nil {
			return err
		}
		if bounded && remaining < size {
			return errors.New("记录被截断")
		}
		written, err := io.Copy(writerFunc(write), io.LimitReader(reader, size))
		if err != nil {
			return fmt.Errorf("数据块读取失败: %w", err)
		}
		if written != size {
			return errors.New("数据块被截断")
		}
		if bounded {
			remaining -= size
		}
	}
	// 记录流结束后的尾段全部来自原版基准。
	if err := gap(patchedSize); err != nil {
		return err
	}
	if position != patchedSize {
		return errors.New("差异记录重建长度不符")
	}
	// patchedSHA256 为空表示记录流只是中间态（盘片条目的记录流后面还有
	// 声明式操作），最终哈希由操作完成后的整体核对承担。
	if patchedSHA256 != "" && hex.EncodeToString(hash.Sum(nil)) != patchedSHA256 {
		return errors.New("差异记录重建哈希不符")
	}
	return nil
}

// writerFunc 把写函数适配成 io.Writer，供回放管线直接消费记录数据。
type writerFunc func(data []byte) error

func (fn writerFunc) Write(data []byte) (int, error) {
	if err := fn(data); err != nil {
		return 0, err
	}
	return len(data), nil
}
