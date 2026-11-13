//go:build saga2

package main

import "errors"

func textureIdentityAvailable() bool { return false }

func runTextureIdentity([]string) error {
	return errors.New("texture-identity 仅适用于一代补丁器")
}
