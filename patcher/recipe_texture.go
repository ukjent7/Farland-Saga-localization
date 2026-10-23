//go:build saga2

// Install-time Texture.tex generation. The plan carries only text layers; the
// installer decodes the user's pristine atlas, blits the layers, re-encodes the
// PNG, and rebuilds the SSF container with the original 2048-byte header and
// the untouched Parts metadata.
package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"image"
	"image/color"
	"image/draw"
	"image/png"
	"math"
	"path/filepath"
	"strconv"
)

const (
	textureHeaderBytes = 40
	texturePanelWidth  = 228
	texturePanelHeight = 225
)

type textureAtlas struct {
	image         *image.NRGBA
	sectionHeader []byte
	partsSuffix   []byte
	boxes         [][4]int
}

func pad20(value string) []byte {
	out := bytes.Repeat([]byte{' '}, 20)
	copy(out, value)
	return out
}

func parseTexturePayload(payload []byte) (*textureAtlas, error) {
	if len(payload) < textureHeaderBytes {
		return nil, errors.New("Texture 载荷过短")
	}
	if !bytes.Equal(payload[:20], pad20("Texture")) {
		return nil, errors.New("Texture 载荷缺少 Texture 标记")
	}
	end := int(binary.LittleEndian.Uint32(payload[20:24]))
	pngSize := int(binary.LittleEndian.Uint32(payload[32:36]))
	width := int(binary.LittleEndian.Uint16(payload[36:38]))
	height := int(binary.LittleEndian.Uint16(payload[38:40]))
	if end != textureHeaderBytes+pngSize || end > len(payload) {
		return nil, errors.New("Texture 图像段长度不合法")
	}
	if width <= 0 || height <= 0 || width > 16384 || height > 16384 {
		return nil, errors.New("Texture 尺寸不合法")
	}
	partsStart := (end + 3) &^ 3
	if partsStart > len(payload) {
		return nil, errors.New("Texture 图像段越过载荷")
	}
	if !bytes.Equal(payload[end:partsStart], make([]byte, partsStart-end)) {
		return nil, errors.New("Texture 图像段对齐字节不为零")
	}
	if partsStart+28 > len(payload) || !bytes.Equal(payload[partsStart:partsStart+20], pad20("Parts")) {
		return nil, errors.New("Texture 缺少 Parts 元数据")
	}
	sectionLength := int(binary.LittleEndian.Uint32(payload[partsStart+20 : partsStart+24]))
	partCount := int(binary.LittleEndian.Uint32(payload[partsStart+24 : partsStart+28]))
	if partCount <= 0 || partCount > 65536 || sectionLength != 28+partCount*32 {
		return nil, errors.New("Texture Parts 元数据长度不合法")
	}
	if partsStart+sectionLength > len(payload) {
		return nil, errors.New("Texture Parts 元数据越过载荷")
	}

	decoded, err := png.Decode(bytes.NewReader(payload[textureHeaderBytes:end]))
	if err != nil {
		return nil, fmt.Errorf("Texture PNG 解码失败: %w", err)
	}
	if decoded.Bounds().Dx() != width || decoded.Bounds().Dy() != height {
		return nil, errors.New("Texture PNG 尺寸与头部不符")
	}
	imageData := image.NewNRGBA(image.Rect(0, 0, width, height))
	draw.Draw(imageData, imageData.Bounds(), decoded, decoded.Bounds().Min, draw.Src)

	boxes := make([][4]int, partCount)
	for index := range boxes {
		offset := partsStart + 28 + index*32
		values := [8]float32{}
		for field := range values {
			values[field] = math.Float32frombits(binary.LittleEndian.Uint32(payload[offset+field*4:]))
		}
		box := [4]int{
			int(math.RoundToEven(float64(values[4]) * float64(width))),
			int(math.RoundToEven(float64(values[5]) * float64(height))),
			int(math.RoundToEven(float64(values[6]) * float64(width))),
			int(math.RoundToEven(float64(values[7]) * float64(height))),
		}
		if !validBox(box, width, height) {
			return nil, fmt.Errorf("Texture part %d 越界", index)
		}
		boxes[index] = box
	}

	return &textureAtlas{
		image:         imageData,
		sectionHeader: append([]byte(nil), payload[:textureHeaderBytes]...),
		partsSuffix:   append([]byte(nil), payload[partsStart:]...),
		boxes:         boxes,
	}, nil
}

func generateRecipeTexture(file *recipeFile, original []byte, basePath, exePath string, blob []byte, language string) ([]byte, error) {
	params, err := LoadSSFParameters(basePath, exePath, filepath.Base(file.Path))
	if err != nil {
		return nil, fmt.Errorf("解析 Texture SSF 参数失败: %w", err)
	}
	payload := TransformSSF(original[2048:], params, false)
	atlas, err := parseTexturePayload(payload)
	if err != nil {
		return nil, err
	}
	if len(file.ImageSize) != 2 || atlas.image.Bounds().Dx() != file.ImageSize[0] ||
		atlas.image.Bounds().Dy() != file.ImageSize[1] {
		return nil, errors.New("Texture 图集尺寸与 recipe 不符")
	}
	if len(atlas.boxes) != file.PartCount {
		return nil, fmt.Errorf("Texture part 数量不符：期望 %d，实际 %d", file.PartCount, len(atlas.boxes))
	}
	for key, expected := range file.Parts {
		index, err := strconv.Atoi(key)
		if err != nil || index < 0 || index >= len(atlas.boxes) {
			return nil, fmt.Errorf("Texture part %s 编号不合法", key)
		}
		if atlas.boxes[index] != expected {
			return nil, fmt.Errorf("Texture part %s 坐标与 recipe 不符", key)
		}
	}
	layers := file.Layers[language]
	if layers == nil {
		return nil, fmt.Errorf("Texture 没有 %s 的图层", language)
	}

	for _, label := range layers.Labels {
		if label.Part < 0 || label.Part >= len(atlas.boxes) {
			return nil, fmt.Errorf("Texture 标签 part %d 越界", label.Part)
		}
		if atlas.boxes[label.Part] != label.Box {
			return nil, fmt.Errorf("Texture 标签 part %d 坐标与 recipe 不符", label.Part)
		}
		layer, err := decodeTextureLayer(blob, label.DataOffset, label.DataLength)
		if err != nil {
			return nil, err
		}
		if layer.Bounds().Dx() != label.Box[2]-label.Box[0] ||
			layer.Bounds().Dy() != label.Box[3]-label.Box[1] {
			return nil, errors.New("Texture 标签图层尺寸与 box 不符")
		}
		draw.Draw(atlas.image, image.Rect(label.Box[0], label.Box[1], label.Box[2], label.Box[3]),
			layer, image.Point{}, draw.Src)
	}

	if file.Panels == nil || len(file.Panels.Parts) == 0 {
		return nil, errors.New("Texture 缺少面板信息")
	}
	titleLayer, err := decodeTextureLayer(blob, layers.Title.DataOffset, layers.Title.DataLength)
	if err != nil {
		return nil, err
	}
	buttonLayer, err := decodeTextureLayer(blob, layers.Button.DataOffset, layers.Button.DataLength)
	if err != nil {
		return nil, err
	}
	titleBox := file.Panels.TitleBox
	bodyBox := file.Panels.BodyBox
	if titleLayer.Bounds().Dx() != titleBox[2]-titleBox[0] ||
		titleLayer.Bounds().Dy() != titleBox[3]-titleBox[1] ||
		buttonLayer.Bounds().Dx() != bodyBox[2]-bodyBox[0] ||
		buttonLayer.Bounds().Dy() != bodyBox[3]-bodyBox[1] {
		return nil, errors.New("Texture 面板图层尺寸与 recipe 不符")
	}
	for _, part := range file.Panels.Parts {
		if part < 0 || part >= len(atlas.boxes) {
			return nil, fmt.Errorf("Texture 面板 part %d 越界", part)
		}
		box := atlas.boxes[part]
		if box[2]-box[0] != texturePanelWidth || box[3]-box[1] != texturePanelHeight {
			return nil, fmt.Errorf("Texture 面板 part %d 尺寸不符", part)
		}
		paintTexturePanel(atlas.image, box, titleBox, bodyBox, titleLayer, buttonLayer)
	}

	var pngBuffer bytes.Buffer
	encoder := png.Encoder{CompressionLevel: png.DefaultCompression}
	if err := encoder.Encode(&pngBuffer, atlas.image); err != nil {
		return nil, fmt.Errorf("Texture PNG 编码失败: %w", err)
	}
	pngData := pngBuffer.Bytes()

	header := append([]byte(nil), atlas.sectionHeader...)
	end := textureHeaderBytes + len(pngData)
	binary.LittleEndian.PutUint32(header[20:24], uint32(end))
	binary.LittleEndian.PutUint32(header[32:36], uint32(len(pngData)))
	rebuilt := make([]byte, 0, end+(4-end%4)%4+len(atlas.partsSuffix))
	rebuilt = append(rebuilt, header...)
	rebuilt = append(rebuilt, pngData...)
	rebuilt = append(rebuilt, make([]byte, (4-end%4)%4)...)
	rebuilt = append(rebuilt, atlas.partsSuffix...)

	encoded := TransformSSF(rebuilt, params, true)
	output := make([]byte, 2048+len(encoded))
	copy(output[:2048], original[:2048])
	copy(output[2048:], encoded)

	// Decode the file we just produced. The game sees this exact container, so
	// a mismatch here means the file must not be written.
	checkPayload := TransformSSF(output[2048:], params, false)
	if !bytes.Equal(checkPayload, rebuilt) {
		return nil, errors.New("Texture 重新加密后无法还原")
	}
	check, err := parseTexturePayload(checkPayload)
	if err != nil {
		return nil, fmt.Errorf("Texture 回读失败: %w", err)
	}
	if !bytes.Equal(check.image.Pix, atlas.image.Pix) {
		return nil, errors.New("Texture 回读像素不符")
	}
	if !bytes.Equal(check.partsSuffix, atlas.partsSuffix) {
		return nil, errors.New("Texture 回读 Parts 元数据不符")
	}
	if !bytes.Equal(check.sectionHeader[:20], atlas.sectionHeader[:20]) ||
		!bytes.Equal(check.sectionHeader[24:32], atlas.sectionHeader[24:32]) ||
		!bytes.Equal(check.sectionHeader[36:40], atlas.sectionHeader[36:40]) {
		return nil, errors.New("Texture 回读头部不符")
	}
	for index, expected := range atlas.boxes {
		if check.boxes[index] != expected {
			return nil, errors.New("Texture 回读 part 坐标不符")
		}
	}
	return output, nil
}

func decodeTextureLayer(blob []byte, offset, length int) (*image.NRGBA, error) {
	if offset < 0 || length <= 0 || offset > len(blob) || length > len(blob)-offset {
		return nil, errors.New("Texture 图层范围越过 plan")
	}
	decoded, err := png.Decode(bytes.NewReader(blob[offset : offset+length]))
	if err != nil {
		return nil, fmt.Errorf("Texture 图层 PNG 解码失败: %w", err)
	}
	bounds := decoded.Bounds()
	layer := image.NewNRGBA(image.Rect(0, 0, bounds.Dx(), bounds.Dy()))
	draw.Draw(layer, layer.Bounds(), decoded, bounds.Min, draw.Src)
	return layer, nil
}

func paintTexturePanel(target *image.NRGBA, panel, titleBox, bodyBox [4]int, title, button *image.NRGBA) {
	left, top := panel[0], panel[1]
	// The original renderer rebuilt the title gradient by stretching one
	// text-free row. A one-row image resized vertically repeats that row.
	sourceOffset := target.PixOffset(left+16, top+51)
	for y := 0; y < titleBox[3]-titleBox[1]; y++ {
		destination := target.PixOffset(left+titleBox[0], top+titleBox[1]+y)
		copy(target.Pix[destination:destination+(titleBox[2]-titleBox[0])*4],
			target.Pix[sourceOffset:sourceOffset+(titleBox[2]-titleBox[0])*4])
	}
	draw.Draw(target, image.Rect(left+bodyBox[0], top+bodyBox[1],
		left+bodyBox[2], top+bodyBox[3]),
		&image.Uniform{C: color.NRGBA{0, 0, 0, 255}}, image.Point{}, draw.Src)
	compositePillowOver(target, image.Rect(left+titleBox[0], top+titleBox[1],
		left+titleBox[2], top+titleBox[3]), title)
	compositePillowOver(target, image.Rect(left+bodyBox[0], top+bodyBox[1],
		left+bodyBox[2], top+bodyBox[3]), button)
}

// compositePillowOver reproduces Pillow's ImagingAlphaComposite arithmetic.
// The reference renderer for the recipe layers uses Pillow, and its 7-bit
// fixed point rounding differs by one from image/draw's 16-bit path on
// translucent edges.
func compositePillowOver(dst *image.NRGBA, rect image.Rectangle, src *image.NRGBA) {
	for y := 0; y < rect.Dy(); y++ {
		for x := 0; x < rect.Dx(); x++ {
			target := dst.PixOffset(rect.Min.X+x, rect.Min.Y+y)
			source := src.PixOffset(x, y)
			sourceAlpha := uint32(src.Pix[source+3])
			if sourceAlpha == 0 {
				continue
			}
			targetAlpha := uint32(dst.Pix[target+3])
			blend := targetAlpha * (255 - sourceAlpha)
			alpha255 := sourceAlpha*255 + blend
			coefficient1 := sourceAlpha * 255 * 255 * 128 / alpha255
			coefficient2 := 255*128 - coefficient1
			for channel := 0; channel < 3; channel++ {
				value := uint32(src.Pix[source+channel])*coefficient1 +
					uint32(dst.Pix[target+channel])*coefficient2 + (0x80 << 7)
				dst.Pix[target+channel] = uint8(divide255(value) >> 7)
			}
			dst.Pix[target+3] = uint8(divide255(alpha255 + 0x80))
		}
	}
}

func divide255(value uint32) uint32 {
	return ((value >> 8) + value) >> 8
}
