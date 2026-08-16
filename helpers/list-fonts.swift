#!/usr/bin/env swift
import AppKit
import CoreText
import Foundation

func argument(_ name: String, default fallback: String) -> String {
    guard let index = CommandLine.arguments.firstIndex(of: name), index + 1 < CommandLine.arguments.count else {
        return fallback
    }
    return CommandLine.arguments[index + 1]
}

let language = argument("--language", default: "ko")
let sample = argument("--sample", default: "한글 자막")
let units = Array(sample.utf16)
let families = NSFontManager.shared.availableFontFamilies.sorted()
let fonts: [[String: Any]] = families.map { family in
    let font = CTFontCreateWithName(family as CFString, 18, nil)
    var glyphs = [CGGlyph](repeating: 0, count: units.count)
    let supported = units.withUnsafeBufferPointer { chars in
        glyphs.withUnsafeMutableBufferPointer { output in
            CTFontGetGlyphsForCharacters(font, chars.baseAddress!, output.baseAddress!, units.count)
        }
    }
    return ["family": family, "supports_sample": supported && !glyphs.contains(0)]
}
let payload: [String: Any] = [
    "source": "CoreText",
    "language": language,
    "sample": sample,
    "fonts": fonts,
]
let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
print(String(decoding: data, as: UTF8.self))
