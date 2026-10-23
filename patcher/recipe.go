//go:build saga2

// Recipe plans carry the localisation's own text masks, never game pixels.
// The plan is compiled by tools/compile_recipe.py and embedded in the patcher;
// the installer composites it onto the user's pristine resources.
package main

import (
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

const (
	recipeMagic       = "FS2RECIPE1\n"
	recipePlanFormat  = 1
	maxRecipeHeader   = 1 << 20
	maxRecipeMaskData = 16 << 20
	maxRecipeCells    = 1 << 20
)

type recipePlan struct {
	Format     int
	Version    string
	Game       string
	Languages  []string
	ArchiveLBA int
	SectorSize int
	Resources  map[string]*recipeResource
	blob       []byte
}

type recipeResource struct {
	Format         string                       `json:"format"`
	AtlasSize      []int                        `json:"atlas_size"`
	ArchiveOffset  int64                        `json:"archive_offset"`
	Size           int                          `json:"size"`
	OriginalSHA256 string                       `json:"original_sha256"`
	Expected       map[string]string            `json:"expected"`
	Operations     map[string][]recipeOperation `json:"operations"`
}

type recipeOperation struct {
	Type   string        `json:"type"`
	Labels []recipeLabel `json:"labels"`
}

type recipeLabel struct {
	Name       string `json:"name"`
	Box        [4]int `json:"box"`
	Erase      string `json:"erase"`
	MaskOffset int    `json:"mask_offset"`
	MaskLength int    `json:"mask_length"`
	Body       int    `json:"body"`
	Outline    int    `json:"outline"`
}

type recipeHeader struct {
	Format       int                        `json:"format"`
	Version      string                     `json:"version"`
	Game         string                     `json:"game"`
	Languages    []string                   `json:"languages"`
	ArchiveLBA   int                        `json:"archive_lba"`
	SectorSize   int                        `json:"sector_size"`
	Resources    map[string]*recipeResource `json:"resources"`
	BinarySHA256 string                     `json:"binary_sha256"`
}

// loadRecipePlan parses the plan container and validates everything the
// installer will use. A truncated mask, an unknown operation, or a missing
// expected hash is rejected here, before the game directory is touched.
func loadRecipePlan(data []byte) (*recipePlan, error) {
	if len(data) < len(recipeMagic)+4 || string(data[:len(recipeMagic)]) != recipeMagic {
		return nil, errors.New("recipe plan 缺少 FS2RECIPE1 标记")
	}
	headerLength := int(binary.LittleEndian.Uint32(data[len(recipeMagic) : len(recipeMagic)+4]))
	start := len(recipeMagic) + 4
	if headerLength <= 0 || headerLength > maxRecipeHeader || start+headerLength > len(data) {
		return nil, errors.New("recipe plan 头部长度不合法")
	}
	var header recipeHeader
	decoder := json.NewDecoder(bytes.NewReader(data[start : start+headerLength]))
	if err := decoder.Decode(&header); err != nil {
		return nil, fmt.Errorf("recipe plan 头部解析失败: %w", err)
	}
	if header.Format != recipePlanFormat {
		return nil, fmt.Errorf("不支持的 recipe plan 格式 %d", header.Format)
	}
	if header.Version == "" || header.Game == "" || len(header.Languages) == 0 {
		return nil, errors.New("recipe plan 缺少版本、游戏或语言")
	}
	if header.SectorSize <= 0 || header.ArchiveLBA < 0 {
		return nil, errors.New("recipe plan 的扇区参数不合法")
	}
	if len(header.Resources) == 0 {
		return nil, errors.New("recipe plan 没有资源")
	}
	seenLanguages := make(map[string]bool, len(header.Languages))
	for _, language := range header.Languages {
		if language == "" || seenLanguages[language] {
			return nil, fmt.Errorf("recipe plan 语言列表不合法: %q", language)
		}
		seenLanguages[language] = true
	}

	compressed := bytes.NewReader(data[start+headerLength:])
	reader, err := gzip.NewReader(compressed)
	if err != nil {
		return nil, fmt.Errorf("recipe plan 的 mask 数据不是 gzip: %w", err)
	}
	blob, err := io.ReadAll(io.LimitReader(reader, maxRecipeMaskData+1))
	closeErr := reader.Close()
	if err != nil {
		return nil, fmt.Errorf("recipe plan 的 mask 数据读取失败: %w", err)
	}
	if closeErr != nil {
		return nil, fmt.Errorf("recipe plan 的 mask 数据损坏: %w", closeErr)
	}
	if len(blob) > maxRecipeMaskData {
		return nil, errors.New("recipe plan 的 mask 数据过大")
	}
	sum := sha256.Sum256(blob)
	if hex.EncodeToString(sum[:]) != header.BinarySHA256 {
		return nil, errors.New("recipe plan 的 mask 数据哈希不符")
	}

	plan := &recipePlan{
		Format:     header.Format,
		Version:    header.Version,
		Game:       header.Game,
		Languages:  header.Languages,
		ArchiveLBA: header.ArchiveLBA,
		SectorSize: header.SectorSize,
		Resources:  header.Resources,
		blob:       blob,
	}
	if plan.Game != "saga2" {
		return nil, fmt.Errorf("recipe plan 属于 %q，不能用于二代补丁", plan.Game)
	}
	for rid, resource := range plan.Resources {
		if err := validateRecipeResource(rid, resource, plan, seenLanguages); err != nil {
			return nil, err
		}
	}
	return plan, nil
}

func validResourceID(rid string) bool {
	if len(rid) != 4 {
		return false
	}
	for _, char := range rid {
		if (char < '0' || char > '9') && (char < 'A' || char > 'F') {
			return false
		}
	}
	return true
}

func validateRecipeResource(rid string, resource *recipeResource, plan *recipePlan, languages map[string]bool) error {
	if !validResourceID(rid) {
		return fmt.Errorf("recipe plan 资源 ID 不合法: %q", rid)
	}
	if resource == nil {
		return fmt.Errorf("recipe plan 资源 %s 为空", rid)
	}
	if resource.Format != "tile_atlas_8bpp" {
		return fmt.Errorf("%s: 不支持的 recipe 资源格式 %q", rid, resource.Format)
	}
	if len(resource.AtlasSize) != 2 || resource.AtlasSize[0] <= 0 || resource.AtlasSize[1] <= 0 ||
		resource.AtlasSize[0] > 8192 || resource.AtlasSize[1] > 8192 ||
		resource.AtlasSize[0]%8 != 0 || resource.AtlasSize[1]%8 != 0 {
		return fmt.Errorf("%s: atlas_size 不合法", rid)
	}
	if resource.ArchiveOffset < 0 || resource.ArchiveOffset%int64(plan.SectorSize) != 0 {
		return fmt.Errorf("%s: archive_offset 未按扇区对齐", rid)
	}
	if resource.Size <= 0 || resource.Size%plan.SectorSize != 0 {
		return fmt.Errorf("%s: 资源大小必须是扇区大小的正整数倍", rid)
	}
	if !validSHA256(resource.OriginalSHA256) {
		return fmt.Errorf("%s: original_sha256 不合法", rid)
	}
	for _, language := range plan.Languages {
		if !validSHA256(resource.Expected[language]) {
			return fmt.Errorf("%s: 缺少 %s 的期望哈希", rid, language)
		}
		operations, ok := resource.Operations[language]
		if !ok || len(operations) != 1 {
			return fmt.Errorf("%s: %s 必须恰好有一个操作", rid, language)
		}
		operation := operations[0]
		if operation.Type != "tile_atlas" {
			return fmt.Errorf("%s: %s 的操作类型 %q 不受支持", rid, language, operation.Type)
		}
		if len(operation.Labels) == 0 {
			return fmt.Errorf("%s: %s 没有 label", rid, language)
		}
		for _, label := range operation.Labels {
			if label.Name == "" {
				return fmt.Errorf("%s: label 缺少名字", rid)
			}
			x0, y0, width, height := label.Box[0], label.Box[1], label.Box[2], label.Box[3]
			if x0 < 0 || y0 < 0 || width <= 0 || height <= 0 ||
				x0 > resource.AtlasSize[0] || y0 > resource.AtlasSize[1] ||
				width > resource.AtlasSize[0]-x0 || height > resource.AtlasSize[1]-y0 {
				return fmt.Errorf("%s/%s: box 越界", rid, label.Name)
			}
			if label.Erase != "row_background" {
				return fmt.Errorf("%s/%s: 不支持的擦除方式 %q", rid, label.Name, label.Erase)
			}
			if label.Body < 0 || label.Body > 255 || label.Outline < 0 || label.Outline > 255 {
				return fmt.Errorf("%s/%s: 颜色索引不合法", rid, label.Name)
			}
			if label.MaskOffset < 0 || label.MaskLength != width*height ||
				label.MaskOffset > len(plan.blob) ||
				label.MaskLength > len(plan.blob)-label.MaskOffset {
				return fmt.Errorf("%s/%s: mask 范围不合法", rid, label.Name)
			}
		}
	}
	return nil
}

func (p *recipePlan) expectedHash(rid, language string) (string, error) {
	resource, ok := p.Resources[rid]
	if !ok {
		return "", fmt.Errorf("recipe plan 没有资源 %s", rid)
	}
	value, ok := resource.Expected[language]
	if !ok {
		return "", fmt.Errorf("recipe plan 没有 %s 的 %s 期望哈希", language, rid)
	}
	return value, nil
}
