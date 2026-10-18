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

// nativeLanguageName 与 uiStrings.name 在 i18n.go：前者给第一屏的语言按钮
// 用（每种语言显示自己的名字），后者给选定语言之后的界面用（用当前界面语言
// 称呼各语言）。
