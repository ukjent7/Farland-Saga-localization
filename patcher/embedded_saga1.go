//go:build !saga2

// 一代内嵌的补丁载荷目录。embed 指令要求路径是字面量，所以每个作品各写
// 一份；payloadDir 在 game_saga1.go 里与之对应。
package main

import (
	"embed"
	"io/fs"
	"path/filepath"
	"sort"
	"strings"
)

//go:embed all:payload/saga1
var embeddedPayloads embed.FS

func embeddedPlanPayloads() ([]*payload, []string) { return nil, nil }

func embeddedZipPayloads() ([]*payload, []string) {
	entries, err := fs.ReadDir(embeddedPayloads, payloadDir)
	if err != nil {
		return nil, []string{err.Error()}
	}
	var loaded []*payload
	var problems []string
	for _, entry := range entries {
		if entry.IsDir() || !strings.EqualFold(filepath.Ext(entry.Name()), ".zip") {
			continue
		}
		data, err := fs.ReadFile(embeddedPayloads, payloadDir+"/"+entry.Name())
		if err != nil {
			problems = append(problems, entry.Name()+": "+err.Error())
			continue
		}
		item, err := openPayloadData(entry.Name(), data)
		if err != nil {
			problems = append(problems, entry.Name()+": "+err.Error())
			continue
		}
		loaded = append(loaded, item)
	}
	sort.Slice(loaded, func(i, j int) bool {
		return languageRank(loaded[i].info.Language) < languageRank(loaded[j].info.Language)
	})
	return loaded, problems
}
