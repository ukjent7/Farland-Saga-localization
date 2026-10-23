//go:build saga2

// 二代专用的 Format 3 扇区回放：补丁包只带修改过的明文扇区，其余部分从
// 原版镜像读取，运行时现场完成扇区混淆、EDC 计算、容器解密与重新加密。
package main

import (
	"bufio"
	"compress/gzip"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

var cdSyncPrefix = []byte{0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00}

func makeSectorHeader(lba uint32) []byte {
	frames := lba + 150
	minute := frames / 4500
	rem := frames % 4500
	second := rem / 75
	frame := rem % 75
	return []byte{
		byte(((minute / 10) << 4) | (minute % 10)),
		byte(((second / 10) << 4) | (second % 10)),
		byte(((frame / 10) << 4) | (frame % 10)),
		1, // MODE1
	}
}

// replaySectors 是只含载荷扇区的回放入口，保留给 GUI 和既有调用方。
func (p *payload) replaySectors(entry fileEntry, basePath string, exePath string, output io.Writer, progress func(int64)) error {
	return p.replaySectorsMerged(entry, basePath, exePath, output, progress, nil)
}

// replaySectorsMerged 在载荷扇区之上叠加 recipe 生成的扇区。
func (p *payload) replaySectorsMerged(entry fileEntry, basePath string, exePath string, output io.Writer, progress func(int64), overrides map[uint32][]byte) error {
	stream, err := p.objects[entry.SectorsObject].Open()
	if err != nil {
		return fmt.Errorf("无法读取扇区对象 %s: %w", entry.SectorsObject, err)
	}
	defer stream.Close()
	gzReader, err := gzip.NewReader(stream)
	if err != nil {
		return fmt.Errorf("扇区对象 %s 不是有效的 gzip: %w", entry.SectorsObject, err)
	}
	defer gzReader.Close()
	return replaySectorStream(entry, basePath, exePath, output, progress, overrides, gzReader)
}

// replaySectorStream rebuilds an image from the pristine base, optional payload
// sectors and recipe overrides. A nil sectors reader means the plan is
// self-contained and only overrides are applied.
func replaySectorStream(entry fileEntry, basePath string, exePath string, output io.Writer, progress func(int64), overrides map[uint32][]byte, sectors *gzip.Reader) error {
	if exePath == "" {
		dir := filepath.Dir(basePath)
		candidate := filepath.Join(dir, gameExecutable)
		if _, err := os.Stat(candidate); err == nil {
			exePath = candidate
		} else {
			candidate = filepath.Join(filepath.Dir(dir), gameExecutable)
			if _, err := os.Stat(candidate); err == nil {
				exePath = candidate
			}
		}
	}
	if _, err := os.Stat(exePath); err != nil {
		return fmt.Errorf("找不到游戏可执行文件 %s: %w", exePath, err)
	}

	discTables, err := ExtractDiscTables(exePath)
	if err != nil {
		return fmt.Errorf("提取光盘置换表失败: %w", err)
	}

	ssfParams, err := LoadSSFParameters(basePath, exePath, filepath.Base(entry.Path))
	if err != nil {
		return fmt.Errorf("解析 SSF 参数失败: %w", err)
	}

	baseFile, err := os.Open(basePath)
	if err != nil {
		return fmt.Errorf("无法读取原版文件 %s: %w", basePath, err)
	}
	defer baseFile.Close()

	edits := make(map[int64][]byte)
	hdrBuf := make([]byte, 8)
	sectorBuf := make([]byte, 2352)

	// addData 把一个逻辑 LBA 的明文载荷写进 2352 字节扇区流。edits 以
	// 2048 字节块为键，因为 SSF 容器按 2048 字节块加密。
	addData := func(lba uint32, data []byte) error {
		if len(data) != 2048 {
			return fmt.Errorf("不支持的扇区载荷长度 %d (LBA %d)", len(data), lba)
		}
		copy(sectorBuf[:12], cdSyncPrefix)
		copy(sectorBuf[12:16], makeSectorHeader(lba))
		copy(sectorBuf[16:2064], data)
		if err := FinishSectorZeroECC(sectorBuf); err != nil {
			return fmt.Errorf("扇区 EDC 计算失败 (LBA %d): %w", lba, err)
		}

		cipher := SectorTransform(sectorBuf, int(lba), discTables.Dwords, discTables.Qwords, true)

		permIdx := 2*283840 + int(lba)
		if permIdx >= len(discTables.Permutation) {
			return fmt.Errorf("LBA 置换索引越界: %d", lba)
		}
		target := (int(lba) & ^63) + int(discTables.Permutation[permIdx])
		offset := int64(target) * 2352

		rem := cipher
		for len(rem) > 0 {
			base := (offset / 2048) * 2048
			plainBlock, ok := edits[base]
			if !ok {
				encBlock := make([]byte, 2048)
				if _, err := baseFile.Seek(2048+base, io.SeekStart); err != nil {
					return fmt.Errorf("定位原版基准块 0x%X 失败: %w", 2048+base, err)
				}
				if _, err := io.ReadFull(baseFile, encBlock); err != nil {
					return fmt.Errorf("读取原版基准块 0x%X 失败: %w", 2048+base, err)
				}
				plainBlock = TransformSSF(encBlock, ssfParams, false)
				edits[base] = plainBlock
			}
			within := int(offset - base)
			sz := min(len(rem), 2048-within)
			copy(plainBlock[within:within+sz], rem[:sz])
			rem = rem[sz:]
			offset += int64(sz)
		}
		return nil
	}

	if sectors != nil {
		for {
			if _, err := io.ReadFull(sectors, hdrBuf); err != nil {
				if errors.Is(err, io.EOF) {
					break
				}
				return fmt.Errorf("读取扇区头失败: %w", err)
			}
			lba := binary.LittleEndian.Uint32(hdrBuf[0:4])
			length := binary.LittleEndian.Uint32(hdrBuf[4:8])
			if length != 2048 {
				return fmt.Errorf("不支持的扇区载荷长度 %d (LBA %d)", length, lba)
			}
			if _, err := io.ReadFull(sectors, sectorBuf[16:2064]); err != nil {
				return fmt.Errorf("读取扇区数据失败 (LBA %d): %w", lba, err)
			}
			if _, ok := overrides[lba]; ok {
				// recipe 生成的内容优先于载荷；资源本身的期望哈希在生成阶段
				// 已经核对，这里不再要求与旧载荷逐字节一致。
				continue
			}
			if err := addData(lba, sectorBuf[16:2064]); err != nil {
				return err
			}
		}
	}

	for lba, data := range overrides {
		if err := addData(lba, data); err != nil {
			return err
		}
	}

	for base, plainBlock := range edits {
		edits[base] = TransformSSF(plainBlock, ssfParams, true)
	}

	if _, err := baseFile.Seek(0, io.SeekStart); err != nil {
		return fmt.Errorf("重置原版基准文件位置失败: %w", err)
	}

	bufReader := bufio.NewReaderSize(baseFile, 1024*1024)
	bufWriter := bufio.NewWriterSize(output, 1024*1024)
	hash := sha256.New()
	var position int64
	var lastProgress int64

	writeChunk := func(data []byte) error {
		hash.Write(data)
		if output != nil {
			if _, err := bufWriter.Write(data); err != nil {
				return err
			}
		}
		position += int64(len(data))
		if progress != nil && (position-lastProgress >= 256*1024 || position == entry.PatchedSize) {
			progress(position)
			lastProgress = position
		}
		return nil
	}

	headerChunk := make([]byte, 2048)
	if _, err := io.ReadFull(bufReader, headerChunk); err != nil {
		return fmt.Errorf("读取原版头部失败: %w", err)
	}
	if err := writeChunk(headerChunk); err != nil {
		return err
	}

	totalPayload := entry.PatchedSize - 2048
	fullBlocks := (totalPayload / 2048) * 2048
	origChunk := make([]byte, 2048)
	for base := int64(0); base < fullBlocks; base += 2048 {
		if _, err := io.ReadFull(bufReader, origChunk); err != nil {
			return fmt.Errorf("读取原版数据流 0x%X 失败: %w", 2048+base, err)
		}
		toWrite := origChunk
		if editedChunk, ok := edits[base]; ok {
			toWrite = editedChunk
		}
		if err := writeChunk(toWrite); err != nil {
			return err
		}
	}

	tailLen := totalPayload - fullBlocks
	if tailLen > 0 {
		tailBuf := make([]byte, tailLen)
		if _, err := io.ReadFull(bufReader, tailBuf); err != nil {
			return fmt.Errorf("读取原版尾部失败: %w", err)
		}
		if err := writeChunk(tailBuf); err != nil {
			return err
		}
	}

	if output != nil {
		if err := bufWriter.Flush(); err != nil {
			return fmt.Errorf("刷新写入失败: %w", err)
		}
	}

	if position != entry.PatchedSize {
		return fmt.Errorf("扇区重构长度不符: 期望 %d, 实际 %d", entry.PatchedSize, position)
	}
	rebuiltSHA256 := hex.EncodeToString(hash.Sum(nil))
	if len(overrides) == 0 && rebuiltSHA256 != entry.PatchedSHA256 {
		return fmt.Errorf("扇区重构哈希不符: 期望 %s, 实际 %s", entry.PatchedSHA256, rebuiltSHA256)
	}
	return nil
}
