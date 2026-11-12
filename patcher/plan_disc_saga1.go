//go:build !saga2

// 盘片镜像的声明式重建。计划为 FarlandSaga.bin 携带裁剪后的记录流（标题
// 图、主题文件、目录扇区与纯校验位改写仍按差异字节携带）加一组盘片操作：
// 安装期先回放记录流得到近原版容器，再从原版盘读取声明的 ISO 文件、按
// build_events 的语义重建事件脚本或应用拼接/字形，经 MODE1 扇区重打包
// （完整 EDC/ECC）与 SSF 块加密写回输出。每字节语义都由黄金门禁
// （tools/derive_fs1_disc.py verify）与容器哈希双重锁定。
package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
)

// fs1DiscIO 是盘片重建的 I/O 视图：原版盘只读（声明的原版文件从这里取），
// 输出文件接受扇区读改写。SSF 块按需解密、拼接、重加密，不做缓存——
// 相邻扇区共享边界块时顺序写总是读回最新状态。
type fs1DiscIO struct {
	base   *os.File
	out    *os.File
	params *SSFParameters
	tables *DiscTables
}

func openFS1DiscIO(basePath, exePath string, out *os.File) (*fs1DiscIO, error) {
	base, err := os.Open(basePath)
	if err != nil {
		return nil, fmt.Errorf("无法读取原版镜像 %s: %w", basePath, err)
	}
	params, err := LoadSSFParameters(basePath, exePath, "FarlandSaga.bin")
	if err != nil {
		base.Close()
		return nil, fmt.Errorf("解析镜像 SSF 参数失败: %w", err)
	}
	tables, err := ExtractDiscTables(exePath)
	if err != nil {
		base.Close()
		return nil, fmt.Errorf("提取光盘置换表失败: %w", err)
	}
	return &fs1DiscIO{base: base, out: out, params: params, tables: tables}, nil
}

func (d *fs1DiscIO) close() error { return d.base.Close() }

func (d *fs1DiscIO) physical(lba int64) int64 {
	permIndex := discPermIndex*283840 + int(lba)
	if permIndex < 0 || permIndex >= len(d.tables.Permutation) {
		return -1
	}
	return (lba & ^int64(63)) + int64(d.tables.Permutation[permIndex])
}

// readBaseSector 返回原版镜像里一个逻辑扇区的 2352 字节明文。
func (d *fs1DiscIO) readBaseSector(lba int64) ([]byte, error) {
	physical := d.physical(lba)
	if physical < 0 {
		return nil, fmt.Errorf("LBA %d 的置换索引越界", lba)
	}
	offset := physical * sectorSize
	first, last := offset/sectorPayload, (offset+sectorSize+sectorPayload-1)/sectorPayload
	stream := make([]byte, 0, (last-first)*sectorPayload)
	for block := first; block < last; block++ {
		raw := make([]byte, sectorPayload)
		if _, err := d.base.ReadAt(raw, 2048+block*sectorPayload); err != nil {
			return nil, fmt.Errorf("读取原版数据块 %d 失败: %w", block, err)
		}
		stream = append(stream, TransformSSF(raw, d.params, false)...)
	}
	window := stream[offset-first*sectorPayload : offset-first*sectorPayload+sectorSize]
	return SectorTransform(window, int(lba), d.tables.Dwords, d.tables.Qwords, false), nil
}

// readBaseFile 从原版镜像读取一个 ISO 文件的完整内容。
func (d *fs1DiscIO) readBaseFile(lba, size int64) ([]byte, error) {
	out := make([]byte, 0, size)
	for index := int64(0); index*sectorPayload < size; index++ {
		sector, err := d.readBaseSector(lba + index)
		if err != nil {
			return nil, err
		}
		out = append(out, sector[16:2064]...)
	}
	if int64(len(out)) < size {
		return nil, errors.New("原版镜像在文件范围内被截断")
	}
	return out[:size], nil
}

// writeSector 把一个 2048 字节载荷写进输出镜像的逻辑扇区。原位重写保留
// 压制扇区的 MSF 头；fresh 用于重定位到新扇区段的载荷。
func (d *fs1DiscIO) writeSector(lba int64, payload []byte, fresh bool) error {
	if len(payload) > sectorPayload {
		return fmt.Errorf("扇区载荷 %d 字节超过 2048", len(payload))
	}
	var sector [sectorSize]byte
	if fresh {
		frame := lba + 150
		sector[0] = 0x00
		for i := 1; i <= 10; i++ {
			sector[i] = 0xFF
		}
		sector[11] = 0x00
		sector[12] = byte(frame / 75 / 60)
		sector[13] = byte(frame / 75 % 60)
		sector[14] = byte(frame % 75)
		sector[15] = 1
	} else {
		plain, err := d.readSector(lba)
		if err != nil {
			return err
		}
		copy(sector[:], plain)
	}
	copy(sector[16:16+len(payload)], payload)
	if err := FinishSector(sector[:]); err != nil {
		return fmt.Errorf("扇区 EDC/ECC 计算失败 (LBA %d): %w", lba, err)
	}
	cipher := SectorTransform(sector[:], int(lba), d.tables.Dwords, d.tables.Qwords, true)

	offset := d.physical(lba) * sectorSize
	if offset < 0 {
		return fmt.Errorf("LBA %d 的置换索引越界", lba)
	}
	rem, position := cipher, offset
	for len(rem) > 0 {
		base := position / sectorPayload * sectorPayload
		within := position - base
		size := len(rem)
		if size > sectorPayload-int(within) {
			size = sectorPayload - int(within)
		}
		block := make([]byte, sectorPayload)
		if _, err := d.out.ReadAt(block, 2048+base); err != nil {
			return fmt.Errorf("读取输出数据块 %d 失败: %w", base, err)
		}
		plain := TransformSSF(block, d.params, false)
		copy(plain[within:within+int64(size)], rem[:size])
		encoded := TransformSSF(plain, d.params, true)
		if _, err := d.out.WriteAt(encoded, 2048+base); err != nil {
			return fmt.Errorf("写回输出数据块 %d 失败: %w", base, err)
		}
		rem = rem[size:]
		position += int64(size)
	}
	return nil
}

// readSector 返回输出镜像里一个逻辑扇区的当前明文（保留原版 MSF 头）。
func (d *fs1DiscIO) readSector(lba int64) ([]byte, error) {
	physical := d.physical(lba)
	if physical < 0 {
		return nil, fmt.Errorf("LBA %d 的置换索引越界", lba)
	}
	offset := physical * sectorSize
	first, last := offset/sectorPayload, (offset+sectorSize+sectorPayload-1)/sectorPayload
	stream := make([]byte, 0, (last-first)*sectorPayload)
	for block := first; block < last; block++ {
		raw := make([]byte, sectorPayload)
		if _, err := d.out.ReadAt(raw, 2048+block*sectorPayload); err != nil {
			return nil, fmt.Errorf("读取输出数据块 %d 失败: %w", block, err)
		}
		stream = append(stream, TransformSSF(raw, d.params, false)...)
	}
	window := stream[offset-first*sectorPayload : offset-first*sectorPayload+sectorSize]
	return SectorTransform(window, int(lba), d.tables.Dwords, d.tables.Qwords, false), nil
}

// applyDiscOps 按序应用盘片操作。声明文件的原版内容读自原版盘，操作结果
// 写入输出镜像；最终哈希由调用方与计划声明的补丁后哈希核对。
func (s *fs1PlanSource) applyDiscOps(planEntry *fs1PlanEntry, entry fileEntry, basePath, exePath string, out *os.File) error {
	disc, err := openFS1DiscIO(basePath, exePath, out)
	if err != nil {
		return err
	}
	defer disc.close()
	for index := range planEntry.Ops {
		op := &planEntry.Ops[index]
		switch op.Type {
		case "event":
			if err := s.rebuildFS1Event(disc, op); err != nil {
				return fmt.Errorf("%s/%s: %w", entry.Path, op.Name, err)
			}
		case "splice":
			if err := applyFS1DiscSplice(disc, op, s.plan.blob); err != nil {
				return fmt.Errorf("%s/%s: %w", entry.Path, op.Name, err)
			}
		case "discfont":
			if err := applyFS1DiscFont(disc, op, s.plan.blob); err != nil {
				return fmt.Errorf("%s/%s: %w", entry.Path, op.Name, err)
			}
		default:
			return fmt.Errorf("%s: 不支持的操作类型 %q", entry.Path, op.Type)
		}
	}
	return nil
}

// seedDiscOutput 把原版容器整体复制为输出底版；盘片操作在这个纯原版状态
// 的镜像上就地重建声明的文件。
func seedDiscOutput(output *os.File, basePath string, progress func(int64)) error {
	base, err := os.Open(basePath)
	if err != nil {
		return fmt.Errorf("无法读取原版镜像 %s: %w", basePath, err)
	}
	defer base.Close()
	if err := output.Truncate(0); err != nil {
		return err
	}
	written, err := io.Copy(output, base)
	if err != nil {
		return fmt.Errorf("复制原版镜像失败: %w", err)
	}
	if progress != nil {
		progress(written)
	}
	return nil
}

// writeDiscRecords 把保留区域的记录片按容器偏移裸写覆盖。记录片与声明区
// 间不相交，操作完成后原样写回即是最终容器字节。
func writeDiscRecords(output *os.File, stream []byte, progress func(int64)) error {
	reader := bytes.NewReader(stream)
	var written int64
	for {
		var head [12]byte
		if _, err := io.ReadFull(reader, head[:]); err != nil {
			if errors.Is(err, io.EOF) {
				if progress != nil {
					progress(written)
				}
				return nil
			}
			return fmt.Errorf("保留记录被截断: %w", err)
		}
		offset := int64(binary.LittleEndian.Uint64(head[0:8]))
		size := int64(binary.LittleEndian.Uint32(head[8:12]))
		data := make([]byte, size)
		if _, err := io.ReadFull(reader, data); err != nil {
			return fmt.Errorf("保留记录数据被截断: %w", err)
		}
		if _, err := output.WriteAt(data, offset); err != nil {
			return fmt.Errorf("写回保留记录 %d 失败: %w", offset, err)
		}
		written += size
		if progress != nil {
			progress(written)
		}
	}
}

// rebuildFS1Event 按声明的字形对序列重建一个事件脚本：解析原版头与文本行，
// 未翻译行沿用原字形对，构建排序并集的字形表（超容量时压缩到只含用到的
// 对），重写每行的定长容量区，最后写回扇区段。
func (s *fs1PlanSource) rebuildFS1Event(disc *fs1DiscIO, op *fs1PlanOp) error {
	data, err := disc.readBaseFile(op.LBA, op.Size)
	if err != nil {
		return err
	}
	patched, err := rebuildFS1EventBytes(data, op, s.plan.blob)
	if err != nil {
		return err
	}
	return writeFS1FileSectors(disc, op, patched)
}

// rebuildFS1EventBytes 是事件重建的纯字节变换，与
// tools/derive_fs1_disc.py 的黄金门禁逐字节一致（test-data/fs1-evt-vectors.json
// 钉住）。
func rebuildFS1EventBytes(data []byte, op *fs1PlanOp, blob []byte) ([]byte, error) {
	header, err := parseFS1EventHeader(data)
	if err != nil {
		return nil, err
	}
	lines, err := scanFS1EventLines(data, header)
	if err != nil {
		return nil, err
	}
	originalPairs := splitPairs(data[header.TableOffset+2 : header.ScriptStart])
	declared := make(map[int][]byte, len(op.Lines))
	for _, line := range op.Lines {
		declared[line.Line] = blob[line.DataOffset : line.DataOffset+line.DataLength]
	}
	effective := make([][][]byte, len(lines))
	used := map[string][]byte{}
	for index, line := range lines {
		if codes, ok := declared[index+1]; ok {
			pairs := splitPairs(codes)
			if len(pairs) > len(line.Codes) {
				return nil, fmt.Errorf("行 %d 的替换超出原容量", index+1)
			}
			effective[index] = pairs
		} else {
			seq := make([][]byte, len(line.Codes))
			for i, code := range line.Codes {
				if int(code) < 1 || int(code) > len(originalPairs) {
					return nil, fmt.Errorf("行 %d 的字形索引 %d 越界", index+1, code)
				}
				seq[i] = originalPairs[code-1]
			}
			effective[index] = seq
		}
		for _, pair := range effective[index] {
			used[string(pair)] = pair
		}
	}
	pairs := make(map[string][]byte, len(originalPairs)+len(used)+1)
	for _, pair := range originalPairs {
		pairs[string(pair)] = pair
	}
	for key, pair := range used {
		pairs[key] = pair
	}
	pairs["\x81\x40"] = []byte{0x81, 0x40}
	allocation := (int64(len(data)) + sectorPayload - 1) / sectorPayload * sectorPayload
	if header.TableOffset+2+int64(len(pairs))*2+int64(len(data))-header.ScriptStart > allocation {
		// 超出分配容量：丢弃不再被任何行引用的原字形对。
		pairs = used
	}
	ordered := make([]string, 0, len(pairs))
	for key := range pairs {
		ordered = append(ordered, key)
	}
	sort.Slice(ordered, func(i, j int) bool {
		return binary.LittleEndian.Uint16([]byte(ordered[i])) < binary.LittleEndian.Uint16([]byte(ordered[j]))
	})
	indices := make(map[string]int, len(ordered))
	for index, key := range ordered {
		indices[key] = index + 1
	}
	script := append([]byte(nil), data[header.ScriptStart:]...)
	for index, line := range lines {
		textPairs := effective[index]
		capacity := len(line.Codes)
		codebytes := make([]byte, 0, len(textPairs)*2)
		for _, pair := range textPairs {
			var encoded [2]byte
			binary.BigEndian.PutUint16(encoded[:], uint16(indices[string(pair)]))
			codebytes = append(codebytes, encoded[:]...)
		}
		replacement := make([]byte, 0, capacity*2+2)
		replacement = append(replacement, codebytes...)
		replacement = append(replacement, 0, 0)
		for i := 0; i < 2*(capacity-len(textPairs)); i++ {
			replacement = append(replacement, '$')
		}
		offset := int(line.Offset - header.ScriptStart + 1)
		if offset < 0 || offset+len(replacement) > len(script) {
			return nil, fmt.Errorf("行 %d 的写回范围越过脚本", index+1)
		}
		copy(script[offset:offset+len(replacement)], replacement)
	}
	patched := make([]byte, 0, op.SizePatched)
	patched = append(patched, data[:header.TableOffset]...)
	var count [2]byte
	binary.BigEndian.PutUint16(count[:], uint16(len(ordered)))
	patched = append(patched, count[:]...)
	for _, key := range ordered {
		patched = append(patched, key...)
	}
	patched = append(patched, script...)
	if int64(len(patched)) != op.SizePatched {
		return nil, fmt.Errorf("重建大小不符：期望 %d，实际 %d", op.SizePatched, len(patched))
	}
	return patched, nil
}

// writeFS1FileSectors 把重建好的文件写回镜像：重定位文件占用新扇区段
// （全新扇区），原位文件重写整个压制分配（尾部零填充、校验位重算）。
func writeFS1FileSectors(disc *fs1DiscIO, op *fs1PlanOp, patched []byte) error {
	if op.RelocateLBA > 0 {
		for offset := 0; offset < len(patched); offset += sectorPayload {
			end := offset + sectorPayload
			if end > len(patched) {
				end = len(patched)
			}
			if err := disc.writeSector(op.RelocateLBA+int64(offset/sectorPayload),
				patched[offset:end], true); err != nil {
				return err
			}
		}
		return nil
	}
	sectors := (op.Size + sectorPayload - 1) / sectorPayload
	for index := int64(0); index < sectors; index++ {
		start := index * sectorPayload
		end := start + sectorPayload
		if end > int64(len(patched)) {
			end = int64(len(patched))
		}
		payload := patched[start:end]
		if len(payload) < sectorPayload {
			payload = append(append([]byte(nil), payload...), make([]byte, sectorPayload-len(payload))...)
		}
		if err := disc.writeSector(op.LBA+index, payload, false); err != nil {
			return err
		}
	}
	return nil
}

// applyFS1DiscSplice 对定长容量字段做原位字节拼接（菜单、道具、主资源
// 镜像等），整文件读改写后按原分配写回。
func applyFS1DiscSplice(disc *fs1DiscIO, op *fs1PlanOp, blob []byte) error {
	data, err := disc.readBaseFile(op.LBA, op.Size)
	if err != nil {
		return err
	}
	buffer := append([]byte(nil), data...)
	for _, patch := range op.Patches {
		copy(buffer[patch.Offset:patch.Offset+patch.DataLength],
			blob[patch.DataOffset:patch.DataOffset+patch.DataLength])
	}
	return writeFS1FileSectors(disc, op, buffer)
}

// applyFS1DiscFont 把 Unifont 派生的 32 字节字形槽位写进盘片字库的原位。
func applyFS1DiscFont(disc *fs1DiscIO, op *fs1PlanOp, blob []byte) error {
	data, err := disc.readBaseFile(op.LBA, op.Size)
	if err != nil {
		return err
	}
	buffer := append([]byte(nil), data...)
	for index, slot := range op.Slots {
		offset := int64(slot) * 32
		copy(buffer[offset:offset+32], blob[op.GlyphsOffset+int64(index)*32:op.GlyphsOffset+(int64(index)+1)*32])
	}
	return writeFS1FileSectors(disc, op, buffer)
}

// parseFS1EventHeader 解析事件脚本的头：事件数（u16 大端，<100）、按 6 字节
// 事件条目排布的目录、字形表（u16 大端对数 + 2 字节 CP932 对）。
func parseFS1EventHeader(data []byte) (*fs1EventHeader, error) {
	if len(data) < 2 {
		return nil, errors.New("事件脚本过短")
	}
	count := int(binary.BigEndian.Uint16(data[0:2]))
	if count <= 0 || count >= 100 {
		return nil, fmt.Errorf("事件数 %d 不合法", count)
	}
	tableOffset := int64(2 + count*6)
	if tableOffset+2 > int64(len(data)) {
		return nil, errors.New("事件脚本头部被截断")
	}
	glyphCount := int(binary.BigEndian.Uint16(data[tableOffset : tableOffset+2]))
	scriptStart := tableOffset + 2 + int64(glyphCount)*2
	if scriptStart > int64(len(data)) {
		return nil, errors.New("字形表越过文件末尾")
	}
	return &fs1EventHeader{TableOffset: tableOffset, ScriptStart: scriptStart, Events: count}, nil
}

type fs1EventHeader struct {
	TableOffset int64
	ScriptStart int64
	Events      int
}

type fs1EventLine struct {
	Offset int64
	Codes  []uint16
}

// scanFS1EventLines 按脚本操作码扫出全部文本行（0x22 引导，1 起始的
// 大端 u16 字形索引直到 0000 终止）。
func scanFS1EventLines(data []byte, header *fs1EventHeader) ([]fs1EventLine, error) {
	var lines []fs1EventLine
	pos := header.ScriptStart
	for pos < int64(len(data)) {
		switch data[pos] {
		case 0x25:
			pos += 5
		case 0x40:
			pos += 2
		case 0x27:
			end := bytes.IndexByte(data[pos+1:], 0)
			if end < 0 {
				return nil, fmt.Errorf("字节字面量未终止于 %X", pos)
			}
			pos += int64(end) + 2
		case 0x22:
			start := pos
			pos++
			var codes []uint16
			terminated := false
			for pos+2 <= int64(len(data)) {
				code := binary.BigEndian.Uint16(data[pos : pos+2])
				pos += 2
				if code == 0 {
					terminated = true
					break
				}
				codes = append(codes, code)
			}
			if !terminated {
				return nil, fmt.Errorf("文本行未终止于 %X", start)
			}
			lines = append(lines, fs1EventLine{Offset: start, Codes: codes})
		default:
			pos++
		}
	}
	return lines, nil
}

// splitPairs 把连续的字形对字节切成 2 字节一片；奇数长度视为畸形。
func splitPairs(data []byte) [][]byte {
	pairs := make([][]byte, 0, len(data)/2)
	for offset := 0; offset+2 <= len(data); offset += 2 {
		pairs = append(pairs, data[offset:offset+2])
	}
	return pairs
}
