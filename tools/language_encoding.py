"""Compatibility shim for tools.language_encoding -> fls.text.language_encoding."""
import sys
import fls.text.language_encoding as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
