// 编译期内嵌的补丁载荷。
//
// 发布形态是单个可执行文件：打包脚本把各语言的 farland-patch-*.zip 放进
// payload/ 再执行 go build，用户拿到的 exe 自带全部补丁，不需要再分发压缩包。
// 这段代码放在平台中立文件里，Linux 上也能跑测试验证内嵌补丁可解析。
package main

import (
	"embed"
	"io/fs"
	"path/filepath"
	"sort"
	"strings"
)

// embeddedPayloads 内嵌 payload/ 下的全部补丁。用 all: 前缀是为了让只放了
// 占位文件的空目录也能编译通过。
//
//go:embed all:payload
var embeddedPayloads embed.FS

// loadEmbedded 读取编译期内嵌的全部补丁载荷。整包读进内存，按中、韩、英
// 的固定顺序返回，按钮位置才稳定。解析失败的包作为问题返回，不让一个坏包
// 拖垮整个程序。
func loadEmbedded() ([]*payload, []string) {
	entries, err := fs.ReadDir(embeddedPayloads, "payload")
	if err != nil {
		return nil, []string{err.Error()}
	}
	var loaded []*payload
	var problems []string
	for _, entry := range entries {
		if entry.IsDir() || !strings.EqualFold(filepath.Ext(entry.Name()), ".zip") {
			continue
		}
		data, err := fs.ReadFile(embeddedPayloads, "payload/"+entry.Name())
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

// languageRank 固定中、韩、英的顺序，未知语言排在最后。
func languageRank(code string) int {
	switch code {
	case "zh-CN":
		return 0
	case "ko-KR":
		return 1
	case "en-US":
		return 2
	}
	return 3
}

// languageLabel 把 manifest 里的语言代码换成界面上显示的中文名。
func languageLabel(code string) string {
	switch code {
	case "zh-CN":
		return "简体中文"
	case "ko-KR":
		return "韩文"
	case "en-US":
		return "English"
	}
	return code
}
