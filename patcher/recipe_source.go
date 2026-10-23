//go:build saga2

// Install-time resource generation. The recipe source reads the pristine
// resource from the user's backed-up disc image, applies the compiled mask
// plan, checks the expected hash, and turns the result into 2048-byte sectors.
// It then hands those sectors to the payload replay, which owns the SSF and
// MODE1 container work.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
)

type recipeSource struct {
	plan      *recipePlan
	language  string
	info      *manifest
	overrides map[uint32][]byte
	generated map[string][]byte
	outputs   map[string]recipeOutput
}

type recipeOutput struct {
	hash string
	size int64
}

// newRecipePayload presents one language of a compiled plan through the same
// payload interface the GUI, wizard, installer and status commands already
// use. The plan is self-contained: no ZIP or other prebuilt game data is
// needed.
func newRecipePayload(plan *recipePlan, language string) (*payload, error) {
	supported := false
	for _, candidate := range plan.Languages {
		if candidate == language {
			supported = true
			break
		}
	}
	if !supported {
		return nil, fmt.Errorf("recipe plan 没有 %s（可用语言：%v）", language, plan.Languages)
	}
	paths := make([]string, 0, len(plan.Files))
	for path := range plan.Files {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	files := make([]fileEntry, 0, len(paths))
	for _, path := range paths {
		file := plan.Files[path]
		patchedHash := file.PatchedSHA256
		patchedSize := file.PatchedSize
		if patchedHash == "" {
			patchedHash = file.OriginalSHA256
		}
		if patchedSize == 0 {
			patchedSize = file.Size
		}
		files = append(files, fileEntry{
			Path:           path,
			OriginalSHA256: file.OriginalSHA256,
			OriginalSize:   file.Size,
			PatchedSHA256:  patchedHash,
			PatchedSize:    patchedSize,
		})
	}
	info := &manifest{
		Format:   3,
		Kind:     payloadKind,
		Version:  plan.Version,
		Language: language,
		Game:     plan.Game,
		Files:    files,
	}
	source := &recipeSource{
		plan:      plan,
		language:  language,
		info:      info,
		generated: map[string][]byte{},
		outputs:   map[string]recipeOutput{},
	}
	return &payload{path: "recipe/plan.bin", info: info, recipe: source}, nil
}

func (s *recipeSource) sourceInfo() *manifest { return s.info }

func (s *recipeSource) engineName() string { return "recipe " + s.plan.Version }

func (s *recipeSource) close() error { return nil }

func (s *recipeSource) patchedHash(path string) (string, bool) {
	output, ok := s.outputs[path]
	return output.hash, ok
}

func (s *recipeSource) patchedSize(path string) (int64, bool) {
	output, ok := s.outputs[path]
	return output.size, ok
}

func (s *recipeSource) replayEntry(entry fileEntry, basePath, exePath string, output io.Writer, progress func(int64)) error {
	file := s.plan.Files[entry.Path]
	if file == nil {
		return fmt.Errorf("recipe plan 没有文件 %s", entry.Path)
	}
	switch file.Kind {
	case "disc":
		overrides, err := s.buildOverrides(entry, basePath, exePath)
		if err != nil {
			return err
		}
		hash := sha256.New()
		writer := io.Writer(hash)
		if output != nil {
			writer = io.MultiWriter(output, hash)
		}
		if err := replaySectorStream(entry, basePath, exePath, writer, progress, overrides, nil); err != nil {
			return err
		}
		s.outputs[entry.Path] = recipeOutput{hash: hex.EncodeToString(hash.Sum(nil)), size: entry.PatchedSize}
		return nil
	case "binary", "texture":
		data := s.generated[entry.Path]
		if data == nil {
			original, err := readOriginalFile(basePath, file)
			if err != nil {
				return err
			}
			if file.Kind == "binary" {
				data, err = applyRecipeBinary(file, original)
			} else {
				data, err = generateRecipeTexture(file, original, basePath, exePath, s.plan.blob, s.language)
			}
			if err != nil {
				return fmt.Errorf("生成 %s 失败: %w", entry.Path, err)
			}
			s.generated[entry.Path] = data
		}
		if output != nil {
			if _, err := output.Write(data); err != nil {
				return fmt.Errorf("写入 %s 失败: %w", entry.Path, err)
			}
		}
		if progress != nil {
			progress(int64(len(data)))
		}
		s.outputs[entry.Path] = recipeOutput{hash: digestBytes(data), size: int64(len(data))}
		return nil
	default:
		return fmt.Errorf("recipe plan 不支持文件类型 %q", file.Kind)
	}
}

func readOriginalFile(path string, file *recipeFile) ([]byte, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("无法读取原版 %s: %w", file.Path, err)
	}
	if int64(len(data)) != file.Size {
		return nil, fmt.Errorf("原版 %s 大小不符：期望 %d，实际 %d", file.Path, file.Size, len(data))
	}
	if digestBytes(data) != file.OriginalSHA256 {
		return nil, fmt.Errorf("原版 %s 哈希不符：期望 %s，实际 %s",
			file.Path, file.OriginalSHA256, digestBytes(data))
	}
	return data, nil
}

func applyRecipeBinary(file *recipeFile, original []byte) ([]byte, error) {
	out := append([]byte(nil), original...)
	for _, patch := range file.Patches {
		originalBytes, err := hex.DecodeString(patch.Original)
		if err != nil {
			return nil, fmt.Errorf("%s: 原版字节不是十六进制", patch.Name)
		}
		patchedBytes, err := hex.DecodeString(patch.Patched)
		if err != nil || len(patchedBytes) != len(originalBytes) {
			return nil, fmt.Errorf("%s: 补丁字节不合法", patch.Name)
		}
		start := int(patch.Offset)
		if start < 0 || start > len(out) || len(originalBytes) > len(out)-start {
			return nil, fmt.Errorf("%s: 补丁越过文件", patch.Name)
		}
		if !bytes.Equal(out[start:start+len(originalBytes)], originalBytes) {
			return nil, fmt.Errorf("%s: 原版字节不匹配", patch.Name)
		}
		copy(out[start:start+len(patchedBytes)], patchedBytes)
	}
	if int64(len(out)) != file.PatchedSize || digestBytes(out) != file.PatchedSHA256 {
		return nil, errors.New("可执行文件补丁结果与 recipe 声明不符")
	}
	return out, nil
}

// buildOverrides generates every recipe resource once and maps its bytes to
// the image's logical sectors. Generation is cached: a retry after a failed
// write does not repeat the expensive SSF reads.
func (s *recipeSource) buildOverrides(entry fileEntry, basePath, exePath string) (map[uint32][]byte, error) {
	if s.overrides != nil {
		return s.overrides, nil
	}
	overrides := make(map[uint32][]byte)
	for rid, resource := range s.plan.Resources {
		generated := s.generated[rid]
		if generated == nil {
			original, err := readOriginalResource(basePath, exePath, s.plan, resource)
			if err != nil {
				return nil, fmt.Errorf("读取原版 %s 失败: %w", rid, err)
			}
			generated, err = applyRecipeResource(rid, resource, s.plan.blob, s.language, original)
			if err != nil {
				return nil, fmt.Errorf("生成 %s 失败: %w", rid, err)
			}
			expected, err := s.plan.expectedHash(rid, s.language)
			if err != nil {
				return nil, err
			}
			if digestBytes(generated) != expected {
				return nil, fmt.Errorf("%s 生成结果哈希不符：期望 %s，实际 %s",
					rid, expected, digestBytes(generated))
			}
			if len(generated) != resource.Size {
				return nil, fmt.Errorf("%s 生成结果大小不符：期望 %d，实际 %d",
					rid, resource.Size, len(generated))
			}
			s.generated[rid] = generated
		}
		first := uint64(s.plan.ArchiveLBA) + uint64(resource.ArchiveOffset)/uint64(s.plan.SectorSize)
		for offset := 0; offset < resource.Size; offset += s.plan.SectorSize {
			lba := uint32(first + uint64(offset/s.plan.SectorSize))
			if int64(lba)*int64(s.plan.SectorSize) >= entry.PatchedSize-2048 {
				return nil, fmt.Errorf("%s: LBA %d 超出镜像范围", rid, lba)
			}
			if _, exists := overrides[lba]; exists {
				return nil, fmt.Errorf("%s: LBA %d 与另一个 recipe 资源重叠", rid, lba)
			}
			chunk := generated[offset : offset+s.plan.SectorSize]
			overrides[lba] = append([]byte(nil), chunk...)
		}
	}
	s.overrides = overrides
	return overrides, nil
}

func digestBytes(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// readOriginalResource pulls one resource out of the pristine container. The
// image stores a 2352-byte logical sector stream split across SSF-encrypted
// 2048-byte blocks; each sector is permuted inside its 64-sector group.
func readOriginalResource(imagePath, exePath string, plan *recipePlan, resource *recipeResource) ([]byte, error) {
	info, err := os.Stat(imagePath)
	if err != nil {
		return nil, fmt.Errorf("无法读取原版镜像 %s: %w", imagePath, err)
	}
	params, err := LoadSSFParameters(imagePath, exePath, filepath.Base(imagePath))
	if err != nil {
		return nil, fmt.Errorf("解析镜像 SSF 参数失败: %w", err)
	}
	tables, err := ExtractDiscTables(exePath)
	if err != nil {
		return nil, fmt.Errorf("提取光盘置换表失败: %w", err)
	}
	file, err := os.Open(imagePath)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	result := make([]byte, 0, resource.Size)
	first := int64(plan.ArchiveLBA) + resource.ArchiveOffset/int64(plan.SectorSize)
	for offset := 0; offset < resource.Size; offset += plan.SectorSize {
		lba := first + int64(offset/plan.SectorSize)
		permIndex := 2*283840 + int(lba)
		if permIndex < 0 || permIndex >= len(tables.Permutation) {
			return nil, fmt.Errorf("LBA %d 的置换索引越界", lba)
		}
		physical := (lba & ^63) + int64(tables.Permutation[permIndex])
		cipher, err := readStreamRange(file, params, physical*2352, 2352, info.Size())
		if err != nil {
			return nil, fmt.Errorf("读取 LBA %d 失败: %w", lba, err)
		}
		plain := SectorTransform(cipher, int(lba), tables.Dwords, tables.Qwords, false)
		result = append(result, plain[16:2064]...)
	}
	if len(result) != resource.Size {
		return nil, fmt.Errorf("原版资源大小不符：期望 %d，实际 %d", resource.Size, len(result))
	}
	if digestBytes(result) != resource.OriginalSHA256 {
		return nil, fmt.Errorf("原版资源哈希不符：期望 %s，实际 %s",
			resource.OriginalSHA256, digestBytes(result))
	}
	return result, nil
}

// readStreamRange reads a range of the decrypted 2352-byte logical stream.
// The stream starts after the image's 2048-byte container header and is
// stored in 2048-byte SSF blocks, so a 2352-byte sector can span two blocks.
func readStreamRange(file *os.File, params *SSFParameters, start, length int64, fileSize int64) ([]byte, error) {
	if start < 0 || length <= 0 || start+length > fileSize-2048 {
		return nil, errors.New("读取范围超出镜像数据区")
	}
	result := make([]byte, length)
	written := int64(0)
	cursor := start
	for written < length {
		base := cursor / 2048 * 2048
		within := cursor - base
		count := length - written
		if count > 2048-within {
			count = 2048 - within
		}
		block := make([]byte, 2048)
		if _, err := file.ReadAt(block, 2048+base); err != nil {
			return nil, err
		}
		plain := TransformSSF(block, params, false)
		copy(result[written:], plain[within:within+count])
		written += count
		cursor += count
	}
	return result, nil
}
