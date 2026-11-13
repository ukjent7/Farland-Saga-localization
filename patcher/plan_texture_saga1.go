//go:build !saga2

// Texture.tex 的声明式重建。计划携带本地化图集的 PNG 字节；安装期从用户
// 原版容器取 SSF 参数与 Parts 元数据，按 [头部][PNG][对齐][原版元数据] 的
// 布局重排并重新加密。PNG 与元数据之间以零补齐到 4 字节边界；重加密后
// 的长度与哈希必须与计划声明一致。
package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
)

const fs1TextureHeaderBytes = 40

// pad20 把字符串填充为定长 20 字节的 SSF 段标记（与二代 recipe_texture 相同）。
func pad20(value string) []byte {
	out := bytes.Repeat([]byte{' '}, 20)
	copy(out, value)
	return out
}

// applyTextureOp 从原版容器与计划携带的本地化 PNG 重建 Texture.tex。
// op.DataOffset 指向 blob 里的 PNG；紧随其后的是原版 Parts 元数据，
// 因此这里按 [40 字节头部][PNG][补零][元数据] 拼装明文，再以原版容器的
// SSF 参数重新加密。长度与哈希由调用方按计划声明核对。
func applyTextureOp(original []byte, op *fs1PlanOp, planEntry *fs1PlanEntry,
	blob []byte, basePath, exePath string) ([]byte, error) {
	png := blob[op.DataOffset : op.DataOffset+op.DataLength]
	metadata := blob[op.DataOffset+op.DataLength : op.DataOffset+op.DataLength+op.Size]
	if len(metadata) < 28 || !bytes.Equal(metadata[:20], pad20("Parts")) {
		return nil, errors.New("Texture 元数据缺少 Parts 标记")
	}
	if len(original) < 2048+fs1TextureHeaderBytes {
		return nil, errors.New("原版 Texture 过短")
	}

	params, err := LoadSSFParameters(basePath, exePath, "Texture.tex")
	if err != nil {
		return nil, fmt.Errorf("解析 Texture SSF 参数失败: %w", err)
	}
	// 整段解密原版容器流（含尾部流式加密区），核对 Texture 标记后改写
	// PNG 长度字段，再整段重新加密。
	plain := TransformSSF(original[2048:], params, false)
	if len(plain) < fs1TextureHeaderBytes || !bytes.Equal(plain[:20], pad20("Texture")) {
		return nil, errors.New("原版 Texture 头部不合法")
	}
	header := append([]byte(nil), plain[:fs1TextureHeaderBytes]...)
	end := fs1TextureHeaderBytes + len(png)
	binary.LittleEndian.PutUint32(header[20:24], uint32(end))
	binary.LittleEndian.PutUint32(header[32:36], uint32(len(png)))
	rebuilt := make([]byte, 0, end+(4-end%4)%4+len(metadata))
	rebuilt = append(rebuilt, header...)
	rebuilt = append(rebuilt, png...)
	rebuilt = append(rebuilt, make([]byte, (4-end%4)%4)...)
	rebuilt = append(rebuilt, metadata...)

	encoded := TransformSSF(rebuilt, params, true)
	output := make([]byte, 2048+len(encoded))
	copy(output[:2048], original[:2048])
	copy(output[2048:], encoded)
	return output, nil
}

