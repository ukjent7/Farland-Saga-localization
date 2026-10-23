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
	Erase  []recipeErase `json:"erase"`
	Labels []recipeLabel `json:"labels"`
	Writes []recipeWrite `json:"writes"`
}

type recipeErase struct {
	Box    [4]int  `json:"box"`
	Mode   string  `json:"mode"`
	Color  *int    `json:"color"`
	Sample *[4]int `json:"sample"`
}

type recipeLabel struct {
	Name       string `json:"name"`
	Box        [4]int `json:"box"`
	MaskOffset int    `json:"mask_offset"`
	MaskLength int    `json:"mask_length"`
	Body       int    `json:"body"`
	Outline    int    `json:"outline"`
}

type recipeWrite struct {
	Name       string `json:"name"`
	Offset     int    `json:"offset"`
	DataOffset int    `json:"data_offset"`
	DataLength int    `json:"data_length"`
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
	if resource.ArchiveOffset < 0 || resource.ArchiveOffset%int64(plan.SectorSize) != 0 {
		return fmt.Errorf("%s: archive_offset 未按扇区对齐", rid)
	}
	if resource.Size <= 0 || resource.Size%plan.SectorSize != 0 {
		return fmt.Errorf("%s: 资源大小必须是扇区大小的正整数倍", rid)
	}
	if !validSHA256(resource.OriginalSHA256) {
		return fmt.Errorf("%s: original_sha256 不合法", rid)
	}
	switch resource.Format {
	case "tile_atlas_8bpp":
		if len(resource.AtlasSize) != 2 || resource.AtlasSize[0] <= 0 || resource.AtlasSize[1] <= 0 ||
			resource.AtlasSize[0] > 8192 || resource.AtlasSize[1] > 8192 ||
			resource.AtlasSize[0]%8 != 0 || resource.AtlasSize[1]%8 != 0 {
			return fmt.Errorf("%s: atlas_size 不合法", rid)
		}
	case "mono_bitmap_1bpp":
		if len(resource.AtlasSize) != 0 {
			return fmt.Errorf("%s: 位图资源不应带 atlas_size", rid)
		}
	default:
		return fmt.Errorf("%s: 不支持的 recipe 资源格式 %q", rid, resource.Format)
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
		switch resource.Format {
		case "tile_atlas_8bpp":
			if err := validateTileOperation(rid, language, operation, resource, plan); err != nil {
				return err
			}
		case "mono_bitmap_1bpp":
			if err := validateMonoOperation(rid, language, operation, resource, plan); err != nil {
				return err
			}
		}
	}
	return nil
}

func validateTileOperation(rid, language string, operation recipeOperation, resource *recipeResource, plan *recipePlan) error {
	if operation.Type != "tile_atlas" {
		return fmt.Errorf("%s: %s 的操作类型 %q 不受支持", rid, language, operation.Type)
	}
	if len(operation.Labels) == 0 || len(operation.Writes) != 0 {
		return fmt.Errorf("%s: %s 的 tile 操作内容不合法", rid, language)
	}
	width, height := resource.AtlasSize[0], resource.AtlasSize[1]
	for index, region := range operation.Erase {
		if err := validateErase(rid, language, index, region, width, height); err != nil {
			return err
		}
	}
	for _, label := range operation.Labels {
		if label.Name == "" {
			return fmt.Errorf("%s: label 缺少名字", rid)
		}
		x0, y0, boxWidth, boxHeight := label.Box[0], label.Box[1], label.Box[2], label.Box[3]
		if x0 < 0 || y0 < 0 || boxWidth <= 0 || boxHeight <= 0 ||
			x0 > width || y0 > height ||
			boxWidth > width-x0 || boxHeight > height-y0 {
			return fmt.Errorf("%s/%s: box 越界", rid, label.Name)
		}
		if label.Body < 0 || label.Body > 255 || label.Outline < 0 || label.Outline > 255 {
			return fmt.Errorf("%s/%s: 颜色索引不合法", rid, label.Name)
		}
		if label.MaskOffset < 0 || label.MaskLength != boxWidth*boxHeight ||
			label.MaskOffset > len(plan.blob) ||
			label.MaskLength > len(plan.blob)-label.MaskOffset {
			return fmt.Errorf("%s/%s: mask 范围不合法", rid, label.Name)
		}
	}
	return nil
}

func validateErase(rid, language string, index int, region recipeErase, width, height int) error {
	name := fmt.Sprintf("%s: %s erase[%d]", rid, language, index)
	x0, y0, boxWidth, boxHeight := region.Box[0], region.Box[1], region.Box[2], region.Box[3]
	if x0 < 0 || y0 < 0 || boxWidth <= 0 || boxHeight <= 0 ||
		x0 > width || y0 > height ||
		boxWidth > width-x0 || boxHeight > height-y0 {
		return fmt.Errorf("%s: box 越界", name)
	}
	switch region.Mode {
	case "row_background":
		if region.Color != nil {
			return fmt.Errorf("%s: 行背景擦除不接受 color", name)
		}
	case "color":
		if region.Color == nil || *region.Color < 0 || *region.Color > 255 {
			return fmt.Errorf("%s: 颜色填充缺少合法 color", name)
		}
	default:
		return fmt.Errorf("%s: 不支持的擦除方式 %q", name, region.Mode)
	}
	if region.Sample != nil {
		sx0, sy0, sampleWidth, sampleHeight := region.Sample[0], region.Sample[1], region.Sample[2], region.Sample[3]
		if sx0 < 0 || sy0 < 0 || sampleWidth <= 0 || sampleHeight <= 0 ||
			sx0 > width || sy0 > height ||
			sampleWidth > width-sx0 || sampleHeight > height-sy0 {
			return fmt.Errorf("%s: sample 越界", name)
		}
	}
	return nil
}

func validateMonoOperation(rid, language string, operation recipeOperation, resource *recipeResource, plan *recipePlan) error {
	if operation.Type != "mono_bitmap" {
		return fmt.Errorf("%s: %s 的操作类型 %q 不受支持", rid, language, operation.Type)
	}
	if len(operation.Writes) == 0 || len(operation.Labels) != 0 || len(operation.Erase) != 0 {
		return fmt.Errorf("%s: %s 的位图操作内容不合法", rid, language)
	}
	// Writes are copied in plan order. Gaps and later filling of an earlier
	// slot are both intentional (generic names are emitted after the named
	// slots), so the plan order is the override order.
	for _, write := range operation.Writes {
		if write.Name == "" {
			return fmt.Errorf("%s: 位图写入缺少名字", rid)
		}
		if write.Offset < 0 || write.DataLength <= 0 ||
			write.Offset > resource.Size || write.DataLength > resource.Size-write.Offset {
			return fmt.Errorf("%s/%s: 写入范围越过资源", rid, write.Name)
		}
		if write.DataOffset < 0 || write.DataOffset > len(plan.blob) ||
			write.DataLength > len(plan.blob)-write.DataOffset {
			return fmt.Errorf("%s/%s: 写入数据范围越过 plan", rid, write.Name)
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
