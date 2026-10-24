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
	"strconv"
)

const (
	recipeMagic      = "FS2RECIPE1\n"
	recipePlanFormat = 2
	maxRecipeHeader  = 16 << 20
	// Texture layers are raw RGBA so their bytes do not depend on the host
	// zlib. The three-language blob is about 120 MiB before the plan's gzip
	// wrapper, so leave room for more labels while still bounding the read.
	maxRecipeMaskData = 256 << 20
	maxRecipeCells    = 1 << 20
	// Nameplates are one 72x48 8bpp image staged at 0x2EB080; the shell copies
	// the full canvas to the screen, so every byte of it comes from the plan.
	plateCanvasBytes = 72 * 48
)

type recipePlan struct {
	Format     int
	Version    string
	Game       string
	Languages  []string
	ArchiveLBA int
	SectorSize int
	Resources  map[string]*recipeResource
	Files      map[string]*recipeFile
	blob       []byte
}

// recipeFile describes one non-resource target file. Disc images and texture
// atlases are generated from the user's original; executables are a small,
// fully declared binary patch.
type recipeFile struct {
	Kind           string                    `json:"kind"`
	Path           string                    `json:"path"`
	OriginalSHA256 string                    `json:"original_sha256"`
	Size           int64                     `json:"size"`
	PatchedSHA256  string                    `json:"patched_sha256,omitempty"`
	PatchedSize    int64                     `json:"patched_size,omitempty"`
	Patches        []recipeBinaryPatch       `json:"patches,omitempty"`
	ImageSize      []int                     `json:"image_size,omitempty"`
	PartCount      int                       `json:"part_count,omitempty"`
	Parts          map[string][4]int         `json:"parts,omitempty"`
	Panels         *recipeTexturePanels      `json:"panels,omitempty"`
	Layers         map[string]*recipeTexture `json:"languages,omitempty"`
}

type recipeBinaryPatch struct {
	Name     string `json:"name"`
	Offset   int64  `json:"offset"`
	Original string `json:"original"`
	Patched  string `json:"patched"`
}

type recipeTexturePanels struct {
	Parts    []int  `json:"parts"`
	TitleBox [4]int `json:"title_box"`
	BodyBox  [4]int `json:"body_box"`
}

type recipeTexture struct {
	Labels []recipeTextureLabel `json:"labels"`
	Title  recipeTextureLayer   `json:"title"`
	Button recipeTextureLayer   `json:"button"`
}

type recipeTextureLabel struct {
	Part       int    `json:"part"`
	Box        [4]int `json:"box"`
	DataOffset int    `json:"data_offset"`
	DataLength int    `json:"data_length"`
	DataWidth  int    `json:"data_width"`
	DataHeight int    `json:"data_height"`
	DataFormat string `json:"data_format"`
}

type recipeTextureLayer struct {
	DataOffset int    `json:"data_offset"`
	DataLength int    `json:"data_length"`
	DataWidth  int    `json:"data_width"`
	DataHeight int    `json:"data_height"`
	DataFormat string `json:"data_format"`
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
	Type             string            `json:"type"`
	Erase            []recipeErase     `json:"erase"`
	Labels           []recipeLabel     `json:"labels"`
	Writes           []recipeWrite     `json:"writes"`
	Patches          []recipeWordPatch `json:"patches"`
	DataOffset       int               `json:"data_offset"`
	DataLength       int               `json:"data_length"`
	TimingCharacters int               `json:"timing_characters"`
}

// recipeWordPatch replaces one big-endian u16 inside a script resource. The
// restaurant scene stores the cook's speaker index this way.
type recipeWordPatch struct {
	Name     string `json:"name"`
	Offset   int    `json:"offset"`
	Original int    `json:"original"`
	Patched  int    `json:"patched"`
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
	Files        map[string]*recipeFile     `json:"files"`
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
		Files:      header.Files,
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
	if err := validateRecipeFiles(plan); err != nil {
		return nil, err
	}
	return plan, nil
}

func validateRecipeFiles(plan *recipePlan) error {
	if len(plan.Files) == 0 {
		return errors.New("recipe plan 没有目标文件")
	}
	for _, required := range []string{gameDiscImage, gameExecutable, "Texture.tex"} {
		if plan.Files[required] == nil {
			return fmt.Errorf("recipe plan 缺少目标文件 %s", required)
		}
	}
	seen := make(map[string]bool, len(plan.Files))
	for name, file := range plan.Files {
		if seen[name] {
			return fmt.Errorf("recipe plan 重复列出文件 %s", name)
		}
		seen[name] = true
		safe, err := safeRelativePath(name)
		if err != nil || safe != name {
			return fmt.Errorf("recipe plan 文件路径不合法: %q", name)
		}
		if file == nil {
			return fmt.Errorf("recipe plan 文件 %s 为空", name)
		}
		if file.Path != name {
			return fmt.Errorf("%s: path 与文件键不一致", name)
		}
		if !validSHA256(file.OriginalSHA256) || file.Size <= 0 {
			return fmt.Errorf("%s: 原版哈希或大小不合法", name)
		}
		switch file.Kind {
		case "disc":
			if name != gameDiscImage {
				return fmt.Errorf("%s: 光盘条目名称不合法", name)
			}
			if len(file.Patches) != 0 || file.Panels != nil || len(file.Layers) != 0 {
				return fmt.Errorf("%s: 光盘条目包含多余字段", name)
			}
		case "binary":
			if name != gameExecutable {
				return fmt.Errorf("%s: 可执行文件条目名称不合法", name)
			}
			if !validSHA256(file.PatchedSHA256) || file.PatchedSize <= 0 {
				return fmt.Errorf("%s: 补丁后哈希或大小不合法", name)
			}
			if len(file.Patches) == 0 {
				return fmt.Errorf("%s: 没有二进制补丁", name)
			}
			for _, patch := range file.Patches {
				if patch.Name == "" || patch.Offset < 0 {
					return fmt.Errorf("%s: 二进制补丁缺少名字或偏移", name)
				}
				original, err := hex.DecodeString(patch.Original)
				if err != nil || len(original) == 0 {
					return fmt.Errorf("%s/%s: 原版字节不合法", name, patch.Name)
				}
				patched, err := hex.DecodeString(patch.Patched)
				if err != nil || len(patched) != len(original) {
					return fmt.Errorf("%s/%s: 补丁字节不合法", name, patch.Name)
				}
				if patch.Offset > file.Size || int64(len(original)) > file.Size-patch.Offset {
					return fmt.Errorf("%s/%s: 补丁越过文件", name, patch.Name)
				}
			}
		case "texture":
			if err := validateRecipeTexture(file, plan); err != nil {
				return err
			}
		default:
			return fmt.Errorf("%s: 不支持的文件类型 %q", name, file.Kind)
		}
	}
	return nil
}

func validateRecipeTexture(file *recipeFile, plan *recipePlan) error {
	if len(file.ImageSize) != 2 || file.ImageSize[0] <= 0 || file.ImageSize[1] <= 0 {
		return fmt.Errorf("%s: image_size 不合法", file.Path)
	}
	width, height := file.ImageSize[0], file.ImageSize[1]
	if file.PartCount <= 0 || len(file.Parts) == 0 {
		return fmt.Errorf("%s: parts 元数据不合法", file.Path)
	}
	for key, box := range file.Parts {
		if key == "" {
			return fmt.Errorf("%s: part 编号为空", file.Path)
		}
		if !validBox(box, width, height) {
			return fmt.Errorf("%s: part %s 越界", file.Path, key)
		}
	}
	if file.Panels == nil || len(file.Panels.Parts) == 0 {
		return fmt.Errorf("%s: panel 信息缺失", file.Path)
	}
	for _, part := range file.Panels.Parts {
		if _, ok := file.Parts[strconv.Itoa(part)]; !ok {
			return fmt.Errorf("%s: panel part %d 没有坐标", file.Path, part)
		}
	}
	for _, box := range [][4]int{file.Panels.TitleBox, file.Panels.BodyBox} {
		if !validBox(box, width, height) {
			return fmt.Errorf("%s: panel box 越界", file.Path)
		}
	}
	if len(file.Layers) != len(plan.Languages) {
		return fmt.Errorf("%s: 语言层数量不符", file.Path)
	}
	for _, language := range plan.Languages {
		layers := file.Layers[language]
		if layers == nil {
			return fmt.Errorf("%s: 缺少 %s 的图层", file.Path, language)
		}
		if len(layers.Labels) == 0 {
			return fmt.Errorf("%s: %s 没有标签图层", file.Path, language)
		}
		for _, label := range layers.Labels {
			if label.Part < 0 || label.Part >= file.PartCount {
				return fmt.Errorf("%s/%s: label part 越界", file.Path, language)
			}
			if _, ok := file.Parts[strconv.Itoa(label.Part)]; !ok {
				return fmt.Errorf("%s/%s: label part %d 没有声明坐标", file.Path, language, label.Part)
			}
			if !validBox(label.Box, width, height) {
				return fmt.Errorf("%s/%s: label box 越界", file.Path, language)
			}
			if err := validateTextureLayer(plan, label.DataOffset, label.DataLength,
				label.DataWidth, label.DataHeight, label.DataFormat, label.Box); err != nil {
				return fmt.Errorf("%s/%s: %w", file.Path, language, err)
			}
		}
		if err := validateTextureLayer(plan, layers.Title.DataOffset, layers.Title.DataLength,
			layers.Title.DataWidth, layers.Title.DataHeight, layers.Title.DataFormat,
			file.Panels.TitleBox); err != nil {
			return fmt.Errorf("%s/%s: title %w", file.Path, language, err)
		}
		if err := validateTextureLayer(plan, layers.Button.DataOffset, layers.Button.DataLength,
			layers.Button.DataWidth, layers.Button.DataHeight, layers.Button.DataFormat,
			file.Panels.BodyBox); err != nil {
			return fmt.Errorf("%s/%s: button %w", file.Path, language, err)
		}
	}
	return nil
}

func validBox(box [4]int, width, height int) bool {
	return box[0] >= 0 && box[1] >= 0 && box[2] > box[0] && box[3] > box[1] &&
		box[2] <= width && box[3] <= height
}

func validateTextureLayer(plan *recipePlan, offset, length, width, height int,
	format string, box [4]int) error {
	if offset < 0 || length <= 0 || offset > len(plan.blob) || length > len(plan.blob)-offset {
		return errors.New("图层数据范围越过 plan")
	}
	switch format {
	case "", "png":
		return nil
	case "raw_rgba":
		if width != box[2]-box[0] || height != box[3]-box[1] {
			return errors.New("raw 图层尺寸与 box 不符")
		}
		if length != width*height*4 {
			return errors.New("raw 图层长度与尺寸不符")
		}
		return nil
	default:
		return fmt.Errorf("未知图层编码 %q", format)
	}
}

func validResourceID(rid string) bool {
	if len(rid) == 4 {
		for _, char := range rid {
			if (char < '0' || char > '9') && (char < 'A' || char > 'F') {
				return false
			}
		}
		return true
	}
	// Story pages use P followed by the four- or five-digit page id.
	if len(rid) < 5 || len(rid) > 6 || rid[0] != 'P' {
		return false
	}
	for _, char := range rid[1:] {
		if char < '0' || char > '9' {
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
	case "card_tilemap_8bpp":
		if len(resource.AtlasSize) != 0 {
			return fmt.Errorf("%s: 角色卡资源不应带 atlas_size", rid)
		}
	case "story_page_2bpp":
		if len(resource.AtlasSize) != 0 {
			return fmt.Errorf("%s: 剧情页不应带 atlas_size", rid)
		}
		if resource.Size != 4096 {
			return fmt.Errorf("%s: 剧情页大小必须是 4096", rid)
		}
	case "word_patches_16be":
		if len(resource.AtlasSize) != 0 {
			return fmt.Errorf("%s: 脚本词补丁不应带 atlas_size", rid)
		}
	case "plate_bitmap_8bpp":
		if len(resource.AtlasSize) != 0 {
			return fmt.Errorf("%s: 名字图不应带 atlas_size", rid)
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
		case "card_tilemap_8bpp":
			if err := validateCardOperation(rid, language, operation, resource, plan); err != nil {
				return err
			}
		case "story_page_2bpp":
			if err := validateStoryOperation(rid, language, operation, resource, plan); err != nil {
				return err
			}
		case "word_patches_16be":
			if err := validateWordOperation(rid, language, operation, resource); err != nil {
				return err
			}
		case "plate_bitmap_8bpp":
			if err := validatePlateOperation(rid, language, operation, resource, plan); err != nil {
				return err
			}
		}
	}
	return nil
}

func validateWordOperation(rid, language string, operation recipeOperation, resource *recipeResource) error {
	if operation.Type != "word_patches" {
		return fmt.Errorf("%s: %s 的操作类型 %q 不受支持", rid, language, operation.Type)
	}
	if len(operation.Patches) == 0 || len(operation.Labels) != 0 ||
		len(operation.Erase) != 0 || len(operation.Writes) != 0 {
		return fmt.Errorf("%s: %s 的脚本词补丁内容不合法", rid, language)
	}
	seen := make(map[int]bool, len(operation.Patches))
	for _, patch := range operation.Patches {
		if patch.Name == "" {
			return fmt.Errorf("%s: 脚本词补丁缺少名字", rid)
		}
		if patch.Offset < 0 || patch.Offset%2 != 0 {
			return fmt.Errorf("%s/%s: 偏移必须是偶数", rid, patch.Name)
		}
		if patch.Offset > resource.Size-2 {
			return fmt.Errorf("%s/%s: 补丁越过资源", rid, patch.Name)
		}
		if patch.Original < 0 || patch.Original > 0xFFFF ||
			patch.Patched < 0 || patch.Patched > 0xFFFF {
			return fmt.Errorf("%s/%s: 词值必须是 u16", rid, patch.Name)
		}
		if patch.Original == patch.Patched {
			return fmt.Errorf("%s/%s: 原值与新值相同", rid, patch.Name)
		}
		if seen[patch.Offset] {
			return fmt.Errorf("%s/%s: 偏移重复", rid, patch.Name)
		}
		seen[patch.Offset] = true
	}
	return nil
}

func validateStoryOperation(rid, language string, operation recipeOperation, resource *recipeResource, plan *recipePlan) error {
	if operation.Type != "story_page" {
		return fmt.Errorf("%s: %s 的操作类型 %q 不受支持", rid, language, operation.Type)
	}
	if len(operation.Labels) != 0 || len(operation.Erase) != 0 || len(operation.Writes) != 0 {
		return fmt.Errorf("%s: %s 的剧情页操作内容不合法", rid, language)
	}
	if operation.DataOffset < 0 || operation.DataLength != 2352 ||
		operation.DataOffset > len(plan.blob) ||
		operation.DataLength > len(plan.blob)-operation.DataOffset {
		return fmt.Errorf("%s: %s 的剧情页图像范围不合法", rid, language)
	}
	if operation.TimingCharacters < 0 || operation.TimingCharacters > 42 {
		return fmt.Errorf("%s: %s 的时序字符数不合法", rid, language)
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

func validatePlateOperation(rid, language string, operation recipeOperation, resource *recipeResource, plan *recipePlan) error {
	if operation.Type != "plate_bitmap" {
		return fmt.Errorf("%s: %s 的操作类型 %q 不受支持", rid, language, operation.Type)
	}
	if len(operation.Writes) != 1 || len(operation.Labels) != 0 || len(operation.Erase) != 0 {
		return fmt.Errorf("%s: %s 的名字图操作内容不合法", rid, language)
	}
	// The shell blits the whole 72x48 canvas, so a write must cover exactly
	// that image and stay inside the resource's declared bytes.
	write := operation.Writes[0]
	if write.Name == "" {
		return fmt.Errorf("%s: 名字图写入缺少名字", rid)
	}
	if write.Offset < 0 || write.DataLength != plateCanvasBytes ||
		write.Offset > resource.Size || write.DataLength > resource.Size-write.Offset {
		return fmt.Errorf("%s/%s: 写入范围越过资源", rid, write.Name)
	}
	if write.DataOffset < 0 || write.DataOffset > len(plan.blob) ||
		write.DataLength > len(plan.blob)-write.DataOffset {
		return fmt.Errorf("%s/%s: 写入数据范围越过 plan", rid, write.Name)
	}
	return nil
}

func validateCardOperation(rid, language string, operation recipeOperation, resource *recipeResource, plan *recipePlan) error {
	if operation.Type != "tilemap_writes" {
		return fmt.Errorf("%s: %s 的操作类型 %q 不受支持", rid, language, operation.Type)
	}
	if len(operation.Writes) == 0 || len(operation.Labels) != 0 || len(operation.Erase) != 0 {
		return fmt.Errorf("%s: %s 的角色卡操作内容不合法", rid, language)
	}
	for _, write := range operation.Writes {
		if write.Name == "" {
			return fmt.Errorf("%s: 角色卡写入缺少名字", rid)
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
