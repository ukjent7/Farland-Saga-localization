// 补丁载荷的读取与差异回放。
//
// 载荷格式由 tools/package_release.py 写入，与 version_storage 的差异对象
// 格式一致：ZIP 内一个 manifest.json 描述每个目标文件的原版/补丁身份，
// objects/<sha256>.gz 是相对原版的 4 KiB 差异块（gzip：一行 JSON 头 +
// 连续的 <偏移 u64, 长度 u32> 记录与块数据）。回放时空隙从原版文件读取，
// 重建结果的 SHA-256 必须等于对象名，任何不符都视为损坏。
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
	payloadFormat = 1
	payloadKind   = "farland-saga-patch"
	manifestName  = "manifest.json"
	objectPrefix  = "objects/"
	blockSize     = 4096
	recordSize    = 12 // struct.pack('<QI')：偏移 u64 小端 + 长度 u32 小端
	maxHeaderLine = 65536
	readChunk     = 1024 * 1024
	maxManifest   = 4 * 1024 * 1024
)

// fileEntry 是 manifest 中一个目标文件的身份与差异对象引用。
type fileEntry struct {
	Path           string `json:"path"`
	OriginalSHA256 string `json:"original_sha256"`
	OriginalSize   int64  `json:"original_size"`
	PatchedSHA256  string `json:"patched_sha256"`
	PatchedSize    int64  `json:"patched_size"`
	Object         string `json:"object"`
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
	// closer 在从磁盘路径打开时关闭底层文件句柄；从内存（go:embed）打开时
	// 为 nil，没有需要释放的资源。
	closer  io.Closer
	objects map[string]*zip.File
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
	if info.Format != payloadFormat {
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
		object, err := safeRelativePath(entry.Object)
		if err != nil {
			return nil, fmt.Errorf("文件 %s 的差异对象名不合法", name)
		}
		if !strings.HasPrefix(object, objectPrefix) {
			return nil, fmt.Errorf("差异对象必须位于 %s 下: %s", objectPrefix, object)
		}
		entry.Object = object
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
		case strings.HasPrefix(file.Name, objectPrefix):
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
		if result.objects[entry.Object] == nil {
			return nil, fmt.Errorf("补丁载荷缺少差异对象 %s（文件 %s）", entry.Object, entry.Path)
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
	if (header.Format != payloadFormat && header.Format != 2) || header.SHA256 != entry.PatchedSHA256 || header.Size != entry.PatchedSize || header.Size < 0 {
		return fmt.Errorf("差异对象 %s 的身份与 manifest 不一致", entry.Object)
	}
	if header.Base != nil && (header.Base.SHA256 != entry.OriginalSHA256 || header.Base.Size != entry.OriginalSize) {
		return fmt.Errorf("差异对象 %s 的原版基准与 manifest 不一致", entry.Object)
	}
	var base *os.File
	if header.Base != nil {
		opened, err := os.Open(basePath)
		if err != nil {
			return fmt.Errorf("无法读取原版文件 %s: %w", basePath, err)
		}
		defer opened.Close()
		base = opened
	}
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
	gap := func(end int64) error {
		if end > position && base == nil {
			return errors.New("完整差异对象缺少原版基准块")
		}
		if base != nil {
			// 写入差异记录时输出位置会越过基准文件的读取位置，每次补空档前
			// 都必须先把基准文件定位到输出位置，和 version_storage.replay 一致。
			if _, err := base.Seek(position, io.SeekStart); err != nil {
				return err
			}
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
					return errors.New("原版文件比差异对象基准短")
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
	for {
		if _, err := io.ReadFull(reader, record); err != nil {
			if errors.Is(err, io.EOF) {
				break
			}
			if errors.Is(err, io.ErrUnexpectedEOF) {
				return fmt.Errorf("差异对象 %s 的记录被截断", entry.Object)
			}
			return fmt.Errorf("差异对象 %s 读取失败: %w", entry.Object, err)
		}
		offset := int64(binary.LittleEndian.Uint64(record[0:8]))
		size := binary.LittleEndian.Uint32(record[8:12])
		maxRecordSize := uint32(blockSize)
		if header.Format == 2 {
			maxRecordSize = 64 * 1024 * 1024
		}
		if offset < position || size == 0 || size > maxRecordSize || offset+int64(size) > header.Size {
			return fmt.Errorf("差异对象 %s 的记录范围非法", entry.Object)
		}
		if err := gap(offset); err != nil {
			return err
		}
		remaining := int64(size)
		for remaining > 0 {
			chunkSize := int(min(readChunk, remaining))
			block := make([]byte, chunkSize)
			if _, err := io.ReadFull(reader, block); err != nil {
				return fmt.Errorf("差异对象 %s 的数据块被截断: %w", entry.Object, err)
			}
			if err := write(block); err != nil {
				return err
			}
			remaining -= int64(chunkSize)
		}
	}
	if err := gap(header.Size); err != nil {
		return err
	}
	if position != header.Size {
		return fmt.Errorf("差异对象 %s 重建长度不符", entry.Object)
	}
	if hex.EncodeToString(hash.Sum(nil)) != header.SHA256 {
		return fmt.Errorf("差异对象 %s 重建哈希不符", entry.Object)
	}
	return nil
}
