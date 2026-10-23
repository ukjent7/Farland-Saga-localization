//go:build saga2

// Install-time resource generation. The recipe source reads the pristine
// resource from the user's backed-up disc image, applies the compiled mask
// plan, checks the expected hash, and turns the result into 2048-byte sectors.
// It then hands those sectors to the payload replay, which owns the SSF and
// MODE1 container work.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

type recipeSource struct {
	payload   *payload
	plan      *recipePlan
	language  string
	info      *manifest
	overrides map[uint32][]byte
	generated map[string][]byte
}

func newRecipeSource(loaded *payload, plan *recipePlan, language string) (*recipeSource, error) {
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
	discFound := false
	for _, entry := range loaded.info.Files {
		if entry.Path == gameDiscImage {
			discFound = true
			if entry.SectorsObject == "" {
				return nil, errors.New("recipe 生成需要 Format 3 扇区载荷")
			}
			break
		}
	}
	if !discFound {
		return nil, fmt.Errorf("补丁载荷没有列出 %s", gameDiscImage)
	}
	synthetic := *loaded.info
	synthetic.Files = append([]fileEntry(nil), loaded.info.Files...)
	return &recipeSource{
		payload:   loaded,
		plan:      plan,
		language:  language,
		info:      &synthetic,
		generated: map[string][]byte{},
	}, nil
}

func (s *recipeSource) sourceInfo() *manifest { return s.info }

func (s *recipeSource) engineName() string { return "recipe " + s.plan.Version }

func (s *recipeSource) close() error { return s.payload.close() }

func (s *recipeSource) replayEntry(entry fileEntry, basePath, exePath string, output io.Writer, progress func(int64)) error {
	if entry.Path != gameDiscImage {
		return s.payload.replayEntry(entry, basePath, exePath, output, progress)
	}
	if entry.SectorsObject == "" {
		return errors.New("recipe 生成需要 Format 3 扇区载荷")
	}
	overrides, err := s.buildOverrides(entry, basePath, exePath)
	if err != nil {
		return err
	}
	return s.payload.replaySectorsMerged(entry, basePath, exePath, output, progress, overrides)
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
