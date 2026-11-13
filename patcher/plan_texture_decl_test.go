//go:build !saga2

package main

import (
	"encoding/base64"
	"errors"
)

func base64Decode(value string) ([]byte, error) {
	return base64.StdEncoding.DecodeString(value)
}

func errorsNew(text string) error {
	return errors.New(text)
}
