//go:build !saga2

// Texture.tex 的声明式重建（schema 2）。计划只携带译文墨迹层：一张单色
// mask 与一张 RGBA ink。安装期解码用户自己的原版图集，按声明把 371 个标签
// 区域清成常量底色、把 7 个选项面板的标题渐变条由原版图集自身的一行按
// Pillow 的预乘 alpha 公式重建、把面板主体清成黑色，然后按 mask 叠加 ink。
// 原版美术（角色立绘、战斗精灵图、UI 边框）全部留在用户原版像素里，仓库
// 不含任何游戏美术字节。最后重编码 PNG、补齐到原版 PNG 长度以固定 Parts
// 元数据偏移，再用原版 SSF 参数重新加密容器。
package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/color"
	"image/draw"
	"image/png"
	"math"
)

const (
	fs1TextureHeaderBytes = 40
	fs1TextureLabelCount  = 371
	fs1TextureStripHeight = 46
	fs1TextureStripWidth  = 196
)

// pad20 把字符串填充为定长 20 字节的 SSF 段标记（与二代 recipe_texture 相同）。
func pad20(value string) []byte {
	out := bytes.Repeat([]byte{' '}, 20)
	copy(out, value)
	return out
}

// fs1TextureDeclaration 是编译器嵌进计划 blob 的紧凑声明。结构字段用整数
// 坐标与 RGBA 数组表达，避免任何游戏字节进入仓库。
type fs1TextureDeclaration struct {
	ImageSize  [2]int            `json:"image_size"`
	PartCount  int               `json:"part_count"`
	Panels     fs1TexturePanels  `json:"panels"`
	Labels     []fs1TextureLabel `json:"labels"`
	MaskOffset int64             `json:"mask_offset"`
	MaskLength int64             `json:"mask_length"`
	InkOffset  int64             `json:"ink_offset"`
	InkLength  int64             `json:"ink_length"`
}

type fs1TexturePanels struct {
	Parts     []int  `json:"parts"`
	HeaderBox [4]int `json:"header_box"`
	BodyBox   [4]int `json:"body_box"`
	StripRow  int    `json:"strip_row"`
	StripX    [2]int `json:"strip_x"`
}

type fs1TextureLabel struct {
	Part       int      `json:"part"`
	Box        [4]int   `json:"box"`
	Region     *[4]int  `json:"region,omitempty"`
	Background [4]uint8 `json:"background"`
}

// decodeFS1Layer 解码计划里的单色 mask 或 RGBA ink 层，并校验尺寸。
func decodeFS1Layer(data []byte, width, height int, grey bool) (image.Image, error) {
	decoded, err := png.Decode(bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("Texture 图层 PNG 解码失败: %w", err)
	}
	if decoded.Bounds().Dx() != width || decoded.Bounds().Dy() != height {
		return nil, errors.New("Texture 图层尺寸与图集不符")
	}
	if grey {
		out := image.NewGray(image.Rect(0, 0, width, height))
		draw.Draw(out, out.Bounds(), decoded, image.Point{}, draw.Src)
		return out, nil
	}
	out := image.NewNRGBA(image.Rect(0, 0, width, height))
	draw.Draw(out, out.Bounds(), decoded, image.Point{}, draw.Src)
	return out, nil
}

// fs1TextureParts 解析原版 Parts 元数据得到 499 个部件坐标。框坐标来自用户
// 自己的容器，仓库只保留结构校验值。
func fs1TextureParts(payload []byte, width, height int) ([][4]int, []byte, []byte, error) {
	if len(payload) < fs1TextureHeaderBytes || !bytes.Equal(payload[:20], pad20("Texture")) {
		return nil, nil, nil, errors.New("原版 Texture 载荷缺少 Texture 标记")
	}
	end := int(binary.LittleEndian.Uint32(payload[20:24]))
	pngSize := int(binary.LittleEndian.Uint32(payload[32:36]))
	if end != fs1TextureHeaderBytes+pngSize || end > len(payload) {
		return nil, nil, nil, errors.New("原版 Texture 图像段长度不合法")
	}
	partsStart := (end + 3) &^ 3
	if partsStart+28 > len(payload) || !bytes.Equal(payload[partsStart:partsStart+20], pad20("Parts")) {
		return nil, nil, nil, errors.New("原版 Texture 缺少 Parts 元数据")
	}
	section := int(binary.LittleEndian.Uint32(payload[partsStart+20 : partsStart+24]))
	count := int(binary.LittleEndian.Uint32(payload[partsStart+24 : partsStart+28]))
	if count <= 0 || count > 65536 || section != 28+count*32 || partsStart+section > len(payload) {
		return nil, nil, nil, errors.New("原版 Texture Parts 元数据长度不合法")
	}
	boxes := make([][4]int, count)
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
		if box[0] < 0 || box[1] < 0 || box[2] <= box[0] || box[3] <= box[1] ||
			box[2] > width || box[3] > height {
			return nil, nil, nil, fmt.Errorf("原版 Texture part %d 越界", index)
		}
		boxes[index] = box
	}
	return boxes, payload[:fs1TextureHeaderBytes], payload[partsStart:], nil
}

// paintFS1Strip 把用户原版图集的一行按 Pillow 的预乘 alpha 插值公式拉伸成
// 面板标题渐变条。公式：q = (c*a + 127)/255; c' = q*255/a（截断除法）。
// 源行与结果都来自用户自己的原版像素，7 个面板逐字节验证过。source 必须
// 是清底之前的原版图集副本：渐变源行本身位于面板标题框内。
func paintFS1Strip(atlas *image.NRGBA, source *image.NRGBA, x0, y0 int, panels fs1TexturePanels) error {
	row := y0 + panels.StripRow
	xStart, xEnd := x0+panels.StripX[0], x0+panels.StripX[1]
	hx0, hy0, hx1, hy1 := panels.HeaderBox[0], panels.HeaderBox[1], panels.HeaderBox[2], panels.HeaderBox[3]
	if row < 0 || row >= atlas.Bounds().Dy() || xStart < 0 || xEnd > atlas.Bounds().Dx() {
		return errors.New("面板渐变源行越界")
	}
	if xEnd-xStart != fs1TextureStripWidth || hy1-hy0 != fs1TextureStripHeight {
		return errors.New("面板渐变尺寸不符")
	}
	for y := 0; y < hy1-hy0; y++ {
		for x := 0; x < hx1-hx0; x++ {
			src := source.NRGBAAt(xStart+x, row)
			alpha := int(src.A)
			pixel := color.NRGBA{A: src.A}
			if alpha > 0 {
				for i, channel := range [3]uint8{src.R, src.G, src.B} {
					premultiplied := (int(channel)*alpha + 127) / 255
					value := premultiplied * 255 / alpha
					if value > 255 {
						value = 255
					}
					switch i {
					case 0:
						pixel.R = uint8(value)
					case 1:
						pixel.G = uint8(value)
					case 2:
						pixel.B = uint8(value)
					}
				}
			}
			atlas.SetNRGBA(x0+hx0+x, y0+hy0+y, pixel)
		}
	}
	return nil
}

// applyTextureOp 从用户原版容器与计划携带的墨迹层重建 Texture.tex。
func applyTextureOp(original []byte, op *fs1PlanOp, planEntry *fs1PlanEntry,
	blob []byte, basePath, exePath string) ([]byte, error) {
	if op.DataOffset < 0 || op.DataLength <= 0 ||
		op.DataOffset > int64(len(blob)) || op.DataLength > int64(len(blob))-op.DataOffset {
		return nil, errors.New("Texture 声明数据范围越过数据段")
	}
	// 数据段布局：[JSON 声明][mask PNG][ink PNG]。op.Size 是声明长度，
	// op.DataLength 是整段长度；声明里的 mask/ink 偏移相对整段起点。
	if op.Size <= 0 || op.Size > op.DataLength {
		return nil, errors.New("Texture 声明长度不合法")
	}
	segment := blob[op.DataOffset : op.DataOffset+op.DataLength]
	var declaration fs1TextureDeclaration
	if err := json.Unmarshal(segment[:op.Size], &declaration); err != nil {
		return nil, fmt.Errorf("Texture 声明解析失败: %w", err)
	}
	if declaration.ImageSize[0] <= 0 || declaration.ImageSize[1] <= 0 ||
		declaration.ImageSize[0] > 16384 || declaration.ImageSize[1] > 16384 {
		return nil, errors.New("Texture 图集尺寸不合法")
	}
	if len(declaration.Labels) != fs1TextureLabelCount {
		return nil, errors.New("Texture 标签数量不符")
	}
	if len(original) < 2048+fs1TextureHeaderBytes {
		return nil, errors.New("原版 Texture 过短")
	}
	params, err := LoadSSFParameters(basePath, exePath, "Texture.tex")
	if err != nil {
		return nil, fmt.Errorf("解析 Texture SSF 参数失败: %w", err)
	}
	payload := TransformSSF(original[2048:], params, false)
	width, height := int(binary.LittleEndian.Uint16(payload[36:38])), int(binary.LittleEndian.Uint16(payload[38:40]))
	if width != declaration.ImageSize[0] || height != declaration.ImageSize[1] {
		return nil, errors.New("Texture 图集尺寸与声明不符")
	}
	end := int(binary.LittleEndian.Uint32(payload[20:24]))
	boxes, header, partsSuffix, err := fs1TextureParts(payload, width, height)
	if err != nil {
		return nil, err
	}
	if len(boxes) != declaration.PartCount {
		return nil, fmt.Errorf("Texture part 数量不符：期望 %d，实际 %d", declaration.PartCount, len(boxes))
	}
	decoded, err := png.Decode(bytes.NewReader(payload[fs1TextureHeaderBytes:end]))
	if err != nil {
		return nil, fmt.Errorf("原版 Texture PNG 解码失败: %w", err)
	}
	atlas := image.NewNRGBA(image.Rect(0, 0, width, height))
	draw.Draw(atlas, atlas.Bounds(), decoded, image.Point{}, draw.Src)
	pristineAtlas := image.NewNRGBA(image.Rect(0, 0, width, height))
	draw.Draw(pristineAtlas, pristineAtlas.Bounds(), decoded, image.Point{}, draw.Src)

	// 声明区域清底：标签框按声明的常量颜色，面板主体按黑色。
	for _, label := range declaration.Labels {
		if label.Part < 0 || label.Part >= len(boxes) || boxes[label.Part] != label.Box {
			return nil, fmt.Errorf("Texture 标签 part %d 坐标与原版不符", label.Part)
		}
		region := [4]int{0, 0, label.Box[2] - label.Box[0], label.Box[3] - label.Box[1]}
		if label.Region != nil {
			region = *label.Region
		}
		if region[0] < 0 || region[1] < 0 || region[2] <= region[0] || region[3] <= region[1] ||
			region[2] > label.Box[2]-label.Box[0] || region[3] > label.Box[3]-label.Box[1] {
			return nil, fmt.Errorf("Texture 标签 part %d 区域越界", label.Part)
		}
		fill := color.NRGBA{R: label.Background[0], G: label.Background[1], B: label.Background[2], A: label.Background[3]}
		rect := image.Rect(label.Box[0]+region[0], label.Box[1]+region[1],
			label.Box[0]+region[2], label.Box[1]+region[3])
		draw.Draw(atlas, rect, &image.Uniform{C: fill}, image.Point{}, draw.Src)
	}
	for _, part := range declaration.Panels.Parts {
		if part < 0 || part >= len(boxes) {
			return nil, fmt.Errorf("Texture 面板 part %d 越界", part)
		}
		box := boxes[part]
		body := declaration.Panels.BodyBox
		draw.Draw(atlas, image.Rect(box[0]+body[0], box[1]+body[1], box[0]+body[2], box[1]+body[3]),
			&image.Uniform{C: color.NRGBA{A: 255}}, image.Point{}, draw.Src)
		if err := paintFS1Strip(atlas, pristineAtlas, box[0], box[1], declaration.Panels); err != nil {
			return nil, err
		}
	}

	// 按 mask 叠加译文墨迹。ink 的 RGBA 通道直接覆盖，不做 alpha 合成：
	// 派生工具保证 ink 像素就是最终像素。偏移是数据段内相对偏移。
	if declaration.MaskOffset < op.Size || declaration.MaskLength <= 0 ||
		declaration.MaskOffset > op.DataLength || declaration.MaskLength > op.DataLength-declaration.MaskOffset ||
		declaration.InkOffset < op.Size || declaration.InkLength <= 0 ||
		declaration.InkOffset > op.DataLength || declaration.InkLength > op.DataLength-declaration.InkOffset {
		return nil, errors.New("Texture 墨迹层偏移越界")
	}
	mask, err := decodeFS1Layer(segment[declaration.MaskOffset:declaration.MaskOffset+declaration.MaskLength], width, height, true)
	if err != nil {
		return nil, err
	}
	ink, err := decodeFS1Layer(segment[declaration.InkOffset:declaration.InkOffset+declaration.InkLength], width, height, false)
	if err != nil {
		return nil, err
	}
	grey, ok := mask.(*image.Gray)
	if !ok {
		return nil, errors.New("Texture mask 图层类型不符")
	}
	rgba, ok := ink.(*image.NRGBA)
	if !ok {
		return nil, errors.New("Texture ink 图层类型不符")
	}
	// 透明像素（alpha=0）的 RGB 通道不可表达：Go 的 png.Decode 把它们归零，
	// 而 Pillow 保留原值。为了逐字节复现派生时的像素，这些像素维持原版
	// 原样；mask 因此也只覆盖 alpha>0 的像素（派生工具已保证）。
	for y := 0; y < height; y++ {
		for x := 0; x < width; x++ {
			if grey.GrayAt(x, y).Y != 0 {
				atlas.SetNRGBA(x, y, rgba.NRGBAAt(x, y))
			}
		}
	}

	var pngBuffer bytes.Buffer
	if err := (&png.Encoder{CompressionLevel: png.DefaultCompression}).Encode(&pngBuffer, atlas); err != nil {
		return nil, fmt.Errorf("Texture PNG 编码失败: %w", err)
	}
	pngData := pngBuffer.Bytes()
	// 一代的补丁容器是变长的（历史载荷里 zh/en 都短于原版）：Parts 表跟随
	// 头部 end 字段，不按原版 PNG 长度补齐。头部仅改写两个长度字段。
	newHeader := append([]byte(nil), header...)
	end = fs1TextureHeaderBytes + len(pngData)
	binary.LittleEndian.PutUint32(newHeader[20:24], uint32(end))
	binary.LittleEndian.PutUint32(newHeader[32:36], uint32(len(pngData)))
	rebuilt := make([]byte, 0, end+(4-end%4)%4+len(partsSuffix))
	rebuilt = append(rebuilt, newHeader...)
	rebuilt = append(rebuilt, pngData...)
	rebuilt = append(rebuilt, make([]byte, (4-end%4)%4)...)
	rebuilt = append(rebuilt, partsSuffix...)
	encoded := TransformSSF(rebuilt, params, true)
	output := make([]byte, 2048+len(encoded))
	copy(output[:2048], original[:2048])
	copy(output[2048:], encoded)
	return output, nil
}
