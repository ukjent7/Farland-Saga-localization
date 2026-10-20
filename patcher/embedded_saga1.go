//go:build !saga2

// 一代内嵌的补丁载荷目录。embed 指令要求路径是字面量，所以每个作品各写
// 一份；payloadDir 在 game_saga1.go 里与之对应。
package main

import "embed"

//go:embed all:payload/saga1
var embeddedPayloads embed.FS
