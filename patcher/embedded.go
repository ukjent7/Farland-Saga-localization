// 编译期内嵌的补丁载荷。
//
// 发布形态是单个可执行文件。二代把公开仓库里的 recipe 编译成 plan.bin 后
// 由 go:embed 内嵌；一代仍按语言把 farland-patch-*.zip 放进 payload/saga1
// 再嵌入。两个作品的内嵌来源由 embedded_saga*.go 按构建标签二选一，这段
// 代码只负责统一顺序和解析结果。
package main

// loadEmbedded 读取编译期内嵌的全部补丁载荷。整包读进内存，按中、韩、英
// 的固定顺序返回，按钮位置才稳定。解析失败的包作为问题返回，不让一个坏包
// 拖垮整个程序。
func loadEmbedded() ([]*payload, []string) {
	if payloads, problems := embeddedPlanPayloads(); len(payloads) > 0 || len(problems) > 0 {
		return payloads, problems
	}
	return embeddedZipPayloads()
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
