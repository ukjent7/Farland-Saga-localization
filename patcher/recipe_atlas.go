//go:build saga2

// 8bpp tile atlas editing: decode the original map and tiles into a pixel
// plane, erase the label rows, blit the recipe masks, then rebuild the tile
// allocation with the same first-fit strategy as the private Python pipeline.
// Matching that strategy byte for byte is what keeps the generated resource
// identical to the released candidate.
package main

import (
	"encoding/binary"
	"errors"
	"fmt"
)

const (
	tileCellBytes = 64
	tileCellStep  = 2 // map values count 32-byte units
)

type tileAtlas struct {
	raw         []byte
	width       int
	height      int
	graphics    int
	cells       int
	pixelWidth  int
	pixelHeight int
	mapValues   []uint16
	plane       []byte
}

func decodeTileAtlas(raw []byte) (*tileAtlas, error) {
	if len(raw) < 16 {
		return nil, errors.New("tile atlas 头部不完整")
	}
	width := int(binary.BigEndian.Uint32(raw[0:4]))
	height := int(binary.BigEndian.Uint32(raw[4:8]))
	graphics := int(binary.BigEndian.Uint32(raw[12:16]))
	if width <= 0 || height <= 0 {
		return nil, errors.New("tile atlas 尺寸不合法")
	}
	mapCells := width * height
	if mapCells > maxRecipeCells {
		return nil, fmt.Errorf("tile atlas 有 %d 个 cell，超过上限", mapCells)
	}
	if mapCells > 1<<15 {
		return nil, errors.New("tile atlas 的 map 超出了 u16 索引范围")
	}
	mapEnd := 16 + mapCells*2
	if mapEnd > len(raw) {
		return nil, errors.New("tile atlas 的 map 被截断")
	}
	if graphics < mapEnd || graphics > len(raw) {
		return nil, errors.New("tile atlas 的 graphics 偏移不合法")
	}
	tiles := (len(raw) - graphics) / tileCellBytes
	if tiles == 0 {
		return nil, errors.New("tile atlas 没有 tile 数据")
	}
	if tiles > 1<<15 {
		return nil, errors.New("tile atlas 的 tile 数量过多")
	}
	atlas := &tileAtlas{
		raw:         raw,
		width:       width,
		height:      height,
		graphics:    graphics,
		cells:       tiles,
		pixelWidth:  width * 8,
		pixelHeight: height * 8,
		mapValues:   make([]uint16, mapCells),
		plane:       make([]byte, width*8*height*8),
	}
	for index := range atlas.mapValues {
		entry := binary.BigEndian.Uint16(raw[16+index*2:])
		if entry%tileCellStep != 0 || int(entry)/tileCellStep >= tiles {
			return nil, fmt.Errorf("tile atlas 的 map[%d]=%d 不是有效的 tile 序号", index, entry)
		}
		atlas.mapValues[index] = entry
	}
	for cy := 0; cy < height; cy++ {
		for cx := 0; cx < width; cx++ {
			entry := int(atlas.mapValues[cy*width+cx])
			offset := graphics + entry*32
			for y := 0; y < 8; y++ {
				row := (cy*8 + y) * atlas.pixelWidth
				copy(atlas.plane[row+cx*8:row+cx*8+8], raw[offset+y*8:offset+y*8+8])
			}
		}
	}
	return atlas, nil
}

// applyTileAtlasOperations erases each label row with its most common colour,
// then overlays the plan's mask. The mask already contains the final outline
// and body choices, so Go never has to rasterise a font.
func applyTileAtlasOperations(atlas *tileAtlas, operations []recipeOperation, blob []byte) (int, error) {
	ink := 0
	for _, operation := range operations {
		if operation.Type != "tile_atlas" {
			return 0, fmt.Errorf("不支持的 atlas 操作 %q", operation.Type)
		}
		for _, label := range operation.Labels {
			x0, y0, width, height := label.Box[0], label.Box[1], label.Box[2], label.Box[3]
			if x0 < 0 || y0 < 0 || width <= 0 || height <= 0 ||
				x0 > atlas.pixelWidth || y0 > atlas.pixelHeight ||
				width > atlas.pixelWidth-x0 || height > atlas.pixelHeight-y0 {
				return 0, fmt.Errorf("%s: box 越界", label.Name)
			}
			if label.MaskOffset < 0 || label.MaskLength < 0 ||
				label.MaskOffset > len(blob) ||
				label.MaskLength > len(blob)-label.MaskOffset {
				return 0, fmt.Errorf("%s: mask 范围越界", label.Name)
			}
			mask := blob[label.MaskOffset : label.MaskOffset+label.MaskLength]
			if len(mask) != width*height {
				return 0, fmt.Errorf("%s: mask 长度与 box 不符", label.Name)
			}
			for y := y0; y < y0+height; y++ {
				background := rowBackground(atlas.plane, atlas.pixelWidth, x0, x0+width, y)
				row := y * atlas.pixelWidth
				for x := x0; x < x0+width; x++ {
					atlas.plane[row+x] = background
				}
			}
			for y := 0; y < height; y++ {
				row := (y0 + y) * atlas.pixelWidth
				for x := 0; x < width; x++ {
					value := mask[y*width+x]
					switch value {
					case 0:
					case 1:
						atlas.plane[row+x0+x] = byte(label.Outline)
						ink++
					case 2:
						atlas.plane[row+x0+x] = byte(label.Body)
						ink++
					default:
						return 0, fmt.Errorf("%s: mask 值 %d 不合法", label.Name, value)
					}
				}
			}
		}
	}
	return ink, nil
}

// rowBackground returns the first value with the highest count in the row,
// matching Python Counter.most_common(1)'s stable insertion-order tie-break.
func rowBackground(plane []byte, stride, start, end, y int) byte {
	var counts [256]int
	row := y * stride
	for x := start; x < end; x++ {
		counts[plane[row+x]]++
	}
	best := 0
	bestCount := -1
	for x := start; x < end; x++ {
		value := plane[row+x]
		if counts[value] > bestCount {
			best = int(value)
			bestCount = counts[value]
		}
	}
	return byte(best)
}

// repackTileAtlas recomputes every cell image, claims an existing tile for
// each distinct image, and allocates a spare tile only when no existing tile
// holds it. Cells whose image is unchanged but whose map entry pointed at a
// duplicate tile are still repointed, exactly like the Python apply().
func repackTileAtlas(atlas *tileAtlas) (int, error) {
	contents := make([]string, atlas.width*atlas.height)
	for index := range contents {
		cx := index % atlas.width
		cy := index / atlas.width
		cell := make([]byte, tileCellBytes)
		for y := 0; y < 8; y++ {
			row := (cy*8 + y) * atlas.pixelWidth
			copy(cell[y*8:y*8+8], atlas.plane[row+cx*8:row+cx*8+8])
		}
		contents[index] = string(cell)
	}

	distinct := make([]string, 0, len(contents))
	seen := make(map[string]bool, len(contents))
	for _, content := range contents {
		if !seen[content] {
			seen[content] = true
			distinct = append(distinct, content)
		}
	}
	if len(distinct) > atlas.cells {
		return 0, fmt.Errorf("tile atlas 需要 %d 个 tile，只有 %d 个", len(distinct), atlas.cells)
	}

	holdings := make(map[string][]int, atlas.cells)
	for entry := 0; entry < atlas.cells*tileCellStep; entry += tileCellStep {
		offset := atlas.graphics + entry*32
		content := string(atlas.raw[offset : offset+tileCellBytes])
		holdings[content] = append(holdings[content], entry)
	}

	claimed := make(map[string]int, len(distinct))
	used := make(map[int]bool, len(distinct))
	for _, content := range distinct {
		for _, entry := range holdings[content] {
			if !used[entry] {
				claimed[content] = entry
				used[entry] = true
				break
			}
		}
	}

	spare := make([]int, 0, atlas.cells-len(claimed))
	for entry := 0; entry < atlas.cells*tileCellStep; entry += tileCellStep {
		if !used[entry] {
			spare = append(spare, entry)
		}
	}
	spareIndex := 0
	for _, content := range distinct {
		if _, ok := claimed[content]; ok {
			continue
		}
		if spareIndex >= len(spare) {
			return 0, errors.New("tile atlas 没有空闲 tile 可分配")
		}
		entry := spare[spareIndex]
		spareIndex++
		claimed[content] = entry
		used[entry] = true
		offset := atlas.graphics + entry*32
		copy(atlas.raw[offset:offset+tileCellBytes], content)
	}

	conflicts := 0
	for index, content := range contents {
		entry := claimed[content]
		if int(atlas.mapValues[index]) != entry {
			atlas.mapValues[index] = uint16(entry)
			conflicts++
		}
	}
	for index, entry := range atlas.mapValues {
		binary.BigEndian.PutUint16(atlas.raw[16+index*2:], entry)
	}
	return conflicts, nil
}
